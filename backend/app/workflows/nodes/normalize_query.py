"""Query 标准化与多轮上下文化节点。

【模块职责说明】：
1. 多轮指代消解与省略补全（Contextualization）：
   作为 RAG 图工作流中介于上下文加载（load_context）与策略路由（route_query）之间的节点，
   基于 chat_history 把用户当前问题改写成**独立完整、可单独检索**的问句 ——
   消解"它/这个/上面提到的"等指代、补全省略的主语或宾语。
2. 降级优先（Fail-Safe by Design）：
   无历史直接透传、改写为空回退原问题、任何异常都降级 ——
   本节点绝不让 LLM 抖动阻断检索链路（与 QueryRewriter 内各方法的降级约定一致）。
3. 状态增量契约对齐（Partial State Update）：
   遵循 LangGraph 的 `TypedDict(total=False)` 规范，仅返回产出的 `{"query": ...}` 键值对，
   交由底层图执行器自动合并写入全局状态 `RAGState`。

【为什么改写结果写进 query 而不是覆盖 question】：
question 是"用户原话"，是业务事实，后续决策器 / 答案校验都要以它为准（避免被改写带偏）；
query 是"这一轮实际拿去检索的词"，是可加工的工作变量。
两者分开存，才能既保留用户的真实意图、又让检索用上补全后的问句。
"""

# 引入改写器单例（它的 contextualize 方法负责实际改写）
from app.llm.query_rewriter import get_query_rewriter
# 引入 RAG 共享图状态类型定义
from app.workflows.rag_state import RAGState


async def normalize_query(state: RAGState) -> RAGState:
    """RAG 工作流节点：基于对话历史把当前问题改写为独立可检索的问句。

    :param state: 当前工作流状态（需包含 "question"；可选 "chat_history"）
    :return: 仅包含增量字段 `{"query": str}` 的字典，用于合并回全局状态
    """
    # chat_history 由 load_context 预加载（正序、最近若干轮）；缺失时用 [] 兜底
    history = state.get("chat_history") or []

    # 首轮对话没有历史 → 没有指代可消解，直接透传原问题，省一次 LLM 调用
    if not history:
        return {"query": state["question"]}

    # 有历史则交给改写器做上下文化（内部已保证失败降级回原问题，不会抛异常）
    rewritten = await get_query_rewriter().contextualize(
        question=state["question"], history=history
    )
    return {"query": rewritten}
