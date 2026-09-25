"""知识库问答编排服务（ChatService）。

【模块职责说明】：
1. 流水线总控与节点编排（Pipeline Orchestration）：
   作为 RAG 问答链路的业务总入口，把前序章节已经实现好的四类「零件」按顺序驱动起来：
   Repository（读写会话与消息）→ Workflow Nodes（装历史 / 改写查询 / 向量检索 / 流式生成）
   → Prompt（组装提示词）→ 引用落库（AnswerCitation）。
   本模块只负责「什么时候调用谁」，不实现检索算法、不拼提示词、不写 SQL。

2. 会话生命周期管理（Conversation Lifecycle）：
   提供非流式的会话与消息基础能力：新建会话、查询会话、拉取会话全部消息。
   这部分属于基础 CRUD，服务于前端会话侧边栏与历史回看场景。

3. 流式问答主链路（Streaming Answer Pipeline）：
   以 stream_answer 为入口，按「校验会话 → 开独立 session → 装历史 → 落库用户提问
   → 检索 → 流式生成 → 落库助手回复与引用」的顺序驱动全流程，
   并在每个阶段对应位置向外 yield 标准 SSE 事件，供路由层直接转发给前端。

4. 双会话策略（Dual Session Strategy）：
   - 非流式接口（创建会话 / 历史记录）使用 FastAPI 注入的**请求级 session**，
     生命周期与单次 HTTP 请求严格对齐；
   - 流式问答使用**独立 session**（与请求生命周期解耦），由 stream_answer 内部管理，
     因为 SSE 长连接期间不能长期占用请求级连接，避免撑爆连接池。

5. 引用序号契约（Citation Ordinal Contract）：
   引用编号必须与提示词中给大模型看到的「片段 N」编号严格一致（均从 1 开始），
   否则前端渲染的 [N] 角标会与真实来源错位，溯源功能失效。

6. 异常边界（Error Boundary）：
   流式响应一旦开始发送，HTTP 状态码就已经确定为 200，后续任何异常都无法再改用 4xx/5xx 表达。
   因此流式接口内部必须显式捕获异常并转为 error 事件下发，不能把异常直接抛给上层中间件。
"""

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from uuid import UUID

# 第 9 期：观测 SDK 的装饰器。本服务是"编排者"，内部是普通 Python 方法，
# SDK 自动捕获不到 —— 不装饰的话 trace 树会缺少【根节点】，所有子 span 变成散落的孤儿。
from langsmith import traceable

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
# 【第 9 期】可观测性工具：本章只落地 import 与类上的装饰器，
# 真正"取号 + 拼链接 + 下发落库"在第 8 章接入 —— 放在这里是为了让 import 与用法同处一处。
from app.core.observability import build_trace_url, get_current_trace_id
from app.db.models import AnswerCitation, Conversation, Message
from app.db.repositories.citation_repo import AnswerCitationRepository
from app.db.repositories.conversation_repo import ConversationRepository
from app.db.session import AsyncSessionLocal
from app.llm.answer_verifier import VerifyResult, get_answer_verifier
from app.llm.prompts import REFUSAL_ANSWER
from app.retrieval.vector_retriever import RetrievedChunk
from app.workflows.graph import get_rag_graph
from app.workflows.nodes import load_context, stream_generate
from app.workflows.rag_state import RAGState

# 初始化模块级业务日志记录器
logger = get_logger(__name__)


@dataclass(frozen=True)
class EvaluationAnswer:
    """评测专用领域模型：跑一遍完整 RAG 后拿到的一次性非流式结果快照。

    与 stream_answer 的三点差异：
    1. 沙箱隔离：不落 conversations / messages，跑几百条也不污染线上历史；
    2. 指标底座：除 answer 外完整暴露 chunks，供外层映射成
       RagasSample.retrieved_contexts，用于算召回率与忠实度；
    3. 归因画像：聚合路由 / Agent 轨迹 / 校验结果 / 耗时，
       让 Bad Case 能定位到"切片没召回、模型慢、还是触发拒答"。
    frozen=True 保证进入并发评测与统计环节后不会被无意篡改。
    """

    # --- 核心结果区 ---------------------------------------------------------
    # 最终答案文本（可能已被 verify 整段替换成拒答文案）。
    # 映射：RagasSample.answer → answer_relevancy / faithfulness 的入参。
    answer: str

    # 是否以"拒答"收场。来源三条：① plan_retrieval 判定 refuse（只置位）；
    # ② refuse 节点写文案；③ verify 校验失败（替换答案 + 强制置位）。
    # ①② 是"没找到依据"、③ 是"答了但不可信"，归因时修复动作完全不同。
    refused: bool

    # 实际召回并喂给模型的切片列表。
    # 映射：`[c.content for c in chunks]` → RagasSample.retrieved_contexts（是 content 不是 text）。
    # 拒答时照样有值（只清空 citations），RAGAS 仍能算上下文类指标。
    chunks: list[RetrievedChunk]

    # --- 决策链路与白盒轨迹区 -----------------------------------------------
    # 路由结果，固定 5 键（见 _build_query_route_payload）：route(original|rewrite|hyde|
    # multi_query) / query / rewritten_query / hyde_answer / multi_queries。
    # 单知识库，无 intent / target_kb；且路由由 LLM 决策，同一题两次跑可能不同。
    query_route: dict

    # 检索循环的逐轮「决策 + 观察」轨迹，每项固定形如（见 rag_state.py）：
    # {round, action, reason, route, query, retrieved_count, top_score}
    # （plan_retrieval 追加决策、observe_context 回填观察，同一轮只占一条记录）。
    # 排查用：retrieved_count 恒为 0 = 空转；round 逼近视 agent_max_rounds = 不收敛。
    agent_steps: list[dict]

    # 答案真实性校验结果（VerifyResult = verified + reason），即"回答能否由上下文支撑"。
    # 与敏感词 / 内容安全无关；拒答路径不校验，恒为 None。
    verify_result: VerifyResult | None

    # --- 可观测性与性能诊断区 -----------------------------------------------
    # 链路追踪 ID（本项目接 LangSmith）。未启用观测时为 None，
    # 且只在 @traceable 装饰的函数体内有效、函数返回即失效。
    trace_id: str | None

    # 端到端耗时（ms）。起算点是进入 answer_for_evaluation 那一刻，不含执行器调度开销。
    latency_ms: int

    # 首字耗时（TTFT，ms）。起算点与 latency_ms 是同一个 started_at，检索 / 规划 /
    # 重排都算在内 —— 测的是"用户体感首字时间"，不是"模型首 token 时间"
    # （实测 5172 ms，同期 latency_ms 7378 ms）。拒答或异常未调 LLM 时为 None。
    first_token_latency_ms: int | None

    # --- 异常与证据溯源区 ---------------------------------------------------
    # 【为什么这两个必须压在最后】dataclass 语法强制：带默认值的字段不能排在无默认值
    # 字段之前，否则抛 TypeError —— 所以 citations 别顺手往上挪。
    #
    # 未捕获异常的摘要（str(exc)，空则退化为类名；完整堆栈在 logger.exception）。
    # 不为 None 即基础设施故障：第 6 节标 failed，第 3 节按 is_error=True 归到 other。
    error_message: str | None = None

    # 引用角标列表（拒答时为空）。元素固定 9 键（见 _serialize_citation）：
    # ordinal / chunk_id / document_id / document_name / page_no / section_path /
    # score / quote / retrieval_meta。是 ordinal 不是 index，与 prompt 的
    # 「片段 N」一致、从 1 开始。这个形状就是第 3 节 compute_citation_hit 的入参。
    # 用 default_factory=list，避免所有实例共享同一个列表。
    citations: list[dict] = field(default_factory=list)


def _serialize_agent_steps(state: RAGState) -> list[dict]:
    """SSE / metadata 共用的 agent_steps 载荷格式。

    把每一条决策记录浅拷贝成新 dict 再下发，避免两种后果：
    ① 下游（前端或落库序列化）意外就地改动 state 里的原始记录；
    ② 后续节点继续追加字段时，与已发出的载荷共享同一份对象引用。
    """
    return [dict(step) for step in state.get("agent_steps", [])]


def _build_verify_payload(
    result: VerifyResult, *, replacement_answer: str | None
) -> dict:
    """verify_result SSE / metadata 共用载荷。

    replacement_answer 仅在 verified=False 时携带：前端按它整段替换流式出来的答案，
    与 PRD"verify 失败 → 拒答替换"语义对齐。

    【为什么用关键字参数（`*`）】：
    replacement_answer 是可选且语义敏感的参数（它决定前端是否整段改文案），
    强制关键字传参能杜绝"位置传错把 reason 当 replacement"这类事故。
    """
    payload: dict = {
        "verified": result.verified,
        # verified=True 时 reason 通常是空串，统一转成 None 下发：
        # 空串在 JSON 里没有信息量，null 更利于前端判断"没有理由可展示"。
        "reason": result.reason or None,
    }
    # 只有"校验不通过"才带替换文案 —— 通过时带一个 null 字段反而让前端多一次判断
    if not result.verified and replacement_answer is not None:
        payload["replacement_answer"] = replacement_answer
    return payload



def _build_retrieval_meta(chunk: RetrievedChunk) -> dict:
    """构建混合检索调试元数据。

    【为什么需要它】：
    混合检索把两条腿（向量 + 中文全文）的结果融合成一份，最终排序依据是 RRF 融合分。
    但"为什么这段被选中"无法从最终分数反推——它可能来自向量路第 1、也可能只来自
    关键词路第 8。把每条腿的排名与原始分都留下来，线上排查与前端调试面板才有据可依。

    【为什么 all-None 字段也要保留键】：
    显式下发 null 比省略字段更利于前端类型推断——字段存在且为 null 时，
    TS 能确定"这个键存在，只是这条路没召回它"；字段缺失时类型上仍是可选的，
    前端还得额外判断键是否存在。这与 query_route 载荷的处理方式一致。

    【精度取舍】：
    - 相似度 / ts_rank 保留 4 位：前端展示足够，避免浮点尾数抖动；
    - rrf_score 保留 6 位：因为它本身量级很小（k=60 时约 0.015~0.033），
      若只留 4 位，0.0163 与 0.0164 这类真实差距会被抹平，排序信息丢失。

    :param chunk: 融合后的检索切片
    :return: 可直接 JSON 序列化并落库（JSONB）的调试元数据字典
    """
    return {
        # 命中来源：list() 把 tuple 转成列表，JSON 里数组比元组更自然
        "sources": list(chunk.sources),
        "vector_rank": chunk.vector_rank,
        # 原始余弦相似度：仅向量路命中时有值，否则为 None
        "vector_score": (
            round(chunk.vector_score, 4) if chunk.vector_score is not None else None
        ),
        "keyword_rank": chunk.keyword_rank,
        # 原始 ts_rank：仅关键词路命中时有值
        "keyword_score": (
            round(chunk.keyword_score, 4) if chunk.keyword_score is not None else None
        ),
        # RRF 融合分：两位小数不够用，故保留 6 位
        "rrf_score": (
            round(chunk.rrf_score, 6) if chunk.rrf_score is not None else None
        ),
        # 精排成对相关性分（第 8 期新增）：保留 4 位。
        #   为什么不用 6 位：它的值域是 [0,1]（不是 RRF 那种 0.0x 量级），
        #   4 位已能区分 0.3508 与 0.3509 这类相邻结果，再多位只是浮点尾数抖动。
        "rerank_score": (
            round(chunk.rerank_score, 4) if chunk.rerank_score is not None else None
        ),
    }


def _serialize_citation(chunk: RetrievedChunk, ordinal: int) -> dict:
    """把检索切片转换为 citations SSE 事件载荷（与前端约定一致）。

    【为什么 ordinal 必须显式传入，而不是用列表下标现算】：
    ordinal 必须与 prompt 中交给大模型看到的「片段 N」编号完全一致（均从 1 开始）。
    prompt 侧是 `enumerate(chunks, start=1)` 生成的编号，模型据此在回答里写 [1][2]；
    前端则按本函数返回的 ordinal 渲染 [N] 角标。
    若这里改用下标自增，一旦后续引入重排序、过滤等会改变顺序的环节，
    引用序号就会与模型回答中的编号错位。因此把编号作为入参显式传递，杜绝顺序丢失。

    :param chunk: 检索召回的不可变切片视图
    :param ordinal: 该切片在本次回答中的引用序号（从 1 开始，与 prompt 编号一致）
    :return: 可直接 JSON 序列化并下发给前端的引用字典
    """
    return {
        # 引用序号：与模型回答中的 [N] 严格对应
        "ordinal": ordinal,
        # 主键统一转字符串，避免 UUID 对象在 JSON 序列化时报错
        "chunk_id": str(chunk.chunk_id),
        "document_id": str(chunk.document_id),
        # 快照冗余：文档名 / 页码 / 章节路径随引用一起下发，前端无需二次请求
        "document_name": chunk.document_name,
        "page_no": chunk.page_no,
        "section_path": chunk.section_path,
        # 相似度分数保留 4 位小数，避免浮点尾数在前端表格里抖动
        "score": round(chunk.score, 4),
        # 引用原文：供前端展开查看「这句话出自哪一段」
        "quote": chunk.content,
        # 检索调试元数据：与落库到 AnswerCitation.retrieval_meta 的是同一份结构，
        # 保证"实时展示"与"历史回看"两条路径看到的信息完全一致
        "retrieval_meta": _build_retrieval_meta(chunk),
    }


def _build_query_route_payload(state: RAGState) -> dict:
    """构造 query_route SSE 事件载荷，与 /metadata 共用同一份字段结构。

    【为什么始终携带 4 个可选字段（值为 None 也保留）】：
    前端按 `route` 决定渲染哪种调试面板，但 TypeScript 类型里这几个字段都是
    `T | null`——**显式下发 null 比省略字段更利于前端类型推断**：
    字段存在且为 null 时，TS 能确定"这个键存在，只是没有值"；
    字段缺失时，类型上仍是可选的，前端仍需额外判断键是否存在。

    :param state: 当前工作流状态（route / query / 各策略明细）
    :return: 可直接下发的 query_route 事件载荷
    """
    return {
        # 路由结果：缺省视为 original（未开启优化或未接入时的安全默认）
        "route": state.get("route", "original"),
        "query": state.get("query", ""),
        # 以下三项按策略产出，未产出时为 None；前端据此决定面板明细怎么渲染
        "rewritten_query": state.get("rewritten_query"),
        "hyde_answer": state.get("hyde_answer"),
        "multi_queries": state.get("multi_queries"),
    }


class ChatService:
    """知识库问答编排服务。

    【会话（session）使用约定】：
    - 非流式接口（创建会话 / 历史记录）使用 FastAPI 注入的**请求级 session**；
    - 流式问答使用**独立 session**（与请求生命周期解耦），由 stream_answer 内部管理。

    因此构造函数只接收一个请求级 session：非流式方法直接用 self.session；
    后续实现流式方法时，应在该方法内部自行创建独立 session，不要复用 self.session。
    """

    def __init__(self, session: AsyncSession) -> None:
        """注入请求级异步数据库会话。

        :param session: 由 FastAPI 依赖注入提供的请求生命周期 AsyncSession
        """
        self.session = session

    # =========================================================================
    # 非流式接口：会话基础 CRUD
    # =========================================================================

    async def create_conversation(self, title: str = "新对话") -> Conversation:
        """创建一个新会话。

        【事务边界】：
        仓储层只负责 add + flush，提交与刷新由本层控制。
        必须 refresh 一次，才能把数据库生成的 created_at / updated_at
        回填到实体上（这两个字段是 server_default，Python 侧实例化时并不存在），
        否则序列化响应时这两个字段为 None。
        """
        repo = ConversationRepository(self.session)
        conversation = await repo.create(title)
        # 提交事务：仓储层不主动 commit，由服务层统一控制事务边界
        await self.session.commit()
        # 刷新实体：回填数据库生成的时间戳等字段，避免响应里出现 None
        await self.session.refresh(conversation)
        return conversation

    async def list_conversations(
        self,
        page: int,
        page_size: int,
    ) -> tuple[list[tuple[Conversation, int]], int]:
        """分页拉取会话列表，每项带上消息条数。

        【为什么返回值是元组列表而不是"会话列表 + 另一个计数表"】：
        消息数是每个会话的附属属性，绑在同一个元组里能杜绝"两个列表顺序不一致"的隐患；
        分页接口还需要总条数，因此再返回一个 total。

        【事务边界】：纯读操作，不需要 commit。
        """
        repo = ConversationRepository(self.session)
        return await repo.list_page(page=page, page_size=page_size)

    async def delete_conversation(self, conversation_id: UUID) -> None:
        """删除会话（及其消息与引用，由外键级联清理）。

        【为什么不存在时要抛 404】：
        删除是幂等语义上"删了就是删了"，但接口契约上必须区分
        "删掉了一个真实存在的会话"与"你给了一个不存在的 id" ——
        后者说明前端状态与后端不一致，显式报错比静默成功更利于排查。

        【事务边界】：仓储层只 flush，由本层 commit 落地。
        """
        repo = ConversationRepository(self.session)
        deleted = await repo.delete(conversation_id)
        if not deleted:
            raise NotFoundError("会话不存在")
        await self.session.commit()

    async def get_conversation(self, conversation_id: UUID) -> Conversation:
        """按主键查询会话，不存在时抛出 404 业务异常。

        【异常契约】：
        统一把「查不到」翻译成 NotFoundError，由全局异常处理器转换为标准 404 响应，
        避免各调用方各自写 if None 判断与状态码映射。
        """
        repo = ConversationRepository(self.session)
        conversation = await repo.get(conversation_id)
        if conversation is None:
            raise NotFoundError("会话不存在")
        return conversation

    async def list_messages(self, conversation_id: UUID) -> list[Message]:
        """拉取指定会话的全部历史消息（按时间正序）。

        【为什么先校验会话再查消息】：
        先执行一次会话存在性校验，是为了让「会话不存在（404）」与
        「会话存在但还没有任何消息（200 + 空列表）」两种情况能在 API 层被明确区分。
        若直接查消息表，两种情况的返回都是空列表，调用方无法判断到底是哪一种。
        """
        # 1. 会话存在性校验：不存在会在此处直接抛 404，不会继续向下查消息
        await self.get_conversation(conversation_id)

        # 2. 复用 self.session 再实例化一次仓储（仓储是无状态的，仅持有会话引用）
        repo = ConversationRepository(self.session)

        # 3. 仓储内部按 created_at + id 稳定排序，保证同一毫秒写入的消息顺序不抖动
        messages = await repo.list_messages(conversation_id)

        return messages

    # =========================================================================
    # 流式问答主链路：按「校验 → 装历史 → 落库提问 → 检索 → 生成 → 落库回复」顺序驱动
    # =========================================================================

    # 【第 9 期】trace 树的根 span：本方法是整个问答链路的入口，
    #   不装饰的话下面所有子 span 会变成互不相连的孤儿，看不到"一次问答"这个整体。
    @traceable(name="ChatService.stream_answer", run_type="chain")
    async def stream_answer(
        self, conversation_id: UUID, question: str
    ) -> AsyncIterator[dict]:
        """**这是本模块最核心的一段**：流式问答主链路。

        【执行的六个阶段，每一步对应下方相应位置的 SSE 事件】：
        1. 校验会话存在性（复用非流式的 self.session，本阶段仍是短请求）
        2. 开启独立数据库会话（与请求生命周期解耦，长连接专用）
        2.1 【第 9 期】取本次回答的 trace_id —— 必须在函数体内取，见下方时序说明
        3. 装载上下文 → 图执行（标准化/路由/循环检索/精排/裁定）
        4. 落库并提交用户提问（必须在 load_context 之后，避免本轮提问混进历史）
        5. 下发路由 / 决策轨迹 → 回填引用 → 拒答分支或流式生成
        6. 答案校验（第 8 期新增）→ 落库助手回复与引用 → 收尾

        【事件协议（与前端约定）】：
        message_start → query_route → agent_steps → citations → token…
            → [verify_result] → message_end
        - **拒答路径不发 verify_result**（拒答本身已经是终态，再校验一次毫无意义）；
        - `verify_result.verified=False` 时携带 `replacement_answer`，前端按它**整段替换**
          流式出来的答案，与 PRD"校验失败 → 拒答替换"对齐；
        - 任何阶段出错则 `yield error` 并提前结束。
        - 【第 9 期】`message_start` 追加 `trace_id` / `trace_url` 两个字段，
          前端 `chatStream.ts` 的解析分支读的正是它们，因此**不新增事件类型**。

        【第 9 期 · trace_id 的时序约束（本方法最容易被改坏的一处）】：
        `get_current_trace_id()` 读的是"当前执行上下文"，**只在被 @traceable 装饰的函数体内有效**，
        函数返回之后上下文即被清掉 —— 所以在外面（例如路由层、图节点里）取一律是 None。
        与此同时，`message_start` 必须在**落库用户提问之后**才发（要带 user_message_id），
        而落库又必须早于图执行（否则 load_context 会把本轮提问当成历史读回来）。
        因此本方法的顺序被三重约束钉死，**不要把取号或 message_start 往上/往下挪**。

        【异常处理】：
        流式响应一旦开始返回，HTTP 状态码已固定为 200，后续异常无法再用 4xx/5xx 表达，
        因此必须在函数内部兜住异常并转成 error 事件下发，而不是向上抛出。

        :param conversation_id: 目标会话 ID（必须已存在）
        :param question: 用户本轮提问原文
        :return: 逐条 SSE 事件字典（含 event 与 data 两个键）的异步生成器
        """
        # 1. 会话存在性校验：复用请求级 session。
        #    此步骤在流式响应开始之前完成，若会话不存在会直接抛 404，由路由层正常返回 HTTP 错误。
        await self.get_conversation(conversation_id)

        # 2. 开启独立 session：SSE 长连接期间不能占用请求级连接，否则并发时极易耗尽连接池。
        #    async with 保证无论是正常结束还是异常退出，连接都会被归还。
        async with AsyncSessionLocal() as session:
            try:
                # 2.1 【第 9 期】取本次回答的 trace_id。
                #     【为什么必须写在这里】观测 SDK 的取号函数是从"当前执行上下文"里读的，
                #       而上下文只在本函数体内有效 —— 一旦函数返回就被清掉，外面再取必然是 None。
                #       本函数带着 @traceable 装饰器，所以进入函数体时根 span 已经建好。
                #     【未启用观测时】该函数直接返回 None，下面所有透出逻辑自然全部退化为"没有追踪信息"，
                #       不需要在这里写任何特判。
                trace_id = get_current_trace_id()

                state: RAGState = {
                    "conversation_id": conversation_id,
                    "question": question,
                    # 塞进状态后全链路可读（图内节点只透传、不产出）
                    "trace_id": trace_id,
                }

                # 3. 装载上下文（取历史消息）、查询标准化与策略路由。
                #    注意：这一步必须早于用户提问落库，否则 load_context 会把本轮提问也当成历史读回来。
                state.update(await load_context(state, session))

                # 3.1 图执行：加载上下文之后、检索之前。
                #     按最初设计，load_context 与 stream_generate 仍由 service 直接调用 ——
                #     前者需要 AsyncSession（唯一带 DB IO 的节点），后者要逐 token yield 给 SSE；
                #     两者都不适合放进图。图内只负责
                #     normalize_query → route_query → plan_retrieval → retrieve → observe_context
                #     这条带分支与循环的决策链路。
                #    【这不是"固定公式"】：从手写 await 三个节点收缩成一次 ainvoke，
                #     图里有几个节点、走了几轮，本层都不需要感知。
                final_state = await get_rag_graph().ainvoke(state)
                state.update(final_state)  # type: ignore[arg-type]

                # 4. 落库用户提问并 commit。
                #    此处单独提交一次，是为了让「用户提问」这条记录先于大模型调用持久化：
                #    即使后续 LLM 调用失败，用户问题也不会丢，便于前端重试与问题排查。
                await self._persist_user_message(state, session)

                # 5. 先把 message_start 发给前端，再进入 RAG 主链路。
                #    这样做让前端尽早拿到 user_message_id 用于消息占位，参考资料的耗时不会阻塞首屏。
                #    【第 9 期】顺带带上 trace_id / trace_url：
                #      前端 TraceIdPanel 读的正是 message_start 的这两个字段（chatStream.ts）。
                #      放在这里而不是新开一个事件，是为了不改动前端契约 —— 少一个事件就少一处不一致。
                #      trace_url 走 build_trace_url()：用户没配 URL 前缀时它返回 None，
                #      前端据此只显示"复制"按钮而不给跳转链接（None 是前端约定的"没有"）。
                yield {
                    "event": "message_start",
                    "data": {
                        "user_message_id": str(state["user_message_id"]),
                        "trace_id": trace_id,
                        "trace_url": build_trace_url(trace_id),
                    },
                }

                # 6. 下发策略路由事件，把路由结果推给前端调试面板。
                #    【为什么放在检索之前】：
                #    用户提问那一刻前端先收到 message_start，紧接着就能拿到路由结果并渲染面板；
                #    而检索需要先做一次向量化（约 1 秒），若等检索完再发，面板会白白空等。
                #    【为什么无论什么策略都发】：
                #    route=original 时前端 QueryRoutePanel 自身不渲染，
                #    因此后端无需特判，契约更简单（少一个分支就少一处可能不一致的地方）。
                yield {
                    "event": "query_route",
                    "data": _build_query_route_payload(state),
                }

                # 6.1 下发 Agentic 循环的决策轨迹，供前端渲染"每一轮检索做了什么"。
                #     放在 query_route 之后：前端先拿到策略面板，再逐轮展开决策链。
                yield {
                    "event": "agent_steps",
                    # 【为什么包一层对象】与 citations 的 {"citations": [...]} 保持同一风格。
                    # 成品前端 chatStream.ts 的解析是 `data.steps`，若这里直接发裸数组，
                    # data.steps 会是 undefined → 前端拿到空数组 → 【实时对话时面板不渲染】
                    # （历史回看不受影响：MessageRead.agent_steps 是顶层字段、形状本来就对）。
                    "data": {"steps": _serialize_agent_steps(state)},
                }

                # 7. 检索与观察已由第 3.1 步的图执行完成 —— 原先这里手写的
                #    `state.update(await retrieve(state))` 已被删除：
                #    检索（retrieve）与观测（observe_context）都是图内节点，
                #    在 ainvoke 时一并跑完，本层不再需要、也无法单独调用它们。
                #
                # 8. 引用回填：ordinal 用 enumerate(start=1) 生成，与 prompt 中给模型的「片段 N」编号一致
                #    【拒答路径不下发引用】：
                #    state["retrieved_chunks"] 可能还留着循环中间轮召回到的片段，
                #    但拒答本身就意味着"这些片段不足以作为依据"，
                #    因此拒答时不下发引用 —— 否则前端会展示出与实际结论相矛盾的"参考资料"。
                citations_payload = (
                    []
                    if state.get("refused")
                    else [
                        _serialize_citation(chunk, ordinal=i)
                        for i, chunk in enumerate(
                            state.get("retrieved_chunks", []), start=1
                        )
                    ]
                )
                yield {
                    "event": "citations",
                    "data": {"citations": citations_payload},
                }

                # 9. 生成分支：拒答时直接把预置文案作为唯一 token 下发，完全不调用大模型
                verify_result: VerifyResult | None = None
                if state.get("refused"):
                    yield {
                        "event": "token",
                        "data": {"delta": state["answer"]},
                    }
                else:
                    # ★ 流式节点只负责逐块产出，不写 state；答案的累积与回写由本编排层负责
                    answer_parts: list[str] = []
                    async for delta in stream_generate(state):
                        answer_parts.append(delta)
                        yield {"event": "token", "data": {"delta": delta}}
                    state["answer"] = "".join(answer_parts)

                    # 10. 答案校验（第 8 期新增）：必须等 token 流跑完、拿到【完整答案】才能校验，
                    #     所以放在这里而不是放进 LangGraph 图。
                    #     【为什么只在非拒答路径执行】：拒答路径的 answer 本来就是标准拒答文案，
                    #     再拿它去校验毫无意义（还会白花一次模型调用）。
                    if settings.verify_answer_enabled:
                        verify_result = await get_answer_verifier().verify(
                            # 用 query 而不是 question：校验的是"这段回答有没有答到
                            # 实际检索/生成所依据的那个问题上"，与生成阶段的口径一致。
                            question=state["query"],
                            answer=state["answer"],
                            chunks=list(state.get("retrieved_chunks", [])),
                        )
                        # 校验失败 → 整段替换为统一拒答文案并标记拒答，
                        # 让"落库内容 / 前端展示 / 拒答标志"三者保持一致
                        replacement = (
                            REFUSAL_ANSWER if not verify_result.verified else None
                        )
                        if not verify_result.verified:
                            # 严格按 PRD：替换成统一拒答文案 + 标 refused。
                            # 前端按 replacement_answer 覆盖正文，同时清空引用（下面 citations 已发过，
                            # 由前端依据 replaced 状态决定是否隐藏）。
                            state["answer"] = REFUSAL_ANSWER
                            state["refused"] = True
                        yield {
                            "event": "verify_result",
                            "data": _build_verify_payload(
                                verify_result, replacement_answer=replacement
                            ),
                        }

                # 11. 落库助手回复 + 引用记录（两者在同一事务内提交，保证原子性）
                #     注意必须在【校验与替换之后】调用，否则落库的是替换前的旧答案。
                await self._persist_assistant_message(
                    state, session, verify_result=verify_result
                )

                # 12. 收尾事件：下发 assistant_message_id 与拒答标志，前端据此结束流式状态并回填正式消息
                yield {
                    "event": "message_end",
                    "data": {
                        "message_id": str(state["assistant_message_id"]),
                        # 这里用替换后的真实值：verify 失败会把 refused 置 True，
                        # 前端据此把这条消息当拒答处理（与落库结果一致）。
                        "refused": bool(state.get("refused")),
                    },
                }

            except Exception as exc:
                # 记录完整堆栈供服务端排查（含 conversation_id 便于定位现场）
                logger.exception(
                    "chat stream failed: conversation_id=%s", conversation_id
                )

                # 说明：用户提问已在第 4 步单独提交，回滚不会把它删掉。
                # 这里回滚的是「助手回复 + 引用」这一批尚未提交的写入，
                # 保留用户消息便于前端重试与问题复现（前端可用它做"重发"）。
                await session.rollback()

                # 流式已开始，无法再改用 HTTP 错误码，只能以 error 事件下发
                yield {
                    "event": "error",
                    "data": {
                        "code": "chat_stream_failed",
                        "message": str(exc) or "问答处理失败",
                    },
                }

    # =========================================================================
    # 评测专用入口（第 10 期）：非流式跑一遍完整 RAG，只为离线评测取数
    # =========================================================================

    # 【可观测性设计 - 链路追踪根节点】：
    # 使用 LangSmith / LangChain 提供的 @traceable 装饰器将本函数注册为 Chain 根节点。
    # 为什么必须加？
    # 若不声明根节点，离线评测期间 RAG 内部并发调用的检索切片、Prompt 组装、LLM 生成等子调用
    # 会变成没有父节点的“孤儿 Span（Orphan Spans）”，散落在追踪看板各处；
    # 加上该装饰器后，每次评测都会生成一个树状 Trace，方便在 LangSmith 看板上一键排查 Bad Case 的完整执行轨迹。
    @traceable(name="ChatService.answer_for_evaluation", run_type="chain")
    async def answer_for_evaluation(self, question: str) -> EvaluationAnswer:
        """跑一遍完整 RAG 拿非流式结果，用于离线评测。

        【与生产线上接口 stream_answer 的核心差异】：
        1. 【评测沙箱隔离】：不创建 conversation 实体，不向数据库写入 user/assistant 消息，
           保证几百上千次离线自动化跑批绝不污染线上生产的对话历史表。
        2. 【题目完全正交】：强制注入 `chat_history: []`。生产环境有多轮对话改写（Contextualize），
           离线评测集每道题均独立计算指标，清空历史能杜绝上一题的对话记忆污染下一题。
        3. 【校验口径对齐】：等待生成流全部聚合为完整文本后，统一执行 verify_answer 事实校验，
           校验失败同样重放拒答逻辑，确保评测统计的指标口径与线上完全一致。
        4. 【无中断容错】：单个测试 Case 崩溃不向上 raise 异常打断整个评测批次，
           而是将异常原因收敛至 error_message，交由外部调度引擎统计失败率。

        :param question: 评测集中该用例的原始用户提问
        :return: 非流式评测快照实体（EvaluationAnswer），供下游计算 RAGAS 指标及归因画像
        """
        # -------------------------------------------------------------------------
        # 步骤 1：启动基准计时与链路追踪上下文初始化
        # -------------------------------------------------------------------------
        # 【为什么用 time.perf_counter() 而不用 time.time()？】
        # 1. 避免时钟回拨与负数隐患（Monotonic Clock 单调递增性）：
        #    - time.time() 读取的是操作系统的“挂钟时间”（Wall Clock Time）。生产服务器后台常驻 NTP 自动校时
        #      进程或夏令时/闰秒调整，一旦系统时间在此期间被往回校准了 1~2 秒，耗时算式 (time.time() - started_at)
        #      就会诡异地算出【负数】（如 -1800ms）或几十秒的虚假暴增，导致评测报表数据污染。
        #    - time.perf_counter() 读取的是 CPU 硬件级的单调时钟，物理上严格单调递增、永不倒流，计算耗时差值绝对安全。
        # 2. 高分辨率（适配微秒/毫秒级性能测量）：
        #    - 大模型评测对首字延迟（TTFT，往往在几十到几百毫秒）以及网络 I/O 耗时非常敏感，
        #      perf_counter 以【纳秒为单位】返回，实际分辨率取决于平台（通常亚微秒级），远优于普通时间戳函数。
        started_at = time.perf_counter()
        trace_id = get_current_trace_id()

        # 构造 LangGraph 状态图的初始上下文（RAGState）：
        state: RAGState = {
            # 【为什么用 UUID(int=0) 占位？】：
            # 1. 类型约束：RAGState 状态模式强类型要求 conversation_id 必须是 UUID 类型，不能直接传 None；
            # 2. 数据库安全：评测分支不执行落盘写库操作，使用 00000000-0000-0000-0000-000000000000 占位；
            # 3. 流量染色：日志检索或链路系统看到全 0 UUID，能立刻断定为离线评测或自动化巡检流量。
            "conversation_id": UUID(int=0),
            "question": question,
            # 显式置空多轮历史，禁用多轮指代消解与改写，保证评测用例的单轮独立性
            "chat_history": [],
            "trace_id": trace_id,
        }

        try:
            # ---------------------------------------------------------------------
            # 步骤 2：执行 LangGraph 核心图拓扑（意图识别 -> 检索 -> 重排 -> 路由）
            # ---------------------------------------------------------------------
            # 1. 驱动状态图异步执行：
            #    通过 get_rag_graph().ainvoke(state) 触发编译好的 LangGraph 图流转。
            #    该图内部黑盒完成了：意图分类 -> 关键词/向量混合检索 -> RRF融合 -> 重排打分 -> 拒答裁定。
            #    此处图执行停止在流式生成之前，返回生成所需的上下文终态（final_state）。
            final_state = await get_rag_graph().ainvoke(state)

            # 2. 状态增量合并与静态类型检查抑制：
            #    - 将图流转产出的新字段（如 retrieved_chunks、agent_steps、refused）回填至原 state。
            #    - # type: ignore[arg-type] 的原因：LangGraph 的 ainvoke() 返回的是 dict[str, Any]，
            #      而 TypedDict.update() 期望同类型映射，MyPy 据此报 arg-type。
            state.update(final_state)  # type: ignore[arg-type]

            # 3. 后置校验对象提前占位初始化：
            #    - 只有当流程未被前置拒答、且开启了 verify_answer_enabled 开关时，才会真正调用校验器生成该对象；
            #    - 前置拒答路径或未开启校验时，该变量保持为 None，下游 EvaluationAnswer 据此如实记录“未执行校验”。
            verify_result: VerifyResult | None = None

            # 4. 首字延迟（TTFT）指标占位初始化：
            #    - 该字段用于记录从发起调用到大模型吐出首个 token 的端到端毫秒数；
            #    - 若流程在前置路由阶段直接被拒答（无需调 LLM 生成），或调用发生异常中断，
            #      则此值保持为 None，避免向离线评测报表中注入虚假的首字耗时。
            first_token_latency_ms: int | None = None

            # ---------------------------------------------------------------------
            # 步骤 3：答案生成与流式首字耗时（TTFT）捕获
            # ---------------------------------------------------------------------
            # 检查前置 LangGraph 执行阶段是否已经判定为拒答（如检索无相关切片、上下文判据不足）
            if state.get("refused"):
                # 【拒答快速通道】：
                # 前置规划节点已将标准拒答文案写入 state["answer"]，直接复用该文案；
                # 跳过大模型调用，节省昂贵的 Token 消耗与推理时间；
                # 此时 first_token_latency_ms 保持为 None，如实反映“未发起 LLM 生成”。
                answer = state["answer"]
            else:
                # 【正常生成通道：流式聚合缓冲区】
                # 针对生产接口采用 SSE 逐字下发，而离线评测需拿到完整文本输入给下游指标评测器（RAGAS）；
                # 初始化内存列表作为收集器，避免字符串频繁 += 拼接引发的大量内存重分配。
                parts: list[str] = []

                # 异步遍历大模型推理生成的流式事件生成器（AsyncIterator[str]）：
                async for delta in stream_generate(state):
                    # 捕获 Time To First Token（TTFT，首字生成耗时）：
                    # 利用 if None 守卫逻辑，仅在接收到首个文本切片（Token）的瞬间触发一次计算；
                    # 记录从进入 answer_for_evaluation 开始，到看到第一个字跳出的端到端真实体感时延（毫秒）。
                    if first_token_latency_ms is None:
                        first_token_latency_ms = int(
                            (time.perf_counter() - started_at) * 1000
                            # 算式原理解析：
                            # 1. time.perf_counter() 读取当前纳秒级单调时钟，减去起点 started_at 得到流逝的「秒数（float）」；
                            # 2. 乘以 1000 将「秒（s）」换算为「毫秒（ms）」；
                            # 3. 外层 int(...) 截断取整，得到标准的整数毫秒值，记录从进入评测到吐出首个 Token 的真实端到端体感耗时。
                        )
                    # 将当前流式增量追加至缓冲区
                    parts.append(delta)

                # 将分散的 token 切片高效拼接为完整回答字符串
                answer = "".join(parts)
                # 将最终拼接好的生成结果同步回写至图状态上下文，供后续校验与落库逻辑使用
                state["answer"] = answer

                # -----------------------------------------------------------------
                # 步骤 4：后置事实校验与线上护栏行为对齐
                # -----------------------------------------------------------------
                # 读取全局配置开关：判断当前环境是否启用了生成后的事实一致性（反幻觉）校验
                if settings.verify_answer_enabled:
                    # 执行事实核验流水线（异步调用专用校验器）：
                    # 1. 传入原始提问与完整聚合后的生成回答；
                    # 2. list(state.get("retrieved_chunks", []))：防御性读取切片，
                    #    并包一层 list 复制【列表容器】，防止校验器对列表增删而影响 state；
                    #    注意元素仍是同一批 RetrievedChunk 引用（它本身是 frozen dataclass，天然不可变）。
                    # 3. 校验器内部比对 answer 中的事实断言是否均能由 chunks 充分推导支撑。
                    verify_result = await get_answer_verifier().verify(
                        question,
                        answer,
                        chunks=list(state.get("retrieved_chunks", [])),
                    )

                    # 检查核验结果：若 verified 为 False，说明模型产生了无法由知识库佐证的严重事实幻觉
                    if not verify_result.verified:
                        # 【与线上 verify 失败后的处理完全对齐】：
                        # 1. 废弃并覆盖原有不可信回答，强行替换为统一标准拒答文案 REFUSAL_ANSWER；
                        answer = REFUSAL_ANSWER

                        # 2. 同步回写状态字典，确保后续所有模块读到的都是最终安全文案；
                        state["answer"] = answer

                        # 3. 将状态显式标记为拒答（refused=True）：
                        #    - 触发后续步骤清空引用角标（citations 置空），避免出现“拒绝回答却给出了参考资料”的矛盾展示；
                        #    - 固化进 EvaluationAnswer 产物，供下游评测报表将其归因为“幻觉拦截”典型用例。
                        state["refused"] = True

            # ---------------------------------------------------------------------
            # 步骤 5：切片序列化、角标引用提取与快照装配
            # ---------------------------------------------------------------------
            # 1. 安全提取召回切片：
            #    - 使用 .get(..., []) 规避键缺失风险；
            #    - 外层包 list(...) 复制一份【列表容器】，防止上游对列表增删而影响 state；
            #      元素仍是同一批 RetrievedChunk 引用（它本身是 frozen dataclass，天然不可变）。
            chunks = list(state.get("retrieved_chunks", []))

            # 2. 状态值布尔强转与归一化：
            #    - 将可能为 None 的 state.get("refused") 强制规整为标量布尔值（True/False），
            #      严格契合 EvaluationAnswer 强类型契约。
            refused = bool(state.get("refused"))

            # 3. 引用溯源角标条件装配（拒答业务对齐）：
            #    - 若 refused=True（前置拒答或事实校验未通过）：强制清空引用（置为 []），
            #      杜绝“回答内容拒绝回答，但下方却展示参考切片”的业务逻辑自相矛盾；
            #    - 若正常回答：使用 enumerate(chunks, 1) 从 1 起始编号，确保生成的角标 [1],[2]
            #      与大模型 Prompt 提示词中看到的【片段 N】完全一致，逐个调用 _serialize_citation 序列化。
            citations = (
                []
                if refused
                else [
                    _serialize_citation(c, ordinal=i)
                    for i, c in enumerate(chunks, 1)
                ]
            )

            # 4. 固化并交付不可变评测领域模型实体：
            #    将问答结果、决策轨迹、耗时画像与追踪信息打包，作为供下游 RAGAS 打分和 Bad Case 归因的不可变快照。
            return EvaluationAnswer(
                # 核心业务文本（用于 RAGAS 计算 answer_relevancy 与 faithfulness）
                answer=answer,
                # 拒答状态标记（前置拒答、或事实校验未通过，均为 True）
                refused=refused,
                # 原始切片实体列表（供外层提取 c.content 映射为 RagasSample.retrieved_contexts）
                chunks=chunks,
                # 意图路由快照（包含改写、HyDE 等分支细节，用于排查路由错位）
                query_route=_build_query_route_payload(state),
                # Agent 多轮检索轨迹（包含各轮 action / reason / retrieved_count，用于排查多跳空转）
                agent_steps=_serialize_agent_steps(state),
                # 后置反幻觉校验实体（包含是否通过 verified 及判定理由 reason）
                verify_result=verify_result,
                # LangSmith 链路追踪根节点 ID（未启用时为 None，用于一键跳转查看分布式调用树）
                trace_id=trace_id,
                # 端到端全链路总耗时（毫秒整数）：从函数入口计算至装配交付的完整时长
                latency_ms=int((time.perf_counter() - started_at) * 1000),
                # 首字生成时延（TTFT，毫秒整数）：若前置拒答或异常未触发 LLM，则自然保持为 None
                first_token_latency_ms=first_token_latency_ms,
                # 结构化引用角标列表
                citations=citations,
            )

        except Exception as exc:
            # ---------------------------------------------------------------------
            # 步骤 6：异常隔离护栏（Fail-Safe 故障隔离设计）
            # ---------------------------------------------------------------------
            # 1. 打印带堆栈的现场日志：
            #    - 使用 logger.exception 自动挂载完整 Traceback 堆栈信息；
            #    - question=%r：利用 repr 格式化输出带转义的原题，防止题干自带换行污染日志排版。
            logger.exception("Evaluation answer failed: question=%r", question)

            # 2. 构造并交付降级快照（禁止向上抛出异常，防止评测批处理任务链直接熔断）：
            return EvaluationAnswer(
                # 异常未产出有效回复，置空字符，避免下游误当正常回答处理
                answer="",
                # 异常中断不属于合规业务拒答，标记为 False
                refused=False,
                # 未完成检索或检索产物失效，置空列表，防止产生脏切片
                chunks=[],
                # 【保全现场】：即使崩溃，也保留崩溃前 state 已生成的路由决策，便于定位哪条链路翻车
                query_route=_build_query_route_payload(state),
                # 【保全现场】：保留崩溃前已走过的 Agent 思考轨迹，查明停在第几轮
                agent_steps=_serialize_agent_steps(state),
                # 链路未正常走完，无反幻觉核验结果，置为 None
                verify_result=None,
                # 保留链路追踪根节点 ID，支持在 LangSmith 上溯源崩溃点
                trace_id=trace_id,
                # 记录从进入到崩溃的总时长（毫秒）：辅助判断是瞬间闪退还是超时挂起
                latency_ms=int((time.perf_counter() - started_at) * 1000),
                # 流程中断未成功调用 LLM 吐字，首字延迟恒为 None
                first_token_latency_ms=None,
                # 【双重防御提取异常原因】：
                # - 优先取 str(exc).strip() 异常描述（如 "Connection timed out"）；
                # - 若某些异常未附带描述文本导致空串，自动短路回退为异常类名（如 "TimeoutError"），
                #   确保下游执行器能拿到非空的故障定位信息，将本 case 标为 Failed。
                error_message=str(exc).strip() or exc.__class__.__name__,
            )

    # =========================================================================
    # 内部方法：流式链路的两段落库逻辑
    # =========================================================================

    async def _persist_user_message(
        self,
        state: RAGState,
        session: AsyncSession,
    ) -> None:
        """流式开始前先把用户提问落库并 commit；首次提问时顺手把会话标题改成问题前 30 字。

        【为什么必须单独 commit】：
        让用户提问先于大模型调用持久化。若后续检索或 LLM 调用失败，
        用户问题依然留在数据库里，前端可据此做"重发"，也便于排查问题现场。

        【调用时机约束】：
        必须在 load_context 之后 调用——load_context 读到的「历史消息」不应包含本轮提问。

        【为什么标题更新放在保存用户消息的同一事务里】：
        合并提交可以避免"新建会话已经发了第一条，但标题还停在「新对话」"的中间态
        —— 若分成两次 commit，在两次提交之间刷新侧栏，用户就会看到"新对话"。
        """
        repo = ConversationRepository(session)

        # 判断是否首次提问：会话里一条消息都没有，说明这是第一轮。
        # 【为什么用 count 而不是复用 list_messages】：只需要一个布尔信号，
        # 不必把整批消息实体查出来（轻量查询）。
        if await repo.count_messages(state["conversation_id"]) == 0:
            # 用问题原文做标题（仓储内部已做了 strip / 空值防御 / 只改默认标题的判断）
            await repo.update_title_if_default(
                state["conversation_id"], state["question"]
            )

        user_msg = ConversationRepository.make_user_message(
            state["conversation_id"], content=state["question"]
        )
        await repo.add_messages([user_msg])
        # 一次 commit 同时落地"标题更新 + 用户消息"，保证两者原子
        await session.commit()
        # 回写主键：供上层组装 message_start 事件下发前端
        state["user_message_id"] = user_msg.id

    async def _persist_assistant_message(
        self,
        state: RAGState,
        session: AsyncSession,
        *,
        verify_result: VerifyResult | None = None,
    ) -> None:
        """流式生成结束后落库助手回复消息及其引用，用单事务保证两者原子。

        【为什么两者必须同一事务】：
        助手消息与引用记录是"一次回答"的两个组成部分。
        若分开提交，一旦引用写入失败，就会出现"有回答但点不开引用"的半成品数据，
        且无法自动修复（无法判断哪些回答缺引用）。

        【拒答时不写引用】：
        拒答路径没有可溯源的原文依据，因此跳过 AnswerCitation 写入，
        只落一条带 refused 标记的助手消息。

        【verify_result 为什么默认 None 且用关键字传参】：
        拒答路径根本不会做校验（answer 本就是拒答文案），因此允许不传；
        用 `*` 强制关键字传参，避免与 RAGState / session 位置参数混淆。
        """
        conv_repo = ConversationRepository(session)
        citation_repo = AnswerCitationRepository(session)

        # 元数据先攒成变量，最后统一传给 make_assistant_message ——
        # 这样"要落哪些键"一眼可见（比在构造函数里内联一个多层 dict 更好审）
        extra_metadata: dict = {
            # 拒答标志：历史回看时无需再推断
            "refused": bool(state.get("refused")),
            "query_route": _build_query_route_payload(state),
            # Agentic 循环的决策轨迹：与 agent_steps SSE 事件共用同一份序列化函数，
            # 保证"实时展示"与"历史回看"看到的是同一条链（与 query_route 同一原则）。
            "agent_steps": _serialize_agent_steps(state),
            # 【第 9 期】LangSmith 追踪标识落库：刷新页面 / 翻历史时前端仍能展示与跳转。
            #   【只存 trace_id，不存 trace_url】——
            #   第 9 章的响应模型是"拿落库的 trace_id、按【当前】配置现拼跳转链接"。
            #   若这里也存一份 URL，它就成了永不读取的死数据，而且换 LangSmith 工作区
            #   或改 URL 格式后，历史里那份旧链接会与新规则不一致。
            #   【为什么不在这里把 URL 一并算好】拼接规则属于表现层关切，
            #   放在 API 模型层能在每次响应时反映最新配置。
            "trace_id": state.get("trace_id"),
        }
        if verify_result is not None:
            # verify_result 复用 SSE 的载荷格式，但 metadata【不需要】replacement_answer：
            # 它是"流式 UI 的特殊需求"（前端要拿它整段改文案），
            # 而落库的 answer 已经是替换后的最终文本，历史回看时再带一份重复文案没有意义。
            extra_metadata["verify_result"] = _build_verify_payload(
                verify_result, replacement_answer=None
            )

        assistant_msg = ConversationRepository.make_assistant_message(
            state["conversation_id"],
            content=state["answer"],
            # 把 query 路由结果持久化到 metadata 字段，刷新历史时前端调试面板还能继续展示。
            # 注意：这里复用 _build_query_route_payload，与 SSE 事件共用同一份字段结构，
            # 避免"实时展示"与"历史回看"两条路径的载荷格式各自演化而不一致。
            extra_metadata=extra_metadata,
        )
        await conv_repo.add_messages([assistant_msg])

        if not state.get("refused"):
            # ordinal 同样从 1 开始，与 prompt 编号、citations 事件载荷三者严格对齐；
            # 快照冗余 document_name / page_no / quote，使原文被删除后引用仍可展示。
            citations = [
                AnswerCitation(
                    message_id=assistant_msg.id,
                    ordinal=ordinal,
                    document_id=chunk.document_id,
                    chunk_id=chunk.chunk_id,
                    document_name=chunk.document_name,
                    page_no=chunk.page_no,
                    quote=chunk.content,
                    # 检索调试元数据落库（JSONB 列，迁移 8c44f95568ad 已建）：
                    # 与 citations 事件里下发给前端的是同一份结构，保证实时展示与
                    # 历史回看看到的信息一致。该列可空，因此第 4~5 期的历史引用
                    # 读出来是 NULL——它如实表示"当时系统还没有这个能力"。
                    retrieval_meta=_build_retrieval_meta(chunk),
                )
                for ordinal, chunk in enumerate(
                    state.get("retrieved_chunks", []), start=1
                )
            ]
            await citation_repo.bulk_add(citations)

        # 单次提交：助手消息 + 引用一起落盘，或一起回滚
        await session.commit()
        state["assistant_message_id"] = assistant_msg.id
