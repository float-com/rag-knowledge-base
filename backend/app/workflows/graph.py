"""RAG 检索子图的装配层（LangGraph）。

【模块职责】：
把 `app.workflows.nodes` 下各独立节点用 `StateGraph` 编排成一张**可调用的图**，
并对外暴露编译产物（模块加载时编译一次，请求里直接复用）。

【图的边界（第 7 期最关键的架构决定）】：
图内**只放有分支与循环的决策节点**：
    normalize_query → route_query → plan_retrieval → retrieve → observe_context

刻意**不放**两个节点：
- `load_context`：唯一带数据库 IO、需要 AsyncSession 的节点。
  把 session 透传进图意味着所有节点签名都得多一个参数，
  或要在图配置里塞一个全局 session —— 两种方案都会污染节点纯度。
- `stream_generate`：要逐 token yield 给 SSE，而 LangGraph 的 ainvoke
  更适合拿到一次完整图执行后的最终结果。强行把流式塞进节点要么破坏
  SSE 实时性、要么得改走 astream_events，都不如在 service 里直接 await 干净。

**也就是说**：本图只负责「要不要检索、怎么检索、检索够不够、是否继续检索」，
上下文加载与最终流式回答仍留在业务服务层。

【两个条件边（图的两处分支）】：
1. `plan_retrieval` 之后：planner 说 refuse 就直接结束（省掉一次无意义检索）；
2. `observe_context` 之后：三个终止条件任一满足就结束，否则回 `plan_retrieval` 继续循环。
"""

# 引入 LangGraph 的三个核心构件
from langgraph.graph import END, START, StateGraph

# 引入全局系统配置单例（读 agent_loop_enabled / agent_max_rounds）
from app.core.config import settings
# 引入统一日志工厂
from app.core.logging import get_logger
# 从节点包门面导入图内要用到的五个节点（不直接引用深层子模块）
from app.workflows.nodes import (
    normalize_query,
    observe_context,
    plan_retrieval,
    retrieve,
    route_query,
)
# 引入图共享状态契约
from app.workflows.rag_state import RAGState

logger = get_logger(__name__)


def _after_plan(state: RAGState) -> str:
    """planner 决策为 refuse 时直接结束，避免再做无意义的检索。"""
    # agent_steps 里最后一条就是本轮 plan_retrieval 刚追加的决策记录。
    # 判空是因为：理论上进入本分支时一定已有记录，但条件函数必须容错
    # （若为空就当作"没有拒答意图"，继续走检索）。
    if state.get("agent_steps"):
        last_action = state["agent_steps"][-1].get("action")
        # 用 .get 取 action：记录是 dict，字段可能缺失，直接下标会 KeyError
        if last_action == "refuse":
            return "end"
    # 默认继续检索
    return "retrieve"


def _after_observe(state: RAGState) -> str:
    """observe 后决定继续循环还是结束。

    结束条件（任一即停）：
    - 关闭了 agent loop（退化为单轮）
    - 本轮已足够（context_sufficient=True）
    - 达到 agent_max_rounds 上限
    """
    # ① 总开关关闭：整个图退化成单轮，跑一轮就出图
    if not settings.agent_loop_enabled:
        return "end"
    # ② 本轮召回质量已达标：收敛出图
    if state.get("context_sufficient"):
        return "end"
    # ③ 轮次用尽：必须收敛（否则会无限循环）
    #    用 .get(..., 0) 兜底：首轮进入本函数时该字段可能还没被写入
    if state.get("retrieval_round", 0) >= settings.agent_max_rounds:
        return "end"
    # 三个终止条件都不满足 → 回决策节点，让 planner 决定如何重试
    return "plan"


def _build_graph():
    """装配并编译检索子图。

    :return: 编译后的图（可调用对象）
    """
    # 1. 声明图的状态类型为 RAGState（TypedDict, total=False）
    builder = StateGraph(RAGState)

    # 2. 注册五个节点。
    #    节点是「拿 state、返回 partial state」的函数，LangGraph 会把返回值
    #    增量合并进状态 —— 因此各节点只需返回自己产出的那几个字段。
    builder.add_node("normalize_query", normalize_query)
    builder.add_node("route_query", route_query)
    builder.add_node("plan_retrieval", plan_retrieval)
    builder.add_node("retrieve", retrieve)
    builder.add_node("observe_context", observe_context)

    # 3. 普通边：无分支的直线部分
    builder.add_edge(START, "normalize_query")
    builder.add_edge("normalize_query", "route_query")
    builder.add_edge("route_query", "plan_retrieval")
    builder.add_edge("retrieve", "observe_context")

    # 4. 条件边一：plan_retrieval 之后
    #    把条件函数的返回值（"retrieve" / "end"）映射到真实节点名 / END
    builder.add_conditional_edges(
        "plan_retrieval",
        _after_plan,
        {"retrieve": "retrieve", "end": END},
    )

    # 5. 条件边二：observe_context 之后 —— 这条回边就是整个循环的"闭环"
    builder.add_conditional_edges(
        "observe_context",
        _after_observe,
        {"plan": "plan_retrieval", "end": END},
    )

    # 6. 编译：产出可调用对象。
    #    编译期会校验"每条边都有去处"，因此拓扑写错会在这里直接暴露。
    return builder.compile()


# 模块加载时编译一次；编译产物无状态，请求里直接复用，无额外构造成本。
_rag_graph = _build_graph()


def get_rag_graph():
    """对外暴露已编译好的子图：模块加载时一次编译，请求里直接复用。"""
    return _rag_graph
