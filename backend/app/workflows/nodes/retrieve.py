"""retrieve: 执行混合检索（向量 + 中文全文，RRF 融合）。

multi_query 路径下需要多路召回 → 去重；其他路径走单路。

【第 8 期职责收窄】：本节点【只负责召回】，不再判拒答、不再写 answer。
- 拒答判定移交给 `judge_context`（精排后按精排分裁定）；
- 拒答文案统一由 `refuse` 节点输出。
因此出口从 `retrieval_top_k` 【放大】到 `retrieval_recall_top_k` ——
把全部候选交给下游 rerank 精排，由精排决定最终留下哪几条。

【第 9 章新增职责】：把 `state["permissions"]` 往下透传到检索器。
本节点不判断"谁是管理员"（那是进图前的事），只负责把统一状态里的
权限标签原样传下去，最终变成 chunk_repo 里 SQL 的可见性 WHERE。
若漏传这一处，前面第 8 章的所有权限过滤都会被检索这条路径绕过去。
"""

# 引入全局系统配置单例
from app.core.config import settings
# 引入混合检索器：内部完成两路并发召回 + RRF 融合 + 取最终 Top-K
from app.retrieval.hybrid_retriever import HybridRetriever
# 引入切片数据契约（_merge_chunks 的类型标注与返回值需要）
from app.retrieval.vector_retriever import RetrievedChunk
# 引入 RAG 共享图状态类型定义
from app.workflows.rag_state import RAGState


async def retrieve(state: RAGState) -> RAGState:
    """RAG 工作流节点：执行混合检索，产出候选切片。

    【双路径设计】：
    - multi_query 路径：对各条子查询逐路召回，合并去重后取全局 Top-K；
    - 其他路径（original / rewrite / hyde）：直接对 state["query"] 单路召回。
    两条路径产出的 chunks 结构完全一致，因此下游生成节点无需感知差异。

    【为什么签名里去掉了 session】：
    混合检索器刻意不接收外部 session——两条腿必须各持一个独立会话才能并发
    （一个 AsyncSession 只对应一个连接和一个事务状态，共用会让两路互相毒化）。
    因此本节点不再需要数据库会话参数，也就不再与调用方的写事务耦合。

    【本期只做召回，不判拒答】：
    召回阶段的口径是"宁滥勿缺"——尽量多地把可能有用的都捞回来。
    截断需要一个"相关性"判据，而本阶段手里只有 RRF 融合分，它只反映排名；
    真正的相关性判据来自下游的 rerank（成对打分），所以截断也放到那里做。

    :param state: 当前工作流状态（需包含 "query"；multi_query 时还需 "multi_queries"）
    :return: 增量状态更新字典，仅含 retrieved_chunks
    """
    # 1. 实例化混合检索器：它自身无状态、不持有会话，可以放心在此新建
    retriever = HybridRetriever()

    # 2. 本期只保留一个召回口径：recall_top_k。
    #    【为什么不再需要 final_top_k】：
    #    融合之后不再截断——全部候选交给 rerank 精排后再裁，
    #    这样"哪几条最终进 prompt"由精排分决定，而不是由融合分决定。
    recall_top_k = settings.retrieval_recall_top_k

    # 2.1 【第 9 章】取出本用户的权限标签，往下透传给检索器（最终变成 SQL 的 WHERE）。
    #     【为什么用 state.get 而不是 state["permissions"]】
    #       permissions 是第 11 期新增字段，历史路径（旧评测脚本、单元测试里手搓的
    #       state 字典）可能没有它；用 .get 拿到 None，正好等价于"不做权限过滤"，
    #       与"内部调用不受限制"的既有语义一致，不会把老代码打断。
    #     【为什么这里不做 is_admin / 通配判断】
    #       本节点是图内节点，只应面向统一状态读写，不该去理解"谁是管理员"。
    #       admin 语义已在【进图前】由 ChatService / _viewer_tags 翻译成 None 或 ["*"]，
    #       判断只保留在"知道用户是谁"的那一层（见 document_repo.build_permission_filter）。
    permissions = state.get("permissions")

    # 3. 按策略决定召回路径
    if state.get("route") == "multi_query" and state.get("multi_queries"):
        # 各子查询独立走一次 hybrid 检索，再合并去重
        bundles: list[list[RetrievedChunk]] = []
        # 【与上一期的差异】：上一期把原始问题 state["query"] 也并入召回列表，
        # 作为"不依赖模型生成的保底检索路径"。本期改为只用子查询，原因有两层：
        #   ① 每条子查询现在本身就是混合检索（语义 + 字面），召回质量已比上一期的
        #      纯向量单路更稳，重复召回原始问题的边际收益下降；
        #   ② 少一次完整的两路并发召回，直接省掉一次嵌入调用与两次数据库查询。
        # 权衡：多路由一旦整体跑偏（如子查询退化成同义替换），就没有原问题兜底了。
        for sub_query in state["multi_queries"] or []:
            bundles.append(
                await retriever.search(
                    sub_query,
                    recall_top_k=recall_top_k,
                    final_top_k=recall_top_k,
                    # 【第 9 章】每条子查询都要带权限 —— 少传一条就是一条越权召回路径
                    permission_tags=permissions,
                )
            )
        chunks = _merge_chunks(bundles, top_k=recall_top_k)
    else:
        # 单查询:  query → 1 次混合检索 → 融合后的候选（不截断）
        chunks = await retriever.search(
            state["query"],
            recall_top_k=recall_top_k,
            final_top_k=recall_top_k,
            permission_tags=permissions,
        )

    # 4. 只写召回结果 —— refused / answer 都不再由本节点负责
    return {"retrieved_chunks": chunks}


def _merge_chunks(
    bundles: list[list[RetrievedChunk]], top_k: int
) -> list[RetrievedChunk]:
    """multi_query 子查询结果合并：去重 + 取 Top-K。

    同一个 chunk 可能在多条子查询中都命中；保留 RRF 分最高的那条，
    再整体按 RRF 分降序取前 top_k。

    :param bundles: 每一路召回的 chunk 列表
    :param top_k: 合并后要保留的条数
    :return: 去重并按 RRF 分降序排列的 Top-K 列表
    """
    # 以 chunk_id 字符串为键去重：RetrievedChunk 是 frozen dataclass，字段可安全作键
    best: dict[str, RetrievedChunk] = {}
    for bundle in bundles:
        for chunk in bundle:
            key = str(chunk.chunk_id)
            prev = best.get(key)
            # 首次出现，或本次的 RRF 分更高时替换，保证同一切片只保留分最高的那一份。
            # 两端都做 `or 0.0` 兜底：rrf_score 类型是 float | None，
            # 直接比较会在 None 参与时抛 TypeError。
            if prev is None or (chunk.rrf_score or 0.0) > (prev.rrf_score or 0.0):
                best[key] = chunk
    # 全局按 RRF 分降序排序后截断，保证输出条数稳定（不随子查询条数波动）
    ranked = sorted(best.values(), key=lambda c: c.rrf_score or 0.0, reverse=True)
    return ranked[:top_k]
