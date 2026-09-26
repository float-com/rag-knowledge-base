"""RAG 工作流图状态模型层。

【模块职责说明】：
1. 图工作流全局状态契约（Graph State Contract）：
   基于 LangGraph 范式声明图计算流水线的共享数据载体 `RAGState`，
   打通从初始提问、多轮历史装配、Query 改写、向量检索召回、拒答熔断、LLM 回答生成到最终持久化落库回写的全生命周期。
2. 增量状态合并策略（Partial Updates / total=False）：
   采用 `TypedDict(total=False)` 声明非强约束宽容契约，
   每个独立图节点（Node）仅需聚焦自身业务边界并返回当前产出的增量字段字典，
   交由 LangGraph 底层状态机自动执行增量更新与状态合并，避免各节点传递全量无关参数。
3. 领域对象与图状态解耦（Decoupled Pipeline Flow）：
   解耦各计算节点间的强类型调用依赖，使上下文装载（load_context）、检索（retrieve）、生成（generate）
   均面向统一状态进行读写，保障节点的高内聚与可插拔性。
"""

from typing import Literal, TypedDict
from uuid import UUID

# 引入数据库消息持久化实体（用于传递多轮会话记录）
from app.db.models import Message
# 引入检索层返回的不可变切片数据契约
from app.retrieval.vector_retriever import RetrievedChunk

# 查询优化策略类型别名（与前端 QueryRouteRead.route 契约严格对齐）：
#   original    原样检索，不做任何改写
#   rewrite     结合多轮历史改写成独立完整问句
#   hyde        先生成假设答案，再用假设答案去检索
#   multi_query 扩展为多条子查询，分别检索后合并
QueryRoute = Literal["original", "rewrite", "hyde", "multi_query"]


class RAGState(TypedDict, total=False):
    """
    RAG 工作流全局状态字典。

    total=False 表示所有字段均为可选（Optional），节点执行时只需返回其自身更新/追加的字段字典。
    """

    # --- 1. 工作流初始输入阶段 (Input) ---
    conversation_id: UUID       # 当前会话的唯一标识 UUID
    question: str              # 用户在输入框键入的原始提问内容
    # 【第 11 期新增】用户有效权限标签，由 ChatService 在【进图前】注入。
    #   - 含 "*" 时检索 SQL 不附加权限过滤（admin 视角）；
    #   - 评测路径固定注入 ["*"]，防止离线评测被权限拦住；
    #   - 【为什么必须在这里显式声明】RAGState 是 TypedDict(total=False)，
    #     未声明的键会被 LangGraph 【静默丢弃】—— 不声明就等于没传。
    permissions: list[str]

    # --- 2. 上下文加载节点 (load_context 节点产出) ---
    chat_history: list[Message] # 数据库中预加载的正序最近多轮对话历史列表

    # --- 3. 意图与 Query 改写节点 (normalize_query 节点产出) ---
    query: str                  # 检索查询词（基础版本与 question 相同；route_query 会按策略覆盖本字段）

    # --- 4. 查询优化策略路由节点 (route_query 节点产出) ---
    # 实际采用的优化策略；下游 retrieve 依据它决定检索行为，service 依据它下发 query_route 事件
    route: QueryRoute
    # 策略选型与检索控制之外，下列三个字段用于调试与前端展示：
    #   rewritten_query / hyde_answer 当前无节点读取，仅供前端调试面板与问题排查使用
    rewritten_query: str | None # rewrite 策略产出的独立完整问句
    hyde_answer: str | None     # hyde 策略产出的假设答案
    multi_queries: list[str] | None # multi_query 策略产出的多条子查询（同时是 retrieve 多路召回的输入）

    # --- 5. 知识检索与熔断判断节点 (retrieve 节点产出) ---
    retrieved_chunks: list[RetrievedChunk] # 向量数据库召回并完成相关度计算的分块列表
    # 是否触发知识库拒答（例如检索结果为空或相似度过低）。为 True 时在条件路由中直接跳过 generate 生成节点
    refused: bool

    # --- 6. Agentic RAG 循环 (plan_retrieval / observe_context 产出) ---
    # agent_steps 每一项形如：
    #   {round, action, reason, route, query, retrieved_count, top_score}
    # 由 plan_retrieval 追加「决策」字段、observe_context 回填「观察」字段，
    # 避免同一轮分两条记录（否则轮次与观测的对应关系要靠下标去猜，极易错位）。
    agent_steps: list[dict]
    # 当前轮次（从 1 开始）。observe_context 用它和 agent_max_rounds 比较来判断是否收敛；
    # 注意它必须每轮自增，否则「轮次用尽」这个出口永远不触发 → 死循环。
    retrieval_round: int
    # observe_context 判定本轮候选是否足够；True 时图走出循环进入 rerank
    #   【第 8 期语义变化】：此前 True 时图【直接 END】，本期改为【走向 rerank】——
    #   observe 只负责"这一轮还要不要再试"，"到底能不能回答"交给 judge_context 裁定。
    context_sufficient: bool

    # rerank 后基于 Top1 score 的拒答闸门
    #   False 时图走向 refuse 节点；True 时图走向 END。
    #   【与 context_sufficient 的区别】：前者是循环内部节奏（要不要再试），
    #   本字段是对外最终结论（能不能回答）—— 两个字段、两个判定时点。
    context_is_enough: bool

    # --- 7. 大模型回答生成节点 (generate 节点产出) ---
    answer: str                 # LLM 根据参考片段最终生成的回答内容（包含 [N] 引用标记）

    # --- 8. 持久化后置落库节点 (chat_service 落库后回写) ---
    user_message_id: UUID       # 写入数据库后生成的本轮用户提问 Message 唯一主键
    assistant_message_id: UUID  # 写入数据库后生成的本轮 AI 回复 Message 唯一主键

    # --- 9. 可观测性 (第 9 期) ---
    # LangSmith trace_id：未启用观测 / 取不到 run tree 时为 None。
    # 【为什么必须声明在状态里】LangGraph 会静默丢弃状态模式中未声明的键 ——
    #   不写在这里，服务层塞进来的 trace_id 会在 ainvoke 之后凭空消失，且不报任何错。
    # 【为什么由服务层写、节点不写】它来自服务层 stream_answer 的运行上下文，
    #   图内节点既看不到也拿不到，全链路所有节点只是"随着状态往后传"。
    trace_id: str | None