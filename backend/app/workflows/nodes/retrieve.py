"""retrieve: 执行向量 Top-K 检索，并判断是否触发拒答。

multi_query 路径下需要多路召回 → 去重；其他路径走单路。
"""

# 引入异步数据库会话类
from sqlalchemy.ext.asyncio import AsyncSession

# 引入全局系统配置单例
from app.core.config import settings
# 引入拒答兜底文案常量
from app.llm.prompts import REFUSAL_ANSWER
# 引入切片数据契约与向量检索器
from app.retrieval.vector_retriever import RetrievedChunk, VectorRetriever
# 引入 RAG 共享图状态类型定义
from app.workflows.rag_state import RAGState


async def retrieve(state: RAGState, session: AsyncSession) -> RAGState:
    """RAG 工作流节点：执行向量检索并进行相关度置信度校验与拒答熔断。

    【双路径设计】：
    - multi_query 路径：对「原始问题 + 各条子查询」逐路召回，合并去重后取全局 Top-K；
    - 其他路径（original / rewrite / hyde）：直接对 state["query"] 单路召回。
    两条路径产出的 chunks 结构完全一致，因此下游生成节点无需感知差异。

    :param state: 当前工作流状态（需包含 "query"；multi_query 时还需 "multi_queries"）
    :param session: 外部注入的异步数据库会话
    :return: 增量状态更新字典，包含 retrieved_chunks、refused 及可能的 answer
    """
    # 1. 实例化向量检索器
    retriever = VectorRetriever(session)
    top_k = settings.retrieval_top_k

    # 2. 按策略决定召回路径
    if state.get("route") == "multi_query" and state.get("multi_queries"):
        # 各子查询独立召回，再合并去重；此处不做 RRF，留待下一期
        bundles: list[list[RetrievedChunk]] = []
        # 【为什么把原始问题也并入召回列表】：
        # 第 1 节保留 state["query"]（原问题）的理由是「作为不依赖模型生成的保底检索路径」——
        # 若此处不调用它，那条保底路径就形同虚设：一旦子查询生成质量不佳（如退化成同义替换），
        # 多路召回会整体跑偏，而原问题本可以兜住。本轮回合的原始问题向量未缓存（只缓存了
        # rewrite/hyde 改写后的版本），需额外一次向量化，但换来一条不受模型质量影响的召回路径。
        for sub_query in [state["query"], *(state["multi_queries"] or [])]:
            bundles.append(await retriever.search(sub_query, top_k=top_k))
        chunks = _merge_chunks(bundles, top_k)
    else:
        # 单查询:  query → 1 次检索 → Top-K 条 chunks
        chunks = await retriever.search(state["query"], top_k=top_k)

    # 3. 双重置信度守卫判定：
    #    - 场景 A: 压根没召回到任何 chunk（not chunks）
    #    - 场景 B: 召回的最佳候选（Top-1）余弦相似度低于配置阈值
    #    多路召回经合并排序后，chunks[0] 即全局最高分，因此本判定无需改动。
    refused = not chunks or chunks[0].score < settings.retrieval_min_score

    # 4. 组装基础增量更新数据
    update: RAGState = {
        "retrieved_chunks": chunks,
        "refused": refused,
    }

    # 5. 若触发熔断，直接置入标准拒答文案，下游无需再调用大模型推理
    if refused:
        update["answer"] = REFUSAL_ANSWER

    return update


def _merge_chunks(bundles: list[list[RetrievedChunk]], top_k: int) -> list[RetrievedChunk]:
    """多路召回结果去重 + 取 Top-K。

    【为什么需要去重】：
    同一个 chunk 可能在多条子查询中都命中（子查询角度不同但仍可能重叠），
    若不去重，同一段原文会在上下文里重复出现，浪费 Token 且抬高无关片段的相似度。

    【为什么保留最高分】：
    高分说明"某个角度的查询与它非常匹配"，这个信息比平均分更有价值。

    【Top-K 语义】：
    每路各取 top_k 条候选 → 合并去重 → 全局按 score 降序 → 取前 top_k。
    这样输出条数始终是 top_k，prompt 的 Token 预算可预测（不会随路数波动）。

    :param bundles: 每一路召回的 chunk 列表
    :param top_k: 合并后要保留的条数
    :return: 去重并按相似度降序排列的 Top-K 列表
    """
    # 以 chunk_id 字符串为键去重：RetrievedChunk 是 frozen dataclass，字段可安全作键
    best: dict[str, RetrievedChunk] = {}
    for bundle in bundles:
        for chunk in bundle:
            key = str(chunk.chunk_id)
            prev = best.get(key)
            # 首次出现或本次分数更高时替换，保证同一切片只保留最高分的那一份
            if prev is None or chunk.score > prev.score:
                best[key] = chunk
    # 全局按相似度降序排序后截断，保证输出条数稳定
    ranked = sorted(best.values(), key=lambda c: c.score, reverse=True)
    return ranked[:top_k]
