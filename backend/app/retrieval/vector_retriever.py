"""向量检索封装层。

【模块职责说明】：
1. 业务链路聚合与抽象（Retriever Pattern）：
   封装从“自然语言 Query -> 异步文本向量化（Embedding）-> pgvector Top-K 近似检索 -> 数据契约转换”的完整检索管线，
   对上层业务（如 RAG 编排、Agent 节点）屏蔽底层向量模型调用与 SQL 检索细节，实现“文本输入即得结构化分块”。
2. 不可变数据契约（Data Transfer Object）：
   对外暴露只读且不可变的 `RetrievedChunk`（frozen dataclass），作为跨阶段传递的标准数据契约，
   防范检索上下文在后续重排序（Rerank）、多路召回融合或 Prompt 拼接时被意外篡改。
3. 距离与相似度映射度量（Metric Normalization）：
   承接底层 pgvector 原生余弦距离（Cosine Distance），统一转换并归一化为余弦相似度分数（Cosine Similarity，score = 1.0 - distance），
   确保上层阈值过滤与相关度评估遵循“分值越大越相似”的一致性心智模型。
4. 纯异步 I/O 驱动（Async Pipeline）：
   全面适配异步编程范式，向量生成与底层数据库检索均基于 `async/await` 非阻塞执行，杜绝阻塞主事件循环。
"""

from dataclasses import dataclass, field
from uuid import UUID

# 引入异步数据库会话类，用于支持 async/await 异步数据库操作
from sqlalchemy.ext.asyncio import AsyncSession

# 引入底层分块数据仓储层，负责具体的数据库 SQL / 向量检索执行
from app.db.repositories.chunk_repo import DocumentChunkRepository
# 引入获取 embedding 模型的工厂函数/单例
from app.ingestion.embedder import get_embeddings


# frozen=True 表示实例创建后属性不可修改（不可变对象），防止数据在管道传递过程中被意外篡改
@dataclass(frozen=True)
class RetrievedChunk:
    """
    检索结果中单个 chunk 的展示视图（DTO 数据传输对象）。
    统一各模块之间传递的检索数据契约。

    【score 字段的兼容语义】：
    score 恒为"越大越相似"，但它的**具体含义随召回路径变化**：
    - 向量单路：等价于 vector_score（余弦相似度，值域 [0,1]）；
    - 关键词单路：等价于 keyword_score（ts_rank，无固定上界）；
    - 混合检索：等价于 rrf_score（RRF 融合分）。
    score 与 vector_score 两个字段并存，是为了让下游能分别按
    「统一排序位」或「特定路的原始分」取值，语义更清晰不产生歧义。
    """
    chunk_id: UUID  # 分块的全局唯一标识
    document_id: UUID  # 分块所属文档的全局唯一标识
    document_name: str  # 文档名称，方便前端或业务层直接展示溯源
    content: str  # 切分出的文本分块原始内容
    page_no: int | None  # 分块所在的页码（若无页码概念则为 None）
    section_path: str | None  # 分块在文档结构中的章节层级路径（如：第1章/1.1节）
    score: float  # 排序用的统一分数，值越大表示相关度越高

    # --- 以下 6 个字段为第 6 期新增：承载双路召回与 RRF 融合的调试信息 ---
    # sources / vector_rank / keyword_rank / rrf_score 是双路检索的调试字段，
    # 用于让前端调试面板与问题排查能看清"这条是从哪一路召回来的、各排第几"。
    # 单路检索时只有该路自己的 rank 有值，混合检索时多个字段会同时填上。
    sources: tuple[str, ...] = field(default_factory=tuple)  # 命中来源，如 ("vector",) 或 ("vector","keyword")
    vector_rank: int | None = None  # 在向量一路里的排名（1 起）；未命中为 None
    vector_score: float | None = None  # 原始余弦相似度（仅向量路命中时填充）
    keyword_rank: int | None = None  # 在关键词一路里的排名（1 起）；未命中为 None
    keyword_score: float | None = None  # 原始 ts_rank（仅关键词路命中时填充）
    rrf_score: float | None = None  # RRF 融合分（Σ 1/(k + rank)），仅混合检索时填充

    # --- 以下 1 个字段为第 8 期新增：承载精排阶段的成对相关性分 ---
    # reranker query-chunk 成对打分的相关度，越大越相关。
    #   【为什么单独一个字段而不是覆盖 score】：
    #   score 是"统一排序键"，其含义随召回路径变化（向量路=余弦、关键词路=ts_rank、混合=RRF）；
    #   而 rerank_score 是【精排模型给出的绝对相关性】，量纲与前几者都不同。
    #   单独存放才能让下游既按 rerank_score 排序，又保留原始向量分 / 召回来源等调试信息。
    rerank_score: float | None = None  # qwen3-rerank 输出的 relevance_score ∈ [0, 1]


class VectorRetriever:
    """
    向量检索器：封装从 用户文本 -> 生成向量 -> pgvector 向量检索 -> 结果模型映射 的完整流水线。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        初始化检索器，注入数据库异步会话并实例化底层的 chunk 仓储。
        """
        self.chunk_repo = DocumentChunkRepository(session)

    async def search(self, query: str, top_k: int) -> list[RetrievedChunk]:
        """
        根据文本 query 进行向量近似检索，返回最相似的 Top-K 个分块。

        :param query: 用户输入的查询文本
        :param top_k: 返回最相关的文档块数量
        :return: 包含相似度评分的 RetrievedChunk 列表
        """
        # 1. 将单条 query 文本向量化
        #    使用 DashScope / OpenAI 等接口提供的异步 aembed_query 方法直接生成向量嵌入
        embedding = await get_embeddings().aembed_query(query)

        # 2. 调用数据访问层执行 pgvector 的 Top-K 向量相似度查询
        #    返回结果通常包含 chunk 实体以及 pgvector 计算出的余弦距离 (cosine distance)
        rows = await self.chunk_repo.vector_search(embedding, top_k)

        # 3. 解析与组装结果列表
        return [
            RetrievedChunk(
                chunk_id=chunk.id,
                document_id=chunk.document_id,
                # 从级联关联的 document 对象中获取文档名
                document_name=chunk.document.name,
                content=chunk.content,
                page_no=chunk.page_no,
                section_path=chunk.section_path,
                # 4. 距离转相似度分数：
                #    pgvector 的 cosine_distance 范围在 [0, 2]（标准化向量为 [0, 1]），距离越小越相似；
                #    通过 1.0 - distance 将其转换为相似度（范围 [0, 1]），数值越大代表越相似。
                #    【为什么 score 与 vector_score 写成同一个表达式】：
                #    历史上 score 就等价于向量余弦相似度，retrieve 节点的拒答阈值
                #    （settings.retrieval_min_score）与前端引用卡片的 score 展示都依赖这个语义。
                #    单路向量检索时二者必须严格相等，否则阈值判定会与新字段悄悄脱钩。
                score=1.0 - distance,
                # 5. 填充第 6 期新增的调试字段：
                #    enumerate(..., start=1) 让 rank 从 1 起，与 RRF 公式里的 rank 口径一致
                sources=("vector",),
                vector_rank=rank,
                vector_score=1.0 - distance,
            )
            for rank, (chunk, distance) in enumerate(rows, start=1)
        ]