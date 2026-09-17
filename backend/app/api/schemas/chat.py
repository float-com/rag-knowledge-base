"""知识库问答 API 请求与响应模型（契约层）。

【模块职责说明】：
1. 协议契约声明（API Contract Declaration）：
   用 Pydantic 模型统一描述问答模块对外的请求体与响应体结构。
   路由层与 Service 层之间只通过本模块的模型交换数据，避免把 ORM 实体直接暴露到 HTTP 边界。

2. OpenAPI 类型推导（Schema-driven Codegen）：
   所有字段都带类型注解与约束（如 max_length），FastAPI 会自动生成精确的 OpenAPI 文档，
   前端据此生成 TypeScript 类型，做到前后端字段零猜测。

3. ORM 实体到响应模型的转换（Entity-to-DTO Mapping）：
   通过 model_config 的 from_attributes 与自定义 from_orm 工厂方法，
   把 SQLAlchemy 实体（Conversation / Message / AnswerCitation）翻译成脱敏的响应结构。
   其中 CitationRead 需要从快照字段映射，MessageRead 需要按角色决定是否附带引用。

4. 引用序号语义（Ordinal Semantics）：
   CitationRead.ordinal 必须与 prompt 中给大模型看到的「片段 N」编号一致（从 1 开始），
   前端据此渲染 [N] 角标。严禁按数组下标重新推导，否则重排序后会张冠李戴。
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.db.models import AnswerCitation, Message

# 消息角色字面量联合类型：
# 用 Literal 而非 Enum，使 OpenAPI 直接输出枚举候选值字符串，前端无需额外映射
MessageRoleValue = Literal["user", "assistant", "system"]


# =============================================================================
# 1. 会话相关模型
# =============================================================================
class ConversationCreate(BaseModel):
    """创建会话的请求体。"""

    # 会话标题：默认「新对话」，限制 1~256 字符，防止空标题与超长标题
    title: str = Field("新对话", min_length=1, max_length=256)


class ConversationRead(BaseModel):
    """会话详情响应体。

    model_config 的 from_attributes=True 让 Pydantic 可以直接从 ORM 实体
    （Conversation）按属性名取值，免去手写转换函数。
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    created_at: datetime
    updated_at: datetime


# =============================================================================
# 2. 引用快照模型
# =============================================================================
class CitationRead(BaseModel):
    """assistant 消息引用的 chunk 快照。

    【为什么是「快照」而不是外键查询】：
    原文档或 chunk 可能在后续被重新索引或删除，外键会被置空（ON DELETE SET NULL），
    但历史会话里的引用卡片仍必须完整展示当时的溯源信息。
    因此 document_name / page_no / quote 采用冗余存储，读取时直接返回。

    【可为空的字段】：
    document_id 与 chunk_id 在原文被删除后为 NULL，因此类型必须是 `UUID | None`，
    而不是 `UUID`——否则序列化时校验会直接失败。
    """

    id: UUID
    # 与 prompt 中的「片段 N」编号一致（从 1 开始）；前端按此渲染 [N] 角标，
    # 禁止按数组下标推导，避免重排序/过滤后引用串号
    ordinal: int
    # 历史快照：外键在原文被删除后会被置空
    document_id: UUID | None = None
    chunk_id: UUID | None = None
    document_name: str
    page_no: int | None = None
    quote: str

    @classmethod
    def from_orm(cls, citation: AnswerCitation) -> "CitationRead":
        """从 AnswerCitation ORM 实体构建引用响应模型。

        :param citation: AnswerCitation 实体实例
        :return: 引用响应模型
        """
        return cls(
            id=citation.id,
            ordinal=citation.ordinal,
            document_id=citation.document_id,
            chunk_id=citation.chunk_id,
            document_name=citation.document_name,
            page_no=citation.page_no,
            quote=citation.quote,
        )


# =============================================================================
# 3. 消息模型
# =============================================================================
class MessageRead(BaseModel):
    """单条消息响应体（含 assistant 消息的引用列表）。"""

    id: UUID
    role: MessageRoleValue
    content: str
    created_at: datetime
    # 引用列表：只有 assistant 消息才会有；默认空列表，避免前端判空
    citations: list[CitationRead] = Field(default_factory=list)

    @classmethod
    def from_orm(cls, message: Message) -> "MessageRead":
        """从 Message ORM 实体构建消息响应模型。

        【为什么按角色过滤引用】：
        只有 assistant 消息会产生引用（user / system 消息不会）。
        这里显式判断角色，避免用户消息因异常数据关联了引用而把脏数据透出到前端。

        【前置条件】：
        调用方查询消息时必须已预加载 citations 关系
        （conversation_repo.list_messages 内部已使用 selectinload），
        否则在异步环境下访问 message.citations 会触发懒加载并抛出 MissingGreenlet。

        :param message: Message 实体实例
        :return: 消息响应模型
        """
        return cls(
            id=message.id,
            role=message.role,
            content=message.content,
            created_at=message.created_at,
            citations=(
                [CitationRead.from_orm(c) for c in message.citations]
                if message.role == "assistant"
                else []
            ),
        )


# =============================================================================
# 4. 会话详情与问答请求模型
# =============================================================================
class ConversationDetail(BaseModel):
    """会话详情响应体：会话主体 + 全部历史消息（用于前端完整回放）。"""

    conversation: ConversationRead
    messages: list[MessageRead]


class ChatRequest(BaseModel):
    """流式问答的请求体。

    question 限制 1~2000 字符：
    - min_length=1 拦截空提问，避免无意义地消耗一次向量检索与大模型调用；
    - max_length=2000 控制上下文预算，防止超长提问挤占参考资料与历史消息的 Token 空间。
    """

    question: str = Field(min_length=1, max_length=2000)
