"""retrieve: 执行混合检索（向量 + 中文全文，RRF 融合），并判断是否触发拒答。

multi_query 路径下需要多路召回 → 去重；其他路径走单路。
"""

# 引入全局系统配置单例
from app.core.config import settings
# 引入拒答兜底文案常量
from app.llm.prompts import REFUSAL_ANSWER
# 引入混合检索器：内部完成两路并发召回 + RRF 融合 + 取最终 Top-K
from app.retrieval.hybrid_retriever import HybridRetriever
# 引入切片数据契约（_merge_chunks 的类型标注与返回值需要）
from app.retrieval.vector_retriever import RetrievedChunk
# 引入 RAG 共享图状态类型定义
from app.workflows.rag_state import RAGState


async def retrieve(state: RAGState) -> RAGState:
    """RAG 工作流节点：执行混合检索并进行语义置信度校验与拒答熔断。

    【双路径设计】：
    - multi_query 路径：对各条子查询逐路召回，合并去重后取全局 Top-K；
    - 其他路径（original / rewrite / hyde）：直接对 state["query"] 单路召回。
    两条路径产出的 chunks 结构完全一致，因此下游生成节点无需感知差异。

    【为什么签名里去掉了 session】：
    混合检索器刻意不接收外部 session——两条腿必须各持一个独立会话才能并发
    （一个 AsyncSession 只对应一个连接和一个事务状态，共用会让两路互相毒化）。
    因此本节点不再需要数据库会话参数，也就不再与调用方的写事务耦合。

    :param state: 当前工作流状态（需包含 "query"；multi_query 时还需 "multi_queries"）
    :return: 增量状态更新字典，包含 retrieved_chunks、refused 及可能的 answer
    """
    # 1. 实例化混合检索器：它自身无状态、不持有会话，可以放心在此新建
    retriever = HybridRetriever()

    # 2. 两个不同的召回口径（必须区分，写反不会报错但会静默少召回）：
    #    - recall_top_k：每条腿各自召回的候选宽度，必须够宽，融合才有素材；
    #    - final_top_k ：融合之后真正交给 LLM 的切片数，受 prompt Token 预算约束。
    recall_top_k = settings.retrieval_recall_top_k
    final_top_k = settings.retrieval_top_k

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
                    final_top_k=final_top_k,
                )
            )
        chunks = _merge_chunks(bundles, top_k=final_top_k)
    else:
        # 单查询:  query → 1 次混合检索 → 融合后 Top-K 条 chunks
        chunks = await retriever.search(
            state["query"],
            recall_top_k=recall_top_k,
            final_top_k=final_top_k,
        )

    # 4. 拒答熔断判定（判定口径见 _should_refuse 的说明）
    refused = _should_refuse(chunks)

    # 5. 组装基础增量更新数据
    update: RAGState = {
        "retrieved_chunks": chunks,
        "refused": refused,
    }

    # 6. 按拒答与否写入 answer（第 7 期修正：两个分支都要写）
    #    【为什么"不拒答"时也要显式写空串】：
    #    Agentic 循环里本节点会被执行多轮，而 LangGraph 的状态是【跨轮累积】的 ——
    #    若第 1 轮熔断写了拒答文案、第 2 轮不熔断却不覆盖它，
    #    这段上一轮的文案就会残留在 state 里，最终出现
    #    「refused=False 却带着拒答文案」的矛盾状态，
    #    而服务层在非拒答路径会把 state["answer"] 当模型答案用。
    #    所以不熔断时必须显式清空 —— 与 plan_retrieval 的 rewrite 分支"显式写 None 清残留"同一手法。
    update["answer"] = REFUSAL_ANSWER if refused else ""

    return update


def _should_refuse(chunks: list[RetrievedChunk]) -> bool:
    """混合检索后的拒答判定，仅看 Top1 的语义相关度。

    【为什么不能再用 chunks[0].score 比阈值】：
    混合检索之后 score 字段存的是 RRF 融合分，量级约为 [0, 0.033]，
    而 retrieval_min_score（0.6）是按余弦相似度标定的——两者量纲完全不同，
    直接比较会让所有查询都恒拒答。所以必须回到"向量路的原始相似度" vector_score 上判断。

    【为什么 Top1 的 vector_score 为 None 时要拒答】：
    None 表示"向量路压根没召回它"，也就是仅由关键词路命中的切片。
    这类切片虽然字面精确（型号 / 接口路径 / 错误码），但它缺少语义层面的证据：
    无从判断这段文字是否真的在回答当前问题，可能只是恰好命中了同一个词。
    因此采取保守策略——宁可拒答，也不在没有语义佐证的情况下让模型开口。

    :param chunks: 融合后的切片列表（已按 rrf_score 降序）
    :return: True 表示应触发拒答熔断
    """
    # 场景 A：一条都没召回 → 无任何依据，必然拒答
    if not chunks:
        return True

    # 取融合后的首位：排序依据是 RRF 分，因此它就是"两条腿综合看最相关"的那一条
    top = chunks[0]

    # 场景 B：Top1 仅命中关键词路，缺乏语义佐证 → 保守拒答
    if top.vector_score is None:
        return True

    # 场景 C：向量路命中了，但语义相似度低于阈值 → 不达标，拒答
    return top.vector_score < settings.retrieval_min_score


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
