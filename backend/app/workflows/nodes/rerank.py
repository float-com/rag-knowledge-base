"""rerank：Agentic RAG 检索链路里的[精排]节点。

【模块职责】：
把检索节点召回的**大候选池**交给精排模型重新打分排序，再**截断**成最终要送进生成的小集合。

【分工原则（本期的核心设计）】：
    召回阶段宁滥勿缺，精排阶段宁缺勿滥。

- `retrieve` 负责"宁滥勿缺"：用 `retrieval_recall_top_k`（20）尽量多地把**可能有用**的都捞回来，
  不在这里做截断 —— 因为截断需要一个"相关性"判据，而召回阶段手里只有融合分，它只反映排名。
- `rerank` 负责"宁缺勿滥"：用专门的成对打分模型给出**绝对相关性**，这时候再截断才站得住脚。

【为什么本节点不做拒答判定】：
判定"够不够、要不要拒答"的职责在本期被移到 `judge_context` 节点。
本节点只做"打分 + 排序 + 截断"这一件事，保持单一职责。
"""

# 引入全局系统配置单例（读 rerank_enabled / retrieval_top_k）
from app.core.config import settings
# 引入精排客户端单例
from app.llm.reranker import get_reranker
# 引入图共享状态契约
from app.workflows.rag_state import RAGState


async def rerank(state: RAGState) -> RAGState:
    """对召回的候选切片做精排，并截断到 retrieval_top_k。

    :param state: 当前工作流状态（需包含 query 与 retrieved_chunks）
    :return: 增量字典，仅含 retrieved_chunks（已重排并截断）
    """
    chunks = list(state.get("retrieved_chunks", []))

    # 【两个短路条件】：
    # ① 精排总开关关闭 —— 支持"有 / 无精排"的对比实验（与 agent_loop_enabled 同一思路）
    # ② 候选不足 2 条 —— 没有排序可言，调精排纯属浪费一次请求与 token
    # 两者都直接返回空增量（而不是原样写回 retrieved_chunks）：
    # 状态本来就是增量的，"没改动"就不该写，避免下游误以为本节点产出过内容。
    if not settings.rerank_enabled or len(chunks) <= 1:
        return {}

    # 精排 + 排序（客户端内部已保证失败时降级为"原序不重排"，不会抛异常）
    reranked = await get_reranker().rerank(state["query"], chunks)

    # 截断到最终要送进生成的条数。
    # 注意这里是【切片】而不是原地改：reranked 已按 rerank_score 降序。
    return {"retrieved_chunks": reranked[: settings.retrieval_top_k]}
