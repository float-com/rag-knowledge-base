"""observe_context：Agentic RAG 循环的[观察]节点。

【模块职责】：
看这一轮的召回结果够不够好，决定"继续循环"还是"出图"。

【纯规则节点】：
本节点**不调用任何模型**，只看 Top1 的向量相似度是否过阈值。
之所以用向量原始分而不是融合分：只有余弦相似度是绝对值、可与阈值比较
（见第 6 期归档：rrf_score 上限仅 2/(k+1)，ts_rank 无上界且跨查询不可比）。

【回填而非新增】：
把[观察]字段写回 agent_steps 的**最后一条**记录（也就是本轮 plan_retrieval 刚追加的那条），
使"决策"与"观察"始终是同一条 dict，避免用下标算术去对应两者。
"""

# 引入全局系统配置单例（读 retrieval_min_score 阈值）
from app.core.config import settings
# 引入切片数据契约（类型标注用）
from app.retrieval.vector_retriever import RetrievedChunk
# 引入 RAG 共享图状态类型定义
from app.workflows.rag_state import RAGState


async def observe_context(state: RAGState) -> RAGState:
    """观察本轮召回质量：判定是否充分，并把观察结果回填到 agent_steps。

    :param state: 当前工作流状态（需包含 retrieved_chunks；循环中还需 agent_steps）
    :return: 增量字典，含 agent_steps（已回填）、retrieval_round（自增）、context_sufficient
    """
    # 1. 取本轮召回的切片。
    #    retrieve 可能降级返回空列表，因此这里必须容忍缺失，用 [] 兜底。
    chunks: list[RetrievedChunk] = state.get("retrieved_chunks", [])

    # 2. 判定是否充分（纯规则，不调模型）
    sufficient = _is_sufficient(chunks)

    # 3. 取出 Top1 的向量相似度，供前端与 planner 复盘。
    #    【为什么保留 4 位小数】与第 6 期 retrieval_meta 一致：避免浮点尾数抖动。
    #    【为什么判 None】仅关键词命中的切片 vector_score 为 None，
    #    此时不能参与比较（None < float 会抛 TypeError），直接给 None。
    top_score = (
        round(chunks[0].vector_score, 4) # 向量相似度分数保留 4 位小数，既便于质量阈值判断，又避免过长浮点数浪费上下文 Token
        if chunks and chunks[0].vector_score is not None
        else None
    )

    # 4. 回填[观察]字段到本轮那条记录。
    #    复制列表而非原地改，保持节点"复制 → 修改 → 整份写回"的纯函数风格。
    steps = list(state.get("agent_steps", []))
    if steps:
        # 只动最后一条 —— 它就是本轮 plan_retrieval 刚追加的决策记录。
        # 用 last = dict(...) 再整体替换，而不是 steps[-1]["x"] = ...：
        # 前者产出的是一个新 dict，与"不原地修改状态"的约定一致。
        last = dict(steps[-1])
        last["retrieved_count"] = len(chunks)
        last["top_score"] = top_score
        last["sufficient"] = sufficient
        steps[-1] = last

    # 5. 组装增量更新。
    #    retrieval_round 必须自增：它是"轮次用尽"这个出口的判定依据，
    #    若漏掉自增，rounds >= agent_max_rounds 永远为假 → 循环永不退出（死循环）。
    return {
        "agent_steps": steps,
        "retrieval_round": state.get("retrieval_round", 0) + 1,
        "context_sufficient": sufficient,
    }


def _is_sufficient(chunks: list[RetrievedChunk]) -> bool:
    """与 retrieve._should_refuse 互补：Top1 语义相似度过阈值即视为足够。

    【两者为什么必须互补】：
    - `retrieve._should_refuse`：判"该不该拒答"，**为 True 时熔断**；
    - `_is_sufficient`：判"够不够继续"，**为 True 时收敛出图**。
    两者看的是同一个信号（Top1 的 vector_score 与阈值），但方向相反：
    `_should_refuse` 为 False 的那些情况，正是 `_is_sufficient` 为 True 的情况。
    把它们分开命名，是为了让"熔断"与"收敛"两个语义在代码里各自显式。

    :param chunks: 本轮召回的切片列表
    :return: True 表示候选质量已足够
    """
    # 一条都没召回 → 显然不够
    if not chunks:
        return False

    top = chunks[0]

    # Top1 无向量分（仅关键词命中）→ 缺乏语义佐证，视为不够
    # （与 retrieve._should_refuse 的"场景 B：保守拒答"保持同一口径）
    if top.vector_score is None:
        return False

    # 语义相似度达标即视为足够
    return top.vector_score >= settings.retrieval_min_score