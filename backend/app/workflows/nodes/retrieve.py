"""知识库向量检索与置信度熔断图节点。

【模块职责说明】：
1. 向量检索编排（Vector Retrieval Orchestration）：
   作为 LangGraph 工作流的核心检索节点，桥接 `VectorRetriever` 检索组件，
   基于前序节点产出的标准查询词（state["query"]），执行异步 Top-K 向量近似检索。
2. 双重条件拒答熔断（Refusal Guard & Short-Circuiting）：
   实现严格的置信度守卫，当前置命中“召回列表为空”或“Top-1 最高余弦相似度低于阈值（retrieval_min_score）”时，
   判定知识库缺乏有效依据，触发 `refused=True` 熔断机制，直接提前写入标准拒答文案（REFUSAL_ANSWER），
   避免无意义调用下游大模型产生幻觉，同时极大节约 Token 消耗与响应延迟。
3. 状态增量合并契约（State Contract & Partial Update）：
   符合 LangGraph 的状态更新机制，按需将本次检索产出的分块列表（retrieved_chunks）、
   熔断标志（refused）以及可选的兜底回答（answer）打包为增量字典回传合并。
"""

# 引入异步数据库会话类
from sqlalchemy.ext.asyncio import AsyncSession

# 引入全局系统配置单例
from app.core.config import settings
# 引入拒答兜底文案常量
from app.llm.prompts import REFUSAL_ANSWER
# 引入前面封装的向量检索器类
from app.retrieval.vector_retriever import VectorRetriever
# 引入 RAG 共享图状态类型定义
from app.workflows.rag_state import RAGState


async def retrieve(state: RAGState, session: AsyncSession) -> RAGState:
    """
    RAG 工作流节点：执行向量检索并进行相关度置信度校验与拒答熔断。

    :param state: 当前工作流状态（必须包含 "query" 字段）
    :param session: 外部注入的异步数据库会话
    :return: 增量状态更新字典，包含 retrieved_chunks、refused 及可能的 answer
    """
    # 1. 实例化向量检索器
    retriever = VectorRetriever(session)

    # 2. 异步执行向量近似检索，召回 Top-K 个按相似度排序的 RetrievedChunk 列表
    chunks = await retriever.search(state["query"], top_k=settings.retrieval_top_k)

    # 3. 双重置信度守卫判定：
    #    - 场景 A: 压根没召回到任何 chunk（not chunks）
    #    - 场景 B: 召回的最佳候选（Top-1）余弦相似度分数低于配置阈值（chunks[0].score < retrieval_min_score）
    #    命中任意一种情况，即视为知识库中无可靠支撑依据
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