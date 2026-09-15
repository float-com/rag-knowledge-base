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

from dataclasses import dataclass
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
    统一各模块之间传递的检索数据契约，后续可扩展 rerank 分数等字段。

    score 是 cosine similarity（已统一成“越大越相似”），便于上层做阈值判断。
    """
    chunk_id: UUID  # 分块的全局唯一标识
    document_id: UUID  # 分块所属文档的全局唯一标识
    document_name: str  # 文档名称，方便前端或业务层直接展示溯源
    content: str  # 切分出的文本分块原始内容
    page_no: int | None  # 分块所在的页码（若无页码概念则为 None）
    section_path: str | None  # 分块在文档结构中的章节层级路径（如：第1章/1.1节）
    score: float  # 余弦相似度分数，值越大表示相关度越高


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
                score=1.0 - distance,
            )
            for chunk, distance in rows
        ]