"""会话与消息数据仓储层。

【模块职责说明】：
1. 仓储模式与数据持久化抽象（Repository Pattern）：
   封装 Conversation（会话）与 Message（消息）实体的全生命周期持久化逻辑，隔离底层 SQL 拼装、索引排序与 ORM 映射细节，
   避免上层业务 Service 与 FastAPI 路由直接侵入数据库查询。
2. 工作单元与事务约定（Unit of Work）：
   遵循经典仓储设计规范，内部严禁主动调用 `commit()` 提交事务。
   新增或批量写入实体时仅执行 `await session.flush()` 提前把变更推入数据库缓冲区并回填自增 ID、默认时间戳等属性，
   事务的最终提交与回滚生命周期全权交由外层编排或依赖注入管理器统筹。
3. 关系预加载防 N+1 查询（Eager Loading）：
   全量查询会话消息时通过 `selectinload(Message.citations)` 显式预加载引用的出处分块关系，
   杜绝前端展示历史引用时因懒加载引发的异步上下文 MissingGreenlet 或 N+1 查询风暴。
4. 倒序截断与正序回放算法（Reverse Fetching Optimization）：
   获取最近 N 条对话上下文时，采用“底层 SQL 倒序 limit N + 内存反转正序”的高效查询模式，
   相比传统的先 count 再 offset，节省了一次全量 count 聚合查询，显著降低长会话高频推理的数据库负载。
5. 领域消息工厂封装（Message Factory Helpers）：
   提供 `make_user_message` 与 `make_assistant_message` 静态工厂方法，收敛强类型消息构造规则与默认元数据封装。
"""

from collections.abc import Sequence
from uuid import UUID

# 引入 SQLAlchemy 核心查询组件与函数表达式（count 聚合需要 func）
from sqlalchemy import func, select
# 引入异步数据库会话类
from sqlalchemy.ext.asyncio import AsyncSession
# 引入关系预加载选项，避免懒加载 N+1 查询
from sqlalchemy.orm import selectinload

# 引入数据库持久层实体与消息角色枚举
from app.db.models import Conversation, Message, MessageRole

# ==============================================================================
# 模块级全局常量
# ==============================================================================
# 【为什么抽取为模块常量，而不是在方法内直接写字面量 "新对话"？】
# 遵循单一真实数据源（Single Source of Truth, SSOT）原则。
# 当前它有两个独立的消费者（消费方）：
#   1. `create()` 的默认实参：用于初始落库。
#   2. `update_title_if_default()`：用于判断“当前会话标题是否仍处于未被用户碰过的初始状态”。
# 如果写死字面量，后续只要产品文案改动（例如改成“未命名会话”），开发者极易只修改其中一处，
# 导致两边字符串不匹配，从而使“首问自动改标题”的核心业务逻辑产生静默失效（Silent Failure），
# 且无任何报警日志。
DEFAULT_CONVERSATION_TITLE = "新对话"


class ConversationRepository:
    """
    会话与消息仓储：负责封装对 Conversation 及关联 Message 实体的底层数据库读写操作。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        初始化仓储实例，注入当前请求/任务上下文绑定的异步数据库会话（AsyncSession）。
        """
        self.session = session

    async def create(
        self,
        title: str = DEFAULT_CONVERSATION_TITLE,
        *,
        user_id: UUID | None = None,
    ) -> Conversation:
        """
        创建并持久化一个新会话实体。

        :param user_id: 会话归属人（第 11 期新增）。默认 None 是为了兼容
                        内部调用（如评测、脚本）；HTTP 路径必须显式传入，
                        否则会造出"无主会话"——它在任何用户的列表里都看不到。

        【仓储层的事务与提交约定】：
        这里仅执行 await self.session.flush()，而不是 session.commit()。
        - 为什么 flush？为了向数据库预发送 INSERT 语句，立刻回填由数据库生成的 UUID 主键
          以及 server_default 生成的创建/更新时间戳，让返回值拥有完整属性供上层业务使用。
        - 为什么不 commit？仓储层只做数据访问，事务的边界（Commit / Rollback）必须由外层
          Service 层或 Unit of Work 严格控制，防止出现部分逻辑失败但底层仓储已提前提交的脏事务。
        """
        conversation = Conversation(title=title, user_id=user_id)
        self.session.add(conversation)
        await self.session.flush()
        return conversation

    # ==========================================================================
    # 新增方法 1：消息计数（首问检测）
    # ==========================================================================
    async def count_messages(self, conversation_id: UUID) -> int:
        """统计指定会话下的消息总条数。

        【设计目标与业务场景】：
        专门用于判定“当前提问是否为该会话的首次提问”。当 count == 0 时，说明用户正在开启
        第一轮对话，此时会触发后续的“提取首问文本并自动修改会话标题”逻辑。

        【为什么不复用已有仓储方法（如 list_messages）？】
        若使用 len(await self.list_messages(conversation_id))：
        ORM 会将该会话下的所有 Message 记录完全反序列化为 Python 对象加载进内存，
        当会话包含几十甚至上百条历史消息时，会产生严重的内存与反序列化开销。
        这里直接发送轻量级的 `SELECT count(id)`，让数据库内部完成行统计，开销接近于 0。
        """
        stmt = select(func.count(Message.id)).where(
            Message.conversation_id == conversation_id
        )
        # 【为什么用 scalar_one() 以及显式 int() 强转？】
        # 1. 聚合函数 COUNT() 在 SQL 规范下恒定返回“一行一列”，scalar_one() 在无结果或
        #    多于一行时会显式抛异常，是最符合聚合查询语义的取值方式。
        # 2. 不同的异步数据库驱动（如 asyncpg、aiomysql）或不同配置下，COUNT 的返回值可能为
        #    Decimal 类型甚至特殊包装类，外层显式包裹 int(...) 可确保对外输出强类型的标准 Python int。
        return int((await self.session.execute(stmt)).scalar_one())

    # ==========================================================================
    # 新增方法 2：分页列表（防 N+1 聚合查询）
    # ==========================================================================
    async def list_page(
        self,
        page: int,
        page_size: int,
        *,
        user_id: UUID | None = None,
    ) -> tuple[list[tuple[Conversation, int]], int]:
        """按 updated_at 倒序分页拉取会话列表，并原子性地附带每个会话当前包含的消息条数。

        :param user_id: 归属过滤（第 11 期新增）。传值则只返回该用户的会话；
                        传 None 表示"不限制"（admin 视角 / 内部调用）
        :return: ([(Conversation实体, 消息条数), ...], 满足条件的总会话数)

        【⚠️ 分页的"数据"与"总数"必须用同一套过滤条件】
        下面 `stmt` 与 `count_stmt` 各自带一份 `if user_id is not None` 的判断 ——
        看起来重复，但**绝不能只改一处**：若只过滤数据而不过滤总数，
        前端算出来的总页数会比实际能翻的页数多，翻到后面就是空列表。
        这是分页接口最经典的一类 bug，两处的条件必须严格同源。
        """
        # ----------------------------------------------------------------------
        # 1. 入参防御性约束（Defensive Clamping）
        # ----------------------------------------------------------------------
        # - 页码兜底：强制不能小于第 1 页，防止 offset 计算出负数导致 SQL 语法报错。
        # - 分页大小兜底：上下限双重夹取（1 ~ 100）。上限截断至 100 是为了防止前端恶意传递
        #   page_size=1000000 导致内存打爆或长事务拖垮数据库连接池。
        page = max(page, 1)
        page_size = max(min(page_size, 100), 1)
        offset = (page - 1) * page_size

        # ----------------------------------------------------------------------
        # 2. 单次查询完成数据拉取与统计（核心：杜绝 N+1 查询）
        # ----------------------------------------------------------------------
        # 【为什么坚决不用“查出会话列表后，循环调用 count_messages”？】
        # 传统做法如果每页 20 条，就会产生 1 次查会话 + 20 次查统计 = 21 次数据库 I/O（经典 N+1）。
        # 这里通过 LEFT JOIN + GROUP BY，单次 SQL 交互直接让数据库引擎在底层一次性算好。
        #
        # 【为什么必须用 outerjoin (LEFT JOIN) 而不能用 join (INNER JOIN)？】
        # 刚创建的全新会话还没有产生任何一条 Message。如果使用 INNER JOIN，所有消息数为 0 的
        # 崭新会话都会在关联阶段被数据库自动过滤剔除，造成“刚建好的会话在列表页凭空消失”的严重 Bug。
        #
        # 【双字段排序的考量】：
        # order_by(Conversation.updated_at.desc(), Conversation.id.desc())
        # - 第一排序键 updated_at.desc()：保证最活跃（最新聊过）的会话始终置顶显示。
        # - 第二排序键 id.desc()（主键兜底）：防止极端高并发场景下多个会话的更新时间戳完全相同，
        #   导致数据库由于无序返回产生分页漂移（同一数据在翻页时重复出现或漏出）。
        msg_count = func.count(Message.id).label("message_count")
        stmt = (
            select(Conversation, msg_count)
            .outerjoin(Message, Message.conversation_id == Conversation.id)
            .group_by(Conversation.id)
            .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
            .limit(page_size)
            .offset(offset)
        )
        # 归属过滤：与下面的 count_stmt 必须用同一个条件（见 docstring 的提醒）
        if user_id is not None:
            stmt = stmt.where(Conversation.user_id == user_id)

        rows = (await self.session.execute(stmt)).all()
        # row[0] 为 Conversation ORM 实体，row[1] 为聚合算出的 count 值
        items = [(row[0], int(row[1])) for row in rows]

        # ----------------------------------------------------------------------
        # 3. 统计全表总条数（Total Count）
        # ----------------------------------------------------------------------
        # 分页接口必须向前端返回 total 字段以供前端组件计算“总页数”。
        # 在关系型数据库的标准分页规范中，列表数据和总记录数天然属于两次语义独立的查询。
        count_stmt = select(func.count(Conversation.id))
        if user_id is not None:
            count_stmt = count_stmt.where(Conversation.user_id == user_id)

        total = int((await self.session.execute(count_stmt)).scalar_one())
        return items, total

    # ==========================================================================
    # 新增方法 3：会话硬删除（级联机制与 404 状态区分）
    # ==========================================================================
    async def delete(
        self, conversation_id: UUID, *, user_id: UUID | None = None
    ) -> bool:
        """硬删除指定会话实体。

        :param conversation_id: 目标会话主键 UUID
        :param user_id: 归属过滤（第 11 期新增）。传值则只删除属于该用户的会话；
                        传 None 表示"不限制"（内部调用）
        :return: 删除成功返回 True；若记录不存在 **或不属于该用户** 则返回 False

        【⚠️ 这里复用 self.get(..., user_id=user_id)，而不是自己另拼一套 where】
        删除前的存在性检查必须与"归属校验"共用同一段逻辑 ——
        若检查用一套条件、删除用另一套，就会出现"检查通过但删错了行"这类危险偏差。
        复用 get 也顺带保证了"无权访问 == 不存在 == 返回 False"三者在语义上完全一致。

        【级联删除说明】：
        会话下的历史消息（Message）及其引用的知识库片段（AnswerCitation）不需要在此处手写
        多次循环删除，底层的数据库表外键约束已配置 ON DELETE CASCADE，由数据库引擎在底层原子清理。

        【为什么选择“先 get 确认存在，再 delete”，而不是直接发送 DELETE 语句？】
        1. 语义明确：直接执行 `delete(Conversation).where(...)` 只能通过返回游标的 cursor.rowcount
           来判断是否命中，但在不同的异步数据库驱动和连接池封装中，rowcount 的兼容性和准确性存在差异。
        2. ORM 生命周期与缓存状态同步：先 get 可以利用 Session 的 Identity Map，让 ORM 感知到
           该实体的生命周期转变（从 persistent 变为 deleted），避免内存脏状态。
        """
        conversation = await self.get(conversation_id, user_id=user_id)
        if conversation is None:
            return False

        await self.session.delete(conversation)
        # 同样仅执行 flush，将 DELETE 操作发往数据库进行外键约束检查，严格不提交事务
        await self.session.flush()
        return True

    # ==========================================================================
    # 新增方法 4：标题条件更新（用户意图保护机制）
    # ==========================================================================
    async def update_title_if_default(
        self,
        conversation_id: UUID,
        title: str,
    ) -> None:
        """在首次提问完成后，自动尝试将默认会话标题替换为首问摘要文本。

        【核心业务约束（为什么是 if_default 而不是无条件覆盖？）】：
        在实际业务中，用户在第一次提问前，完全有可能先在侧边栏手动点击了“重命名”。
        如果此处直接执行无条件 UPDATE，系统就会以首次提问的内容暴力覆盖用户自己辛苦键入的自定义名称，
        这属于典型的“系统侵占用户意志”。
        因此必须严格做此守卫：只有当会话当前的标题依然等于 DEFAULT_CONVERSATION_TITLE 时，
        才被视为“用户尚未对标题表态”，系统才可以安全地代为自动命名。
        """
        # 1. 文本清洗防御：过滤纯空格、制表符或换行。如果用户发的是空字符，拒绝修改标题
        new_title = title.strip()
        if not new_title:
            return

        conversation = await self.get(conversation_id)
        # 2. 会话不存在，或者标题已经被用户主动修改过（不再等于初始默认常量）-> 直接安全退出
        if conversation is None or conversation.title != DEFAULT_CONVERSATION_TITLE:
            return

        # 3. 边界截断：限制在前 30 字符。
        # 一方面兼顾前端左侧历史列表的单行宽度排版美观，另一方面防止超长 Prompt 突破数据库 VARCHAR 列上限
        conversation.title = new_title[:30]
        # 仅 flush 暂存变更，统一等待外层 Service 决断最终 commit
        await self.session.flush()
    async def get(
        self, conversation_id: UUID, *, user_id: UUID | None = None
    ) -> Conversation | None:
        """按主键获取会话实体（返回 None 表示不存在 **或无权访问**）。

        :param conversation_id: 会话唯一标识
        :param user_id: 归属过滤（第 11 期新增）。传值则强制要求该会话属于此用户；
                        传 None 表示"不限制"（供后台任务、评测等内部路径使用）
        :return: 对应的 Conversation 实体；不存在或不属于该用户时返回 None

        【为什么把"无权访问"也返回 None，而不是抛 403】
        若"存在但无权"抛 403、"不存在"返回 None，调用方就能通过状态码差异
        **探测出某个 conversation_id 是否真实存在**（越权信息的侧信道）。
        统一返回 None、由上层一律翻译成 404，可以彻底抹掉这个差异。

        【为什么要有 user_id=None 这条不限制的路径】
        会话仓储不只服务于 HTTP 请求 —— 评测跑批、后台任务等内部路径没有"当前登录用户"，
        它们需要能取到任意会话。用 None 显式表达"这是内部调用"，
        比让内部调用伪造一个 user_id 更诚实。

        【为什么传了 user_id 就不能走 session.get()】
        `session.get()` 只接受主键，无法附带 `WHERE user_id = ...` 这样的过滤条件。
        所以一旦要做归属校验，就必须改用 `select()` 显式构造查询。
        """
        if user_id is None:
            # 内部路径：只按主键取，不做归属限制
            return await self.session.get(Conversation, conversation_id)

        stmt = select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_messages(self, conversation_id: UUID) -> list[Message]:
        """
        按时间正序返回该会话下的所有历史消息（包含关联引用数据，用于前端完整回放历史）。

        :param conversation_id: 会话唯一标识
        :return: 包含 citation 关系的完整消息实体列表
        """
        stmt = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            # 双重排序保证毫秒一致时依然有确定性顺序
            .order_by(Message.created_at.asc(), Message.id.asc())
            # 预加载消息所关联的知识库引用（避免异步环境下触发展开属性时的懒加载错误）
            .options(selectinload(Message.citations))
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def recent_messages(self, conversation_id: UUID, limit: int) -> list[Message]:
        """
        提取指定会话最近的 N 条消息，并按时间正序排列返回（常用于组装 RAG 的 chat_history 上下文）。

        :param conversation_id: 会话唯一标识
        :param limit: 最大获取消息条数
        :return: 正序排列的最近 N 条消息列表
        """
        # 边界防守：若限制数量非法则直接返回空
        if limit <= 0:
            return []

        # 优化策略：先按时间倒序 limit N 取出最新的 N 条，再在 Python 内存中反转为正序
        # 避免为了算正序而先去 count(*) 总行数，单次查询搞定
        stmt = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(limit)
        )
        rows = list((await self.session.execute(stmt)).scalars().all())
        # 反转列表恢复时间正序
        return list(reversed(rows))

    async def add_messages(self, messages: Sequence[Message]) -> None:
        """
        批量向数据库追加消息。

        :param messages: 待持久化的 Message 序列
        """
        if not messages:
            return
        self.session.add_all(messages)
        # 推送至数据库，触发底层校验与生成默认属性
        await self.session.flush()

    @staticmethod
    def make_user_message(conversation_id: UUID, content: str) -> Message:
        """
        构建用户端提问（USER 角色）消息实体的工厂方法。

        :param conversation_id: 所属会话 ID
        :param content: 用户提问文本
        :return: 待持久化的 Message ORM 实例
        """
        return Message(
            conversation_id=conversation_id,
            role=MessageRole.USER,
            content=content,
        )

    @staticmethod
    def make_assistant_message(
        conversation_id: UUID,
        content: str,
        *,
        extra_metadata: dict | None = None,
    ) -> Message:
        """
        构建模型回复端（ASSISTANT 角色）消息实体的工厂方法。

        :param conversation_id: 所属会话 ID
        :param content: 大模型生成的自然语言回答
        :param extra_metadata: 附加元数据（例如 Token 消耗、推理时延、检索诊断标识等）
        :return: 待持久化的 Message ORM 实例
        """
        return Message(
            conversation_id=conversation_id,
            role=MessageRole.ASSISTANT,
            content=content,
            extra_metadata=extra_metadata or {},
        )