"""回答引用溯源仓储层。

【模块职责说明】：
1. 仓储模式与引用数据持久化（Repository Pattern）：
   封装 AnswerCitation 实体的持久化逻辑，记录每条模型回复所引用的具体文档分块、引用序号及对应片段，
   隔离底层的 SQL 拼装与 ORM 映射细节。
2. 工作单元与事务约定（Unit of Work）：
   遵循仓储层事务设计规范，仓储内部严禁主动调用 `commit()` 提交事务。
   批量追加实体时仅执行 `await session.flush()` 将待插入记录刷新至数据库事务缓冲区，
   保证引用数据与外层的回答消息（Message）能在同一数据库事务中实现原子性提交或回滚。
3. 纯异步 I/O 驱动（Async Engine）：
   依托 SQLAlchemy 2.0+ 异步体系，搭配 `async/await` 异步驱动进行批量落库，
   避免在并发请求时产生任何阻塞事件循环的同步 I/O 操作。
"""

from collections.abc import Sequence
from uuid import UUID

# 引入异步数据库会话类
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# 引入回答引用持久层实体模型；另需两张父表做存在性校验（第 12 期缓存路径）
from app.db.models import AnswerCitation, Document, DocumentChunk


class AnswerCitationRepository:
    """
    回答引用仓储：负责管理 RAG 知识库问答生成的引用溯源数据（AnswerCitation）的数据库访问操作。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        初始化仓储实例，注入当前请求生命周期的异步数据库会话。
        """
        self.session = session

    async def bulk_add(self, citations: Sequence[AnswerCitation]) -> None:
        """
        批量写入知识库回答的引用出处实体。

        :param citations: 待批量持久化的 AnswerCitation 实体序列
        """
        # 1. 空值防守：若本次生成无任何引用分块，直接返回避免无意义的数据库通信
        if not citations:
            return

        # 2. 将序列中的引用对象批量挂载至会话上下文
        self.session.add_all(citations)

        # 3. 推送至底层数据库事务缓冲区，生成自增主键与触发完整性约束检查（不主动 commit）
        await self.session.flush()

    # =========================================================================
    # 第 12 期：缓存引用落库前的存在性校验
    # =========================================================================
    async def existing_chunk_ids(self, chunk_ids: Sequence[UUID]) -> set[UUID]:
        """返回这批 chunk_id 中【仍然存在】的那些。

        【为什么需要它】引用表的 `chunk_id` 外键指向 `document_chunks(id)`
        （`ON DELETE SET NULL` —— 删除时把引用行的该列置空）。
        但**插入时 PostgreSQL 仍会校验 id 是否存在**：
        拿一个已删除的 chunk_id 去插 → ForeignKeyViolation → 整个事务回滚 →
        连带把同一事务里的助手消息一起丢掉。

        语义缓存里的引用是"当时那一刻的快照"，最长可能存活 1 小时，
        期间文档被删是完全可能的 —— 所以落库前必须过滤一遍。

        :param chunk_ids: 待校验的 chunk 主键序列
        :return: 其中真实存在的 id 集合（不存在的不在集合里）
        """
        if not chunk_ids:
            return set()
        stmt = select(DocumentChunk.id).where(DocumentChunk.id.in_(list(chunk_ids)))
        return set((await self.session.execute(stmt)).scalars().all())

    async def existing_document_ids(self, document_ids: Sequence[UUID]) -> set[UUID]:
        """返回这批 document_id 中【仍然存在】的那些。理由同 existing_chunk_ids。

        :param document_ids: 待校验的 document 主键序列
        :return: 其中真实存在的 id 集合
        """
        if not document_ids:
            return set()
        stmt = select(Document.id).where(Document.id.in_(list(document_ids)))
        return set((await self.session.execute(stmt)).scalars().all())