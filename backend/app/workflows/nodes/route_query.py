"""查询优化策略路由图节点。

【模块职责说明】：
1. 图节点与优化器之间的适配层（Node Adapter）：
   QueryRewriter 已经封装了「策略判定 → 调用对应策略 → 失败降级」的全部复杂性，
   本节点只做三件事：读开关、调用优化器、把结果翻译成 RAGState 的增量字典。
   因此它是本期的「缝合点」——把上游的状态契约与下游的优化能力接起来。

2. 总开关的短路入口（Feature Toggle & Short-Circuit）：
   读取 settings.query_route_enabled，关闭时直接返回 {"route": "original"}，
   保留 normalize_query 透传的 state["query"]（即原始提问），不发起任何模型调用。
   这样整条链路退化为上一期的单路检索行为，且零额外成本——
   是"Query 优化到底有没有效"的对照实验入口（详见第 2 章的效果度量）。

3. 查询词的覆盖语义（Query Override Contract）：
   本节点**覆盖** state["query"]，而非新增检索字段。
   因此下游 retrieve 永远只读 query 一个字段，不需要判断策略类型；
   以后新增第 5 种策略，只改 QueryRewriter，本节点与 retrieve 均无需改动。

4. 可选字段的按需回写（Partial State Update）：
   遵循 RAGState(total=False) 的增量合并约定，只回写非 None 的明细字段。
   未被本次策略产出的字段保持"不存在"状态，而不是被写入 None——
   这样下游用 state.get(...) 读取时语义清晰，也不会污染状态。
"""

from app.core.config import settings
from app.llm.query_rewriter import get_query_rewriter
from app.workflows.rag_state import RAGState


async def route_query(state: RAGState) -> RAGState:
    """RAG 工作流节点：判定并执行查询优化策略，产出最终的检索查询词。

    【前置条件】：必须在 normalize_query 之后调用（此时 state["query"] 已就绪）。

    【产出字段】：
    - route           实际采用的策略（原文未开启时为 original）
    - query           覆盖为策略产出的最终检索词（rewrite/hyde 路径下被改写）
    - rewritten_query rewrite 策略的明文改写结果（仅调试与前端展示）
    - hyde_answer     hyde 策略产出的假设答案（仅调试与前端展示）
    - multi_queries   multi_query 策略产出的子查询列表（retrieve 多路召回的输入）

    :param state: 当前工作流全局状态（需包含 "question"）
    :return: 仅包含本次产出字段的增量字典
    """
    # 1. 总开关短路：关闭时不做任何优化，也不调用模型。
    #    注意这里只返回 route，不返回 query——
    #    因为 state["query"] 已由 normalize_query 写入为原始提问，
    #    不改它就是"用原问题检索"，正是我们要的基线行为（也避免了多余的字段覆盖）。
    if not settings.query_route_enabled:
        # 关闭路由：直接走原始查询，保留 normalize_query 透传的 state["query"]
        return {"route": "original"}

    # 2. 委托给统一入口：策略判定、明细产出与失败降级都在 QueryRewriter 内部闭环。
    #    本节点不 try/except——优化器已保证"任何失败都降级 original 且不抛异常"，
    #    在这里再包一层异常处理反而是重复防御。
    #    【第 8 期起传 state["query"] 而不是 state["question"]】：
    #    normalize_query 已把"消解了指代、补全了省略"的独立问句写进 query，
    #    策略路由应当基于那个【已经完整】的问句判定与改写 ——
    #    否则「它怎么配置」这类残缺问句会被判成 original，改写能力白白浪费。
    result = await get_query_rewriter().optimize(
        question=state["query"],
        multi_query_count=settings.multi_query_count,
    )

    # 3. query 字段被覆盖：rewrite / hyde 路径下改用改写文本去向量召回；
    #    multi_query 路径下 result.query 仍是原问题（保底检索路径），子查询单独放在多查询字段。
    update: RAGState = {"route": result.route, "query": result.query}

    # 4. 明细字段按需回写：只写非 None 的，避免把未产出的字段写成 None。
    #    total=False 意味着"没产出"就是"字段不存在"，这是更干净的语义。
    if result.rewritten_query is not None:
        update["rewritten_query"] = result.rewritten_query
    if result.hyde_answer is not None:
        update["hyde_answer"] = result.hyde_answer
    if result.multi_queries is not None:
        update["multi_queries"] = result.multi_queries

    return update
