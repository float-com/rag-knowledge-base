"""RAG 检索子图的装配层（LangGraph）。

【模块职责】：
把 `app.workflows.nodes` 下各独立节点用 `StateGraph` 编排成一张**可调用的图**，
并对外暴露编译产物（模块加载时编译一次，请求里直接复用）。

【图的边界（第 7 期定的架构决定，第 8 期沿用）】：
图内**只放有分支与循环的决策节点**（第 8 期已扩到 8 个）：
    normalize_query → route_query → plan_retrieval → retrieve → observe_context
    → rerank → judge_context →（或 refuse）→ END

刻意**不放**两个节点：
- `load_context`：唯一带数据库 IO、需要 AsyncSession 的节点。
  把 session 透传进图意味着所有节点签名都得多一个参数，
  或要在图配置里塞一个全局 session —— 两种方案都会污染节点纯度。
- `stream_generate`：要逐 token yield 给 SSE，而 LangGraph 的 ainvoke
  更适合拿到一次完整图执行后的最终结果。强行把流式塞进节点要么破坏
  SSE 实时性、要么得改走 astream_events，都不如在 service 里直接 await 干净。
  （第 8 期新增的 `AnswerVerifier` 同理留在服务层：它要等流式结束、拿到完整答案才能校验。）

**也就是说**：本图只负责「要不要检索、怎么检索、检索够不够、是否继续检索、够不够回答」，
上下文加载、最终流式回答与答案校验仍留在业务服务层。

【三个条件边（图的三处分支）】：
1. `plan_retrieval` 之后：planner 说 refuse → 走 `refuse` 节点统一写文案（跳过检索与精排）；
2. `observe_context` 之后：三个终止条件任一满足 → 退出循环进 `rerank`（**不再直接 END**），
   否则回 `plan_retrieval` 继续循环；
3. `judge_context` 之后：**最终闸门** —— 上下文足够 → END，不足 → `refuse` 节点。

【为什么退出循环后还要走 rerank → judge_context，而不是直接 END】：
```text
"这一轮召回不够"（observe 的判断）与"最终能不能回答"（judge 的判断）是两件事：
- 前者决定【要不要再试】，属于循环节奏；
- 后者是【对外结论】，必须由统一的闸门把关。
若 observe 一判不够就 END，拒答判定就又分裂成两套口径了。
```
"""

# 引入 LangGraph 的三个核心构件
from langgraph.graph import END, START, StateGraph

# 引入全局系统配置单例（读 agent_loop_enabled / agent_max_rounds）
from app.core.config import settings
# 引入统一日志工厂
from app.core.logging import get_logger
# 从节点包门面导入图内要用到的八个节点（不直接引用深层子模块）
from app.workflows.nodes import (
    judge_context,
    normalize_query,
    observe_context,
    plan_retrieval,
    refuse,
    rerank,
    retrieve,
    route_query,
)
# 引入图共享状态契约
from app.workflows.rag_state import RAGState

logger = get_logger(__name__)


def _after_plan(state: RAGState) -> str:
    """planner 决策为 refuse 时直接走 refuse 节点统一拒答文案，跳过后续检索 / 精排。

    【第 8 期变化】：此前这里返回 "end"（直接结束图），本期改为返回 "refuse" ——
    文案不再由 plan_retrieval 自己写，而是交给专门的 refuse 节点，实现"拒答出口唯一"。
    """
    # agent_steps 里最后一条就是本轮 plan_retrieval 刚追加的决策记录。
    # 判空是因为：理论上进入本分支时一定已有记录，但条件函数必须容错
    # （若为空就当作"没有拒答意图"，继续走检索）。
    if state.get("agent_steps"):
        last_action = state["agent_steps"][-1].get("action")
        # 用 .get 取 action：记录是 dict，字段可能缺失，直接下标会 KeyError
        if last_action == "refuse":
            return "refuse"
    # 默认继续检索
    return "retrieve"


def _after_observe(state: RAGState) -> str:
    """observe 后决定继续循环还是结束循环交给 rerank 精排。

    退出循环条件（任一即停）：
    - 关闭了 agent loop（退化为单轮）
    - 本轮已足够（context_sufficient=True）
    - 达到 agent_max_rounds 上限

    【退出循环后统一进 rerank，不再直接 END】：
    哪怕本轮 sufficient=False，也走完 rerank → judge_context，
    让 judge_context 一处统一把关拒答闸门。
    否则"这一轮不够"与"最终不够"会变成两套判定，口径容易漂移。
    """
    # ① 总开关关闭：整个图退化成单轮，跑一轮就进精排
    if not settings.agent_loop_enabled:
        return "rerank"
    # ② 本轮召回质量已达标：收敛出循环
    if state.get("context_sufficient"):
        return "rerank"
    # ③ 轮次用尽：必须收敛（否则会无限循环）
    #    用 .get(..., 0) 兜底：首轮进入本函数时该字段可能还没被写入
    if state.get("retrieval_round", 0) >= settings.agent_max_rounds:
        return "rerank"
    # 三个终止条件都不满足 → 回决策节点，让 planner 决定如何重试
    return "plan"


def _after_judge(state: RAGState) -> str:
    """judge_context 后的最终闸门：上下文足够 → END；不足 → refuse 节点。"""
    if state.get("context_is_enough"):
        return "end"
    return "refuse"


def _build_graph():
    """装配并编译检索子图。

    :return: 编译后的图（可调用对象）
    """
    # 1. 声明图的状态类型为 RAGState（TypedDict, total=False）
    builder = StateGraph(RAGState)

    # 2. 注册八个节点。
    #    节点是「拿 state、返回 partial state」的函数，LangGraph 会把返回值
    #    增量合并进状态 —— 因此各节点只需返回自己产出的那几个字段。
    builder.add_node("normalize_query", normalize_query)
    builder.add_node("route_query", route_query)
    builder.add_node("plan_retrieval", plan_retrieval)
    builder.add_node("retrieve", retrieve)
    builder.add_node("observe_context", observe_context)
    builder.add_node("rerank", rerank)
    builder.add_node("judge_context", judge_context)
    builder.add_node("refuse", refuse)

    # 3. 普通边：无分支的直线部分
    builder.add_edge(START, "normalize_query")
    builder.add_edge("normalize_query", "route_query")
    builder.add_edge("route_query", "plan_retrieval")
    builder.add_edge("retrieve", "observe_context")
    #    rerank 与 judge_context 是直线关系：精排完必然要裁定
    builder.add_edge("rerank", "judge_context")

    # 4. 条件边一：plan_retrieval 之后
    #    planner 说放弃 → refuse 节点统一写文案；否则继续检索
    builder.add_conditional_edges(
        "plan_retrieval",
        _after_plan,
        {"retrieve": "retrieve", "refuse": "refuse"},
    )

    # 5. 条件边二：observe_context 之后 —— 这条回边就是整个循环的"闭环"
    #    继续循环回 plan_retrieval；退出循环则进 rerank（不再直接 END）
    builder.add_conditional_edges(
        "observe_context",
        _after_observe,
        {"plan": "plan_retrieval", "rerank": "rerank"},
    )

    # 6. 条件边三：judge_context 之后的最终闸门
    #    上下文足够 → END（去生成）；不足 → refuse 节点统一拒答
    builder.add_conditional_edges(
        "judge_context",
        _after_judge,
        {"end": END, "refuse": "refuse"},
    )

    # 7. refuse 是终点前的最后一步：写完文案即结束
    builder.add_edge("refuse", END)

    # 8. 编译：产出可调用对象。
    #    编译期会校验"每条边都有去处"，因此拓扑写错会在这里直接暴露。
    return builder.compile()


# 模块加载时编译一次；编译产物无状态，请求里直接复用，无额外构造成本。
_rag_graph = _build_graph()


def get_rag_graph():
    """对外暴露已编译好的子图：模块加载时一次编译，请求里直接复用。"""
    return _rag_graph
