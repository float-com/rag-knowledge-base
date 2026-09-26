"""
【模块职责说明】
本模块为知识库底层持久化层中的「文档仓储组件」（DocumentRepository）。
基于 SQLAlchemy 2.0+ 异步编程模型（AsyncSession），封装了对 Document 实体的全套 CRUD 逻辑。
核心设计理念与架构规范如下：

1. 仓储模式（Repository Pattern）与业务解耦：
   隔离底层 SQL 拼装与 ORM 映射细节，向上层业务 Service 提供领域对象操作接口。
2. 事务控制约定（Unit of Work）：
   仓储层严禁自主调用 `commit()` 提交事务。所有写操作仅通过 `flush()` 提前推送 SQL 变更
   （以便即时获取自增列、默认时间戳或校验约束），事务生命周期与最终提交/回滚全权交由外层 Service 控制。
3. 纯异步 I/O 驱动（Async Engine）：
   全量方法采用 `async/await` 搭配 asyncpg 异步驱动，避免数据库 I/O 阻塞 FastAPI 事件循环。
"""

from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.core.permissions import WILDCARD_PERMISSION_TAG
from app.db.models import Document, DocumentStatus


def build_permission_filter(
    permission_tags: list[str] | None,
) -> ColumnElement[bool] | None:
    """构造 `documents` 的权限过滤条件。

    :param permission_tags: 调用方的有效权限标签。**传 None 表示"不做权限过滤"**
                            （供内部调用、admin 视角使用）
    :return: 可直接 `.where(...)` 的条件表达式；None 表示无需过滤

    【⚠️ 本函数是全项目最容易写错的一处 —— 空数组的语义】
    直觉写法是只写"数组重叠"：

        Document.permission_tags.op("&&")(permission_tags)

    但 PostgreSQL 的 `&&` 语义是"两边都必须有元素可重叠"，
    **空数组与任何东西都不重叠，包括另一个空数组**。实测确认：

        SELECT ... WHERE permission_tags && '{}'::varchar[]   -- 返回 0 行！

    而本项目的约定是「**空数组 = 公开，任何登录用户都能看**」（兼容前 8 期上传的存量文档）。
    所以必须显式补一个 OR 分支：

        权限标签重叠  OR  文档标签为空（公开）

    【少了那个 OR 分支会怎样】
    - 有 hr 标签的用户：只看到 HR 文档，公开文档看不到；
    - **无任何角色的用户：一篇都看不到** —— 存量 10 篇文档全体失联。
    这比"少看见"严重得多，它是**静默的全面失联**。

    【加了 OR 分支会不会让 GIN 索引失效？不会】
    已实测 EXPLAIN：PostgreSQL 会把它规划成 `BitmapOr`，
    两个分支各自走 `ix_documents_permission_tags`：

        BitmapOr
          -> Bitmap Index Scan on ix_documents_permission_tags  (permission_tags && '{hr}')
          -> Bitmap Index Scan on ix_documents_permission_tags  (permission_tags = '{}')

    【为什么 admin 要短路】
    持通配标签 `"*"` 时直接 return None（不加任何条件）：
    既省掉一次条件计算，也符合"admin 看全量、且跑得最快"的预期。
    """
    if permission_tags is None:
        # 内部调用：不做权限过滤（例如评测跑批、后台任务）
        return None
    if WILDCARD_PERMISSION_TAG in permission_tags:
        # admin 视角：等价于"不加权限条件"，直接返回 None 而不是空条件
        return None
    return or_(
        Document.permission_tags.op("&&")(permission_tags),
        # 【⚠️ 这里必须传【空列表】而不是字符串 "{}"】
        # 直觉写法是 `Document.permission_tags == "{}"`（照抄 SQL 里的数组字面量），
        # 但那样 SQLAlchemy 会按【字符串】绑定参数，生成：
        #     documents.permission_tags = $1::VARCHAR
        # 而左边是 `varchar[]`，PostgreSQL 直接报：
        #     operator does not exist: character varying[] = character varying
        # （本项目实测踩过这个 500。）
        #
        # 传 [] 时 SQLAlchemy 会沿用左侧的数组类型来绑定参数，生成的 SQL 是：
        #     documents.permission_tags = $1::VARCHAR[]
        # 类型对得上，PostgreSQL 正确识别为"空数组比较"。
        Document.permission_tags == [],
    )


class DocumentRepository:
    """文档实体数据库仓储类，封装针对 Document 表的原子化数据访问。"""

    def __init__(self, session: AsyncSession) -> None:
        # 语法（Python 原生实例绑定）：self.var = param
        #   属性说明 (self.session: AsyncSession)：持有当前请求生命周期内的异步数据库会话引用（由依赖注入容器提供）
        self.session = session

    async def get_by_id(
        self, document_id: UUID, *, permission_tags: list[str] | None = None
    ) -> Document | None:
        """根据文档全局唯一主键 ID 检索单条文档。

        【参数说明】：
        - document_id (UUID): 文档的 UUID 主键标识
        - permission_tags: 【第 11 期新增】调用方有效权限标签。传 None 表示不做权限过滤；
                            传值则要求该文档对调用方可见（公开或标签重叠）

        【返回值】：
        - Document | None: 命中的文档实体模型；若不存在 **或调用方无权查看** 则返回 None

        【为什么"无权"也返回 None 而不是抛 403】
        与 conversation_repo 同一考量：若"存在但无权"报 403、"不存在"返回 None，
        调用方就能靠状态码差异探测出某个 document_id 是否真实存在。统一 None 更安全。
        """
        if permission_tags is None:
            # 内部调用 / admin：直接用主键查询（能命中 Identity Map，最省）
            return await self.session.get(Document, document_id)

        where = build_permission_filter(permission_tags)
        stmt = select(Document).where(Document.id == document_id)
        if where is not None:
            stmt = stmt.where(where)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_hash(self, file_hash: str) -> Document | None:
        """根据文件内容 SHA256/MD5 指纹哈希检索文档（用于上传重复秒传校验）。

        【参数说明】：
        - file_hash (str): 文件完整内容的哈希指纹字符串

        【返回值】：
        - Document | None: 存在相同内容哈希的文档实体；若无重复则返回 None
        """
        # 语法（SQLAlchemy 2.0 声明式查询构建）：select(entity).where(condition)
        #   构造方法 (select)：指定查询的目标实体（Document）
        #   条件过滤 (.where)：绑定等值过滤表达式（Document.file_hash == file_hash）
        stmt = select(Document).where(Document.file_hash == file_hash)

        # 语法（SQLAlchemy 异步执行与单标量提取）：
        #   会话执行 (await self.session.execute(stmt))：异步向数据库发送 SQL 执行请求并返回 ResultProxy
        #   标量解析 (.scalar_one_or_none())：提取首行首列实体；若无匹配记录返回 None，若匹配超过一条抛出 MultipleResultsFound
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def add(self, document: Document) -> Document:
        """持久化新增文档记录并回填数据库生成字段。

        【参数说明】：
        - document (Document): 待新增的文档 ORM 实体实例

        【返回值】：
        - Document: 持久化且已填充主键与默认值的实体对象
        """
        # 语法（SQLAlchemy 原生实例暂存）：session.add(instance)
        #   参数1 (instance: Any)：将新创建的瞬态（Transient）实体纳入当前 Session 上下文追踪
        self.session.add(document)

        # 语法（SQLAlchemy 异步刷新）：await session.flush()
        #   方法特性：仅将变更 SQL (INSERT) 发送至数据库执行，以触发默认约束并回填主键，
        #             但并不提交事务（不执行 COMMIT），连接事务依然处于打开状态
        await self.session.flush()

        # 语法（Python 原生返回）：返回已被数据库回填完完整字段属性的持久态实体
        return document

    async def update_status(
        self,
        document_id: UUID,
        status: DocumentStatus,
        *,
        error_message: str | None = None,
    ) -> None:
        """更新文档生命周期状态及失败报错上下文。

        【参数说明】：
        - document_id (UUID): 待更新的文档主键 ID
        - status (DocumentStatus): 目标状态枚举（如 PARSING、INDEXED、FAILED 等）
        - * (Python 仅限关键字实参语法): 限制后续参数必须使用命名参数传入
        - error_message (str | None): 失败时捕获的错误堆栈摘要，成功时重置为 None
        """
        # 语法（业务内部复用）：通过内部 get_by_id 异步查询实体以纳入当前 Session 追踪
        doc = await self.get_by_id(document_id)

        # 语法（Python 原生控制流）：防御性判空，若记录已被物理删除则优雅退出
        if doc is None:
            return

        # 语法（ORM 原地属性脏更新）：直接修改已追踪实体的属性字段，等待外层事务提交时自动生成 UPDATE 语句
        doc.status = status

        # 语法（Python 原生复合条件判断）：
        #   判断逻辑：仅当显式传入了错误信息，或者当前状态并非失败时（即处理成功时主动清空旧错误残留），才覆写 error_message
        if error_message is not None or status != DocumentStatus.FAILED:
            doc.error_message = error_message

    async def list_paginated(
        self,
        page: int,
        page_size: int,
        *,
        status: DocumentStatus | None = None,
        permission_tags: list[str] | None = None,
    ) -> tuple[list[Document], int]:
        """分页获取文档列表及满足条件的总记录数。

        【参数说明】：
        - page (int): 当前请求的目标页码（从 1 开始计）
        - page_size (int): 每页拉取的文档条数限制
        - status (DocumentStatus | None): 可选的状态枚举过滤标签
        - permission_tags: 【第 11 期新增】调用方有效权限标签。传 None 表示不做权限过滤

        【返回值】：
        - tuple[list[Document], int]: (当前页文档实体列表, 符合条件的总数据条数)

        【⚠️ 权限条件必须同时作用于 items_stmt 与 count_stmt】
        与 conversation_repo.list_page 同一个道理：只过滤数据不过滤总数，
        会让前端算出的总页数比实际可翻的页数多，翻到后面就是空列表。
        """
        # 语法（Python 原生算术运算）：根据页码与每页大小推算数据库游标偏移量（对标 MySQL/PG OFFSET 语法）
        offset = (page - 1) * page_size

        # 语法（SQLAlchemy 2.0 分页列表查询拼装）：
        #   排序 (.order_by)：按创建时间倒序排列（Document.created_at.desc()）
        #   游标跳过 (.offset(offset))：设置查询跳过的行数
        #   条数截断 (.limit(page_size))：设置当前批次拉取上限
        items_stmt = (
            select(Document)
            .order_by(Document.created_at.desc())
            .offset(offset)
            .limit(page_size)
        )

        # 语法（SQLAlchemy 聚合计数语句构建）：
        #   函数调用 (func.count())：生成 COUNT(*) 聚合统计表达式
        #   表源指定 (.select_from(Document))：指定聚合统计的目标主表
        count_stmt = select(func.count()).select_from(Document)

        # 语法（动态 SQL 条件追加）：
        #   判断逻辑：若调用方显式传入了状态过滤参数，同步为列表查询与计数查询追加 WHERE 过滤条件
        if status is not None:
            items_stmt = items_stmt.where(Document.status == status)
            count_stmt = count_stmt.where(Document.status == status)

        # 【第 11 期】权限过滤：同样两个语句都要加（见 docstring 的提醒）
        permission_where = build_permission_filter(permission_tags)
        if permission_where is not None:
            items_stmt = items_stmt.where(permission_where)
            count_stmt = count_stmt.where(permission_where)

        # 语法（SQLAlchemy 标量多行异步提取）：
        #   .scalars()：将执行结果扁平化提取为 ORM 映射实体流（剥离 Tuple 包装）
        #   .all()：将结果集序列全部消费并载入内存集合
        items = (await self.session.execute(items_stmt)).scalars().all()

        # 语法（SQLAlchemy 聚合值单值异步提取）：
        #   .scalar_one()：断言执行结果必有且仅有一行单列值（即 COUNT 统计结果），若为空抛出 NoResultFound
        total = (await self.session.execute(count_stmt)).scalar_one()

        # 语法（Python 原生类型强制转换与元组构造）：
        #   list(items)：转换为标准 Python 列表
        #   int(total)：转换为纯整型数字，组合为元组返回
        return list(items), int(total)

    async def delete(self, document: Document) -> None:
        """从数据库中删除文档实体（关联的切片 chunk 依赖 ORM cascade="all, delete-orphan" 自动级联清理）。

        【参数说明】：
        - document (Document): 待删除的目标文档持久态实体
        """
        # 语法（SQLAlchemy 异步删除标记）：await session.delete(instance)
        #   参数1 (instance: Any)：将指定持久态模型标记为 DELETED 状态，外层事务提交时将生成对应 DELETE 语句
        await self.session.delete(document)