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

# 引入 SQLAlchemy 核心查询组件
from sqlalchemy import select
# 引入异步数据库会话类
from sqlalchemy.ext.asyncio import AsyncSession
# 引入关系预加载选项，避免懒加载 N+1 查询
from sqlalchemy.orm import selectinload

# 引入数据库持久层实体与消息角色枚举
from app.db.models import Conversation, Message, MessageRole


class ConversationRepository:
    """
    会话与消息仓储：负责会话元数据及其对话历史消息的数据库访问操作。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        初始化仓储实例，注入当前请求绑定的异步数据库会话。
        """
        self.session = session

    async def create(self, title: str = "新对话") -> Conversation:
        """
        创建并持久化一个新的对话会话。

        :param title: 对话标题，默认为"新对话"
        :return: 持久化并刷新后的 Conversation 实体
        """
        conversation = Conversation(title=title)
        self.session.add(conversation)
        # 仅 flush，回填生成的主键与默认时间戳，不主动 commit
        await self.session.flush()
        return conversation

    async def get(self, conversation_id: UUID) -> Conversation | None:
        """
        根据主键 UUID 获取指定的会话实体。

        :param conversation_id: 会话唯一标识
        :return: 对应的 Conversation 实体；若不存在则返回 None
        """
        return await self.session.get(Conversation, conversation_id)

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