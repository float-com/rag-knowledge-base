"""plan_retrieval：Agentic RAG 循环的[决策]节点。

【模块职责】：
每进入一轮检索之前，决定"这一轮用什么 query、走什么 route"。

【双分支设计】：
- 第 1 轮：`route_query` 上游已经判定过 route/query，本节点**不调 LLM**，
  只往 agent_steps 追加一条 `action=initial` 的记录（留痕，供前端面板展示）；
- 第 2 轮起：调用 `AgentPlanner.plan`，由 LLM 看历史观察决定重试方式。

【与 retrieve / observe_context 的分工】：
本节点负责「决策」，`retrieve` 负责「执行」，`observe_context` 负责「观察并回填」。
三者共用 agent_steps 里**同一条**记录：本节点追加决策字段，观测节点回填观察字段。
"""

# 引入全局系统配置单例（读 multi_query_count）
from app.core.config import settings
# 引入决策器单例（第 5 章封装）
from app.llm.agent_planner import get_agent_planner
# 引入拒答兜底文案常量
from app.llm.prompts import REFUSAL_ANSWER
# 引入改写器单例（switch_route 要真正补齐策略字段）
from app.llm.query_rewriter import get_query_rewriter
# 引入策略字面量类型与图状态契约
from app.workflows.rag_state import QueryRoute, RAGState


async def plan_retrieval(state: RAGState) -> RAGState:
    """Agentic RAG 循环的决策节点：产出本轮的 query / route，并登记一条决策记录。

    :param state: 当前工作流状态（需包含 question / query；第二轮起还需 agent_steps）
    :return: 仅包含本次产出字段的增量字典
    """
    # 1. 读出当前已累积的决策记录。
    #    用 list(...) 复制一份：本节点不原地修改状态里的列表，
    #    而是"复制 → 修改 → 整份写回"，保持节点纯函数风格（与 retrieve 一致）。
    steps = list(state.get("agent_steps", []))

    # 2. 当前生效的 route 与 query。
    #    - route 缺省视为 original（与 route_query 关闭时的行为一致）；
    #    - query 缺省时回落到原始提问：normalize_query 已写入 query，
    #      但若上游某步短路了，用 question 兜底比拿空串去检索安全得多。
    current_route: QueryRoute = state.get("route", "original") # type: ignore[assignment]
    current_query = state.get("query") or state["question"]

    # 3. 第 1 轮：route_query 已经决定好了 route/query，无需再调 LLM
    if not steps:
        steps.append(
            {
                # 首轮固定记为第 1 轮
                "round": 1,
                # 首轮动作固定为 initial —— 它不是 planner 给的，而是"沿用上游决策"。
                # 前端面板据此能把首轮与后续的 LLM 决策区分开。
                "action": "initial",
                "reason": "首轮检索沿用 route_query 决策",
                "route": current_route,
                "query": current_query,
            }
        )
        return {"agent_steps": steps}

    # 4. 后续轮：由 LLM planner 决定如何重试
    decision = await get_agent_planner().plan(
        question=state["question"],
        current_route=current_route,
        current_query=current_query,
        previous_steps=steps,
    )

    # 5. 准备增量更新。
    #    先以"保持现状"为默认值：若决策是 proceed / refuse，就不需要改动 route/query。
    update: RAGState = {}
    new_route = current_route
    new_query = current_query

    # 6. 决策分发：rewrite_query 与 switch_route 是唯一两种"真的改变检索输入"的动作
    if decision.action == "rewrite_query" and decision.new_query:
        # 改 query 同时把 route 强制重置为 original：上一轮可能是 multi_query。
        # 残留的 multi_queries 会让 retrieve 忽略本轮新 query，必须清干净
        new_query = decision.new_query
        new_route = "original"
        update["query"] = new_query
        update["route"] = new_route # type: ignore[assignment]
        # 显式写 None（而不是"不写"）：
        # 状态是增量的，若只覆盖 route/query 而留着上一轮的明细字段，
        # 下游（前端调试面板 / retrieve 的多路召回）会读到过期的旧产物。
        # 这里宁可写 None，也不给旧值任何被误读的机会。
        update["rewritten_query"] = None
        update["hyde_answer"] = None
        update["multi_queries"] = None
    elif decision.action == "switch_route" and decision.new_route:
        # 真正切换：调 QueryRewriter 补齐目标路由对应字段，避免只换标签不换行为
        # 例：original 切到 hyde，必须真的去生成假设答案，否则检索行为与 original 无异
        rewriter = get_query_rewriter()
        result = await rewriter.apply_route(
            question=state["question"],
            route=decision.new_route,
            multi_query_count=settings.multi_query_count,
        )
        # 以 apply_route 的结果为准：它内部失败会降级 original，
        # 因此这里不能直接用 decision.new_route，否则"降级了却仍标着新策略"。
        new_route = result.route
        new_query = result.query
        update["route"] = new_route
        update["query"] = new_query
        # 明细字段按需回写：apply_route 只会产出对应策略的那一个，
        # 未产出的保持 None（同样是"清掉上一轮残留"的意思）
        update["rewritten_query"] = result.rewritten_query
        update["hyde_answer"] = result.hyde_answer
        update["multi_queries"] = result.multi_queries

    # 7. 登记本轮的[决策]记录。
    #    [观察]字段（retrieved_count / top_score / sufficient）由 observe_context 回填，
    #    因此这里只写决策相关字段，两边共同构成同一条 dict。
    steps.append(
        {
            "round": len(steps) + 1,
            "action": decision.action,
            "reason": decision.reason,
            "route": new_route,
            "query": new_query,
        }
    )
    update["agent_steps"] = steps

    # 8. refuse 时直接终止图：retrieve 不再跑，answer 也要在这里兜底，
    #    否则 service 看到 refused=True 但 state["answer"] 缺失会发送空 token
    if decision.action == "refuse":
        update["refused"] = True
        update["answer"] = REFUSAL_ANSWER

    return update