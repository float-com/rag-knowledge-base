"""
【模块职责说明】
本模块为知识库底层持久化层中的「切片仓储组件」（DocumentChunkRepository）。
基于 SQLAlchemy 2.0+ 异步模型，专门负责文档细粒度切片（DocumentChunk）的高性能数据访问。
核心架构机制与设计考量如下：

1. 强安全多租户归属校验（Anti-IDOR / 防越权）：
   在获取切片时强制组合 `document_id` 与 `chunk_id` 双键过滤，从数据层物理阻断越权访问其他文档切片的风险。
2. 批量高性能写入契约（Bulk Insertion）：
   支持通过 `Sequence` 传入切片集合并调用 `session.add_all()` + `flush()`，
   显著降低数据库往返开销（RTT），保障大规模分块持久化的吞吐。
3. 数据库服务端聚合推导（Server-side Aggregation）：
   文档切片长度统计指标（count/avg/min/max）直接通过 PostgreSQL 原生 `char_length` 及聚合函数在数据库引擎内单条 SQL 完成计算，
   避免将海量切片原始文本加载到 Python 进程二次遍历引发内存膨胀（OOM）。
4. 统计结果载体不可变性保障：
   定义 `ChunkStats` 数据类并启用 `frozen=True`（不可变对象），确保统计数据在业务传递过程中只读且线程安全。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DocumentChunk


# 语法（Python 原生标准库装饰器）：@dataclass(frozen=True)
#   装饰器说明 (@dataclass)：自动为类生成 __init__、__repr__、__eq__ 等常用样板方法，专注充当纯数据传输对象（DTO / VO）
#   参数说明 (frozen=True)：冻结实体属性，实例化后字段只读不可修改（类似 Java 14+ Record 或 final 类），防止数据流转中被意外篡改
@dataclass(frozen=True)
class ChunkStats:
    """单个文档下的 chunk 长度聚合统计数据模型（DTO）。"""
    # 语法（Python 原生类型标注）：声明各统计指标强类型为整数（int）
    total: int
    avg_length: int
    min_length: int
    max_length: int


class DocumentChunkRepository:
    """文档切片实体数据库仓储类，封装对 DocumentChunk 表的原子增删改查。"""

    def __init__(self, session: AsyncSession) -> None:
        # 语法（Python 原生属性绑定）：持有当前请求生命周期的异步会话实例
        self.session = session

    async def bulk_add(self, chunks: Sequence[DocumentChunk]) -> None:
        """批量持久化切片实体集合。

        【参数说明】：
        - chunks: Sequence[DocumentChunk] 切片实体序列（抽象容器类型，兼容 list、tuple 等）
        """
        # 语法（Python 原生隐式布尔判断）：若传入序列为空，快速返回避免触发无意义的数据库 I/O
        if not chunks:
            return

        # 语法（SQLAlchemy 批量纳入追踪）：session.add_all(instances)
        #   参数1 (instances: Iterable[Any])：将切片实体批量纳入当前 Session 上下文追踪
        self.session.add_all(chunks)

        # 语法（SQLAlchemy 异步执行并下推 SQL）：
        #   方法说明 (flush)：一次性生成批量 INSERT SQL 写入数据库连接，但不提交事务（留给 Service 统一 commit）
        await self.session.flush()

    async def delete_by_document(self, document_id: UUID) -> None:
        """物理删除指定文档关联的所有切片记录。

        【参数说明】：
        - document_id (UUID): 父文档唯一主键标识
        """
        # 语法（SQLAlchemy 2.0 批量删除语句构建）：delete(entity).where(condition)
        #   函数说明 (delete)：构建 DELETE FROM document_chunks 语句
        #   条件绑定 (.where)：限定只删除外键绑定当前 document_id 的记录
        stmt = delete(DocumentChunk).where(DocumentChunk.document_id == document_id)

        # 语法（SQLAlchemy 异步执行）：发送 SQL 执行物理删除
        await self.session.execute(stmt)

    async def list_paginated_by_document(
        self,
        document_id: UUID,
        page: int,
        page_size: int,
    ) -> tuple[list[DocumentChunk], int]:
        """按文档主键分页检索切片列表（按切片序号升序），并返回切片总条数。

        【参数说明】：
        - document_id (UUID): 父文档主键 ID
        - page (int): 当前页码（从 1 开始计）
        - page_size (int): 单页大小截断限制

        【返回值】：
        - tuple[list[DocumentChunk], int]: (当前页切片对象列表, 关联切片总数)
        """
        # 语法（Python 原生算术运算）：根据页码推算数据库游标偏移行数
        offset = (page - 1) * page_size

        # 语法（SQLAlchemy 2.0 分页构建）：
        #   条件过滤 (.where)：限定当前文档主键
        #   时序排序 (.order_by(DocumentChunk.chunk_index.asc()))：按切片逻辑序号严格升序，还原原始阅读顺序
        #   游标切分 (.offset().limit())：执行数据库服务端物理分页
        items_stmt = (
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.chunk_index.asc())
            .offset(offset)
            .limit(page_size)
        )

        # 语法（SQLAlchemy 聚合计数构建）：基于文档 ID 统计该文档拥有的总切片数
        count_stmt = (
            select(func.count())
            .select_from(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
        )

        # 语法（SQLAlchemy 异步执行与实体序列消费）：
        #   .scalars().all()：扁平化提取 ORM 映射切片实体并转为序列
        items = (await self.session.execute(items_stmt)).scalars().all()

        # 语法（SQLAlchemy 异步执行与聚合标量单值提取）：
        #   .scalar_one()：提取单行单列的 COUNT 计数结果
        total = (await self.session.execute(count_stmt)).scalar_one()

        # 语法（Python 原生类型构造与元组返回）：返回 (切片列表, 记录总数)
        return list(items), int(total)

    async def get_for_document(
        self,
        document_id: UUID,
        chunk_id: UUID,
    ) -> DocumentChunk | None:
        """根据文档 ID 与切片 ID 复合检索单个切片实体。

        强校验归属，杜绝跨文档越权（水平越权漏洞防护）。

        【参数说明】：
        - document_id (UUID): 期望所属的父文档主键
        - chunk_id (UUID): 切片自身唯一标识

        【返回值】：
        - DocumentChunk | None: 命中的切片实体；若未命中或切片并不归属于该文档，返回 None
        """
        # 语法（SQLAlchemy 2.0 复合条件声明）：
        #   多条件过滤 (.where(cond1, cond2))：隐式按 AND 逻辑拼接两个过滤条件
        stmt = select(DocumentChunk).where(
            DocumentChunk.id == chunk_id,
            DocumentChunk.document_id == document_id,
        )

        # 语法（SQLAlchemy 异步执行与安全单记录提取）：
        #   .scalar_one_or_none()：命中返回单实体，未命中返回 None，重复数据抛出异常
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_stats(self, document_id: UUID) -> ChunkStats | None:
        """一条聚合 SQL 查询文档切片的 count/avg/min/max 指标，避免拉取到 Python 内存二次遍历。

        【参数说明】：
        - document_id (UUID): 目标父文档 ID

        【返回值】：
        - ChunkStats | None: 聚合指标 DTO；若该文档尚无切片数据，返回 None
        """
        # 语法（SQLAlchemy 数据库原生字符长度计算函数）：func.char_length(column)
        #   函数映射：映射到 PostgreSQL 原生 CHAR_LENGTH() 函数，按字符而非字节计算长度
        length = func.char_length(DocumentChunk.content)

        # 语法（SQLAlchemy 2.0 聚合查询与别名指定）：
        #   .label("alias")：为聚合计算列打上字段别名标签，便于后续按属性名提取
        #   func.count() / func.avg() / func.min() / func.max()：分别映射标准 SQL 聚合函数
        stmt = (
            select(
                func.count().label("total"),
                func.avg(length).label("avg_len"),
                func.min(length).label("min_len"),
                func.max(length).label("max_len"),
            )
            .where(DocumentChunk.document_id == document_id)
        )

        # 语法（SQLAlchemy 异步行提取）：
        #   方法调用 (.one())：断言聚合查询必有一行元组返回（即使无数据，COUNT 也返回 0，其余为 NULL）
        row = (await self.session.execute(stmt)).one()

        # 语法（Python 原生防御性判断）：若聚合结果总条数为 0，说明未入库或无分块，直接返回 None
        if not row.total:
            return None

        # 语法（Python 原生短路或运算与数据类实例化）：
        #   短路兜底 (row.field or 0)：防范 SQL avg/min/max 返回 None 时触发类型转换异常
        #   强类型实例化 (ChunkStats(...))：包装为只读 DTO 返回
        return ChunkStats(
            total=int(row.total),
            avg_length=int(row.avg_len or 0),
            min_length=int(row.min_len or 0),
            max_length=int(row.max_len or 0),
        )