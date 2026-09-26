"""关键词（中文全文检索）检索封装层。

【模块职责说明】：
1. 业务链路聚合与抽象（Retriever Pattern）：
   封装从「自然语言 Query -> PostgreSQL 中文全文检索（zhparser 分词）-> Top-K 召回 -> 数据契约转换」
   的完整关键词检索管线，与 VectorRetriever 一起构成混合检索的两条腿。
2. 与向量检索的对称契约（Symmetric Contract）：
   对外产出与 VectorRetriever 完全相同的 RetrievedChunk 不可变契约，
   差异只体现在「填哪些调试字段」上：
   - 向量路填 sources=("vector",) / vector_rank / vector_score；
   - 关键词路填 sources=("keyword",) / keyword_rank / keyword_score。
   下游（RRF 融合、Prompt 拼装）因此无需区分两路来源，可直接统一处理。
3. 量纲隔离（Scale Isolation）：
   本模块产出的 score 是 ts_rank 全文排序分，**与余弦相似度量纲完全不同、不可跨路比较**，
   仅在两路合并排序或单路展示时使用。跨路排序必须交由 RRF 按「排名」而非「分数」完成。
4. 纯异步 I/O 驱动（Async Pipeline）：
   基于 `async/await` 非阻塞执行数据库全文检索，不在事件循环中做任何同步阻塞操作。
"""

# 引入统一的检索数据契约，保证两路召回产出同一结构
from app.db.repositories.chunk_repo import DocumentChunkRepository
from app.retrieval.vector_retriever import RetrievedChunk

# 第 9 期：观测 SDK 的装饰器。本模块走裸 SQLAlchemy，SDK 自动捕获不到，
# 必须手动打点才能让 trace 树里出现"关键词召回"这一层。
from langsmith import traceable

# 引入异步数据库会话类，用于支持 async/await 异步数据库操作
from sqlalchemy.ext.asyncio import AsyncSession


class KeywordRetriever:
    """
    关键词检索器：封装从 用户文本 -> 中文全文检索 -> 结果契约映射 的完整流水线。

    与 VectorRetriever 的差异：不做向量化（无需调用 Embedding 模型），
    因此没有网络 I/O 与模型调用开销，纯数据库检索，耗时通常远低于向量路。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        初始化检索器，注入数据库异步会话并实例化底层的 chunk 仓储。
        """
        self.chunk_repo = DocumentChunkRepository(session)

    # 【第 9 期】手动打点：与向量路对称，本方法走裸 SQLAlchemy 全文检索。
    #   两路都打点后，才能对比"向量路慢还是关键词路慢"。
    @traceable(name="KeywordRetriever.search", run_type="retriever")
    async def search(
        self,
        query: str,
        top_k: int,
        *,
        permission_tags: list[str] | None = None,
    ) -> list[RetrievedChunk]:
        """
        根据文本 query 进行中文全文检索，返回最相关的 Top-K 个分块。

        :param query: 用户输入的查询文本（原始自然语言，无需预处理）
        :param top_k: 返回最相关的文档块数量
        :param permission_tags: 【第 9 章新增】调用方有效权限标签，原样透传给仓储层。
                                **None = 不做权限过滤**（admin / 离线评测 / 启动期种子）。
        :return: 包含 ts_rank 评分的 RetrievedChunk 列表

        【为什么关键词路也必须加这道过滤，而不能只加在向量路】
        两路是并发执行的，最终结果由 RRF 融合。若只有向量路过滤，
        一条"关键词命中但无权"的分块仍会进入最终候选并交给 LLM ——
        等于给越权内容留了一条旁路。两路必须严格对称。
        """
        # 1. 调用数据访问层执行 PostgreSQL 全文检索
        #    底层已完成 chinese_zh 分词、tsvector @@ tsquery 匹配与 ts_rank 打分，
        #    并按 rank 降序返回 (chunk, rank) 列表；
        #    【第 9 章】同时加入文档可见性过滤，无权文档的分块不进入候选。
        rows = await self.chunk_repo.keyword_search(
            query, top_k, permission_tags=permission_tags
        )

        # 2. 解析与组装结果列表（与 VectorRetriever.search 保持严格对称）
        return [
            RetrievedChunk(
                chunk_id=chunk.id,
                document_id=chunk.document_id,
                # 从级联关联的 document 对象中获取文档名（仓储层已 selectinload 预加载）
                document_name=chunk.document.name,
                content=chunk.content,
                page_no=chunk.page_no,
                section_path=chunk.section_path,
                # 3. score 直接取 ts_rank：
                #    【注意】关键词路的 score 不是相似度，取值无上界且受词频/文档长度影响，
                #    绝不能拿去和 settings.retrieval_min_score（相似度阈值）比较，
                #    也绝不能与向量路的 score 相加。它只在「关键词这一路内部」表示相关性排序。
                score=rank_score,
                # 4. 填充关键词路的调试字段：
                #    enumerate(..., start=1) 让 rank 从 1 起，与 RRF 公式里的 rank 口径一致
                sources=("keyword",),
                keyword_rank=rank,
                keyword_score=rank_score,
            )
            for rank, (chunk, rank_score) in enumerate(rows, start=1)
        ]
