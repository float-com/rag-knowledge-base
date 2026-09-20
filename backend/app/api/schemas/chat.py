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

# 查询优化策略字面量联合类型：
# 取值必须与 app.workflows.rag_state.QueryRoute 完全一致，
# 也与前端 QueryRouteRead.route 契约对齐（前端按它索引调试面板配置）
QueryRouteValue = Literal["original", "rewrite", "hyde", "multi_query"]


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
class RetrievalMeta(BaseModel):
    """混合检索调试元数据。

    【六个字段的语义与"能不能横向比较"】：
    - sources: 该 chunk 命中的检索路（vector / keyword）；两路都命中即"混合"
    - *_rank: 在该路召回结果中的名次（从 1 开始），用于复盘排序
    - vector_score: cosine similarity，**绝对值有意义**，做拒答阈值用
    - keyword_score: ts_rank，**相对值**，跨 query 不可比
      （实测同一查询下第 2~5 名分数完全相同，且不同查询间尺度会变）
    - rrf_score: 两路融合分，**仅在同一次检索内可比**
      （k=60 时上限为 2/(k+1) ≈ 0.0328，与余弦相似度不是一个量纲）

    【为什么全部字段都有默认值】：
    来源不同的 chunk 只填自己那一路的字段（仅向量命中时 keyword_* 为 None，
    反之亦然），因此每个字段都必须可缺省。默认值让"只填一部分"的构造方式成立，
    也让前端类型不必声明为可选键——键一定在，值可能为 None。
    """

    # 命中来源：列表而非单个字符串，因为两路都命中时会有两个元素
    sources: list[str] = Field(default_factory=list)
    # 向量路名次（1 起）；未命中该路为 None
    vector_rank: int | None = None
    # 余弦相似度：绝对值有意义，是拒答判定的依据
    vector_score: float | None = None
    # 关键词路名次（1 起）；未命中该路为 None
    keyword_rank: int | None = None
    # ts_rank：只在本路内部有相对意义，跨查询不可比
    keyword_score: float | None = None
    # RRF 融合分：只在同一次检索内可比
    rrf_score: float | None = None


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
    # 混合检索调试元数据；历史消息（第 6 期之前写入的）没有这个字段，
    # 解析失败时静默为 None，前端按缺失隐藏 Tag
    retrieval_meta: RetrievalMeta | None = None

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
            retrieval_meta=_parse_retrieval_meta(citation.retrieval_meta),
        )


def _parse_retrieval_meta(raw: dict | None) -> RetrievalMeta | None:
    """历史消息没有 retrieval_meta，非法/缺失静默返回 None。

    与上一期 `_parse_query_route` 相同的兜底风格：反序列化路径上的解析函数
    **绝不能向上抛异常**——一条历史脏数据不应让整个会话详情接口 500。
    因此这里做两层防御：先判类型（不是 dict 直接返回 None），
    再用 try 包住模型校验（字段类型不符时也返回 None，而不是抛 ValidationError）。
    """
    # 第一层：None（历史消息）与非 dict（脏数据）直接放行
    if not isinstance(raw, dict):
        return None
    try:
        # 第二层：字段类型不符时静默降级，不阻断整个响应
        return RetrievalMeta.model_validate(raw)
    except Exception:
        return None



# =============================================================================
# 3. 查询优化路由快照模型
# =============================================================================
class QueryRouteRead(BaseModel):
    """Query 优化的调试快照。仅 assistant 消息会带，前端用于渲染调试面板。

    既用于流式问答的 query_route 事件载荷，也内嵌在消息响应里供历史回放使用。

    【与 QueryRoute 字面量的对应关系】：
    route 的四个取值必须与 `app.workflows.rag_state.QueryRoute` 严格一致，
    否则前端按 route 索引的调试面板配置（ROUTE_META）会查不到而渲染异常。

    【为什么四个明细字段都可空】：
    每次只会命中一种策略，因此只有对应的那个明细字段有值，其余为 None。
    """

    route: QueryRouteValue
    # 实际用于检索的查询词（rewrite/hyde 路径下是改写后的文本）
    query: str
    # 以下三项按策略产出，未产出时为 None
    rewritten_query: str | None = None
    hyde_answer: str | None = None
    multi_queries: list[str] | None = None


def _parse_query_route(metadata: dict | None) -> QueryRouteRead | None:
    """从 messages.metadata 中提取 query_route 字段。

    【为什么要三层防御】：
    metadata 是 JSONB 自由结构，内容不受 Schema 约束，因此必须假设它可能是：
      ① None 或空字典      → 老数据、或本功能上线前写入的历史消息
      ② 缺少 query_route 键 → user 消息从不写该键
      ③ 键存在但不是 dict   → 极端脏数据
      ④ 键存在且是 dict，但结构与契约不符 → 交给 Pydantic 校验拦截

    任何一种情况都【静默返回 None】，只让该条消息的调试面板不显示，
    绝不因为一段可选元数据而让整个历史接口报错。

    :param metadata: Message.extra_metadata（可为 None）
    :return: 校验通过的 QueryRouteRead；缺失或非法时返回 None
    """
    if not metadata:
        return None
    raw = metadata.get("query_route")
    if not isinstance(raw, dict):
        return None
    try:
        # 交给 Pydantic 校验：多余字段会被忽略，结构不符则抛错后被兜住
        return QueryRouteRead.model_validate(raw)
    except Exception:
        return None


# =============================================================================
# 4. 消息模型
# =============================================================================
class MessageRead(BaseModel):
    """单条消息响应体（含 assistant 消息的引用列表与查询优化快照）。"""

    id: UUID
    role: MessageRoleValue
    content: str
    created_at: datetime
    # 引用列表：只有 assistant 消息才会有；默认空列表，避免前端判空
    citations: list[CitationRead] = Field(default_factory=list)
    # 查询优化快照：从消息元数据中取出，供历史回放时继续展示调试面板。
    # 【为什么必须是顶层字段】前端 fromServerMessage 读取的是 m.query_route，
    # 若只存在数据库的 metadata 里而不在此暴露，刷新历史后调试面板会消失。
    query_route: QueryRouteRead | None = None

    @classmethod
    def from_orm(cls, message: Message) -> "MessageRead":
        """从 Message ORM 实体构建消息响应模型。

        【为什么按角色过滤引用】：
        只有 assistant 消息会产生引用（user / system 消息不会）。
        这里显式判断角色，避免用户消息因异常数据关联了引用而把脏数据透出到前端。

        【查询优化快照的来源与校验】：
        持久化时写在 metadata 的 query_route 键下，这里取出来交给 Pydantic 校验。
        若元数据缺失该键（如 user 消息、或本功能上线前写入的历史消息），
        则返回 None，不影响接口可用性。

        【前置条件】：
        调用方查询消息时必须已预加载 citations 关系
        （conversation_repo.list_messages 内部已使用 selectinload），
        否则在异步环境下访问 message.citations 会触发懒加载并抛出 MissingGreenlet。

        :param message: Message 实体实例
        :return: 消息响应模型
        """
        # 角色只判断一次并缓存：引用过滤与查询快照都依赖它，
        # 重复写 message.role == "assistant" 容易在改动时漏改其中一处
        is_assistant = message.role == "assistant"
        return cls(
            id=message.id,
            role=message.role,
            content=message.content,
            created_at=message.created_at,
            citations=(
                [CitationRead.from_orm(c) for c in message.citations]
                if is_assistant
                else []
            ),
            # 查询优化快照只挂在 assistant 消息上（user 消息从不写入该元数据）；
            # 解析交给 _parse_query_route，它内部已做完整防御，不会抛异常
            query_route=(
                _parse_query_route(message.extra_metadata) if is_assistant else None
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
