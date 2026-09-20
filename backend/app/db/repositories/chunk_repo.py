"""
【模块职责说明】
本模块为知识库底层持久化层中的「切片仓储组件」（DocumentChunkRepository）。
基于 SQLAlchemy 2.0+ 异步模型与 pgvector 扩展，统一承接文档切片（DocumentChunk）的全生命周期持久化与高性能向量检索。
核心架构机制与设计考量如下：

1. pgvector 余弦距离近似检索（Vector Similarity Search）：
   利用 `embedding.cosine_distance` 运算符下推向量距离计算，高效召回高维向量空间中最相似的 Top-K 文档切片。
2. 数据状态守卫与脏读防御（Data Integrity & Status Guard）：
   向量检索时联表校验父级文档状态（`Document.status == "ready"`），严格排除处理中或损坏态的未完成分块，确保知识库检索内容精准有效。
3. 关联实体预加载防 N+1 查询（Eager Loading via selectinload）：
   检索切片时主动使用 `selectinload(DocumentChunk.document)` 批量预加载关联的父文档实体，
   规避上层溯源读取 `chunk.document.name` 时在异步上下文中因懒加载触发 MissingGreenlet 异常及 N+1 查询风暴。
4. 强安全多租户归属校验（Anti-IDOR / 防越权）：
   获取单个切片时强制组合 `document_id` 与 `chunk_id` 双键过滤，从数据层物理阻断水平越权访问其他文档切片的风险。
5. 批量高性能写入契约（Bulk Insertion & Unit of Work）：
   支持通过 `Sequence` 集合调用 `session.add_all()` + `flush()` 批量推送 SQL 缓冲区，
   降低数据库往返开销（RTT），且遵循仓储规范不主动 commit，将事务生命周期全权交由外层编排。
6. 服务端聚合计算与不可变契约（Server-side Aggregation & Immutability）：
   切片统计指标（count/avg/min/max）直接下推至 PostgreSQL 原生 SQL 引擎计算，防止进程内存膨胀（OOM）；
   统计结果承载于 `@dataclass(frozen=True)` 的 `ChunkStats`，保障业务流转中数据只读与线程安全。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
# 引入文档与文档分块持久层 ORM 模型
from app.db.models import Document, DocumentChunk
# 引入关系急加载加载策略，用于在异步环境下高效加载关联父表
from sqlalchemy.orm import selectinload


# 语法（Python 原生标准库装饰器）：@dataclass(frozen=True)
#   装饰器说明 (@dataclass)：自动为类生成 __init__、__repr__、__eq__ 等常用样板方法，专注充当纯数据传输对象（DTO / VO）
#   参数说明 (frozen=True)：冻结实体属性，实例化后字段只读不可修改（类似 Java 14+ Record 或 final 类），防止数据流转中被意外篡改
@dataclass(frozen=True)
class ChunkStats:
    """单个文档下的 chunk 长度聚合统计数据模型（DTO）。"""
    # 语法（Python 原生类型标注）：声明各统计指标强类型为整数（int）
    total: int
    avg_length: int
    min_length: int
    max_length: int


class DocumentChunkRepository:
    """文档切片实体数据库仓储类，封装对 DocumentChunk 表的原子增删改查。"""

    def __init__(self, session: AsyncSession) -> None:
        # 语法（Python 原生属性绑定）：持有当前请求生命周期的异步会话实例
        self.session = session

    async def bulk_add(self, chunks: Sequence[DocumentChunk]) -> None:
        """批量持久化切片实体集合。

        【参数说明】：
        - chunks: Sequence[DocumentChunk] 切片实体序列（抽象容器类型，兼容 list、tuple 等）
        """
        # 语法（Python 原生隐式布尔判断）：若传入序列为空，快速返回避免触发无意义的数据库 I/O
        if not chunks:
            return

        # 语法（SQLAlchemy 批量纳入追踪）：session.add_all(instances)
        #   参数1 (instances: Iterable[Any])：将切片实体批量纳入当前 Session 上下文追踪
        self.session.add_all(chunks)

        # 语法（SQLAlchemy 异步执行并下推 SQL）：
        #   方法说明 (flush)：一次性生成批量 INSERT SQL 写入数据库连接，但不提交事务（留给 Service 统一 commit）
        await self.session.flush()

    async def delete_by_document(self, document_id: UUID) -> None:
        """物理删除指定文档关联的所有切片记录。

        【参数说明】：
        - document_id (UUID): 父文档唯一主键标识
        """
        # 语法（SQLAlchemy 2.0 批量删除语句构建）：delete(entity).where(condition)
        #   函数说明 (delete)：构建 DELETE FROM document_chunks 语句
        #   条件绑定 (.where)：限定只删除外键绑定当前 document_id 的记录
        stmt = delete(DocumentChunk).where(DocumentChunk.document_id == document_id)

        # 语法（SQLAlchemy 异步执行）：发送 SQL 执行物理删除
        await self.session.execute(stmt)

    async def list_paginated_by_document(
        self,
        document_id: UUID,
        page: int,
        page_size: int,
    ) -> tuple[list[DocumentChunk], int]:
        """按文档主键分页检索切片列表（按切片序号升序），并返回切片总条数。

        【参数说明】：
        - document_id (UUID): 父文档主键 ID
        - page (int): 当前页码（从 1 开始计）
        - page_size (int): 单页大小截断限制

        【返回值】：
        - tuple[list[DocumentChunk], int]: (当前页切片对象列表, 关联切片总数)
        """
        # 语法（Python 原生算术运算）：根据页码推算数据库游标偏移行数
        offset = (page - 1) * page_size

        # 语法（SQLAlchemy 2.0 分页构建）：
        #   条件过滤 (.where)：限定当前文档主键
        #   时序排序 (.order_by(DocumentChunk.chunk_index.asc()))：按切片逻辑序号严格升序，还原原始阅读顺序
        #   游标切分 (.offset().limit())：执行数据库服务端物理分页
        items_stmt = (
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.chunk_index.asc())
            .offset(offset)
            .limit(page_size)
        )

        # 语法（SQLAlchemy 聚合计数构建）：基于文档 ID 统计该文档拥有的总切片数
        count_stmt = (
            select(func.count())
            .select_from(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
        )

        # 语法（SQLAlchemy 异步执行与实体序列消费）：
        #   .scalars().all()：扁平化提取 ORM 映射切片实体并转为序列
        items = (await self.session.execute(items_stmt)).scalars().all()

        # 语法（SQLAlchemy 异步执行与聚合标量单值提取）：
        #   .scalar_one()：提取单行单列的 COUNT 计数结果
        total = (await self.session.execute(count_stmt)).scalar_one()

        # 语法（Python 原生类型构造与元组返回）：返回 (切片列表, 记录总数)
        return list(items), int(total)

    async def get_for_document(
        self,
        document_id: UUID,
        chunk_id: UUID,
    ) -> DocumentChunk | None:
        """根据文档 ID 与切片 ID 复合检索单个切片实体。

        强校验归属，杜绝跨文档越权（水平越权漏洞防护）。

        【参数说明】：
        - document_id (UUID): 期望所属的父文档主键
        - chunk_id (UUID): 切片自身唯一标识

        【返回值】：
        - DocumentChunk | None: 命中的切片实体；若未命中或切片并不归属于该文档，返回 None
        """
        # 语法（SQLAlchemy 2.0 复合条件声明）：
        #   多条件过滤 (.where(cond1, cond2))：隐式按 AND 逻辑拼接两个过滤条件
        stmt = select(DocumentChunk).where(
            DocumentChunk.id == chunk_id,
            DocumentChunk.document_id == document_id,
        )

        # 语法（SQLAlchemy 异步执行与安全单记录提取）：
        #   .scalar_one_or_none()：命中返回单实体，未命中返回 None，重复数据抛出异常
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_stats(self, document_id: UUID) -> ChunkStats | None:
        """一条聚合 SQL 查询文档切片的 count/avg/min/max 指标，避免拉取到 Python 内存二次遍历。

        【参数说明】：
        - document_id (UUID): 目标父文档 ID

        【返回值】：
        - ChunkStats | None: 聚合指标 DTO；若该文档尚无切片数据，返回 None
        """
        # 语法（SQLAlchemy 数据库原生字符长度计算函数）：func.char_length(column)
        #   函数映射：映射到 PostgreSQL 原生 CHAR_LENGTH() 函数，按字符而非字节计算长度
        length = func.char_length(DocumentChunk.content)

        # 语法（SQLAlchemy 2.0 聚合查询与别名指定）：
        #   .label("alias")：为聚合计算列打上字段别名标签，便于后续按属性名提取
        #   func.count() / func.avg() / func.min() / func.max()：分别映射标准 SQL 聚合函数
        stmt = (
            select(
                func.count().label("total"),
                func.avg(length).label("avg_len"),
                func.min(length).label("min_len"),
                func.max(length).label("max_len"),
            )
            .where(DocumentChunk.document_id == document_id)
        )

        # 语法（SQLAlchemy 异步行提取）：
        #   方法调用 (.one())：断言聚合查询必有一行元组返回（即使无数据，COUNT 也返回 0，其余为 NULL）
        row = (await self.session.execute(stmt)).one()

        # 语法（Python 原生防御性判断）：若聚合结果总条数为 0，说明未入库或无分块，直接返回 None
        if not row.total:
            return None

        # 语法（Python 原生短路或运算与数据类实例化）：
        #   短路兜底 (row.field or 0)：防范 SQL avg/min/max 返回 None 时触发类型转换异常
        #   强类型实例化 (ChunkStats(...))：包装为只读 DTO 返回
        return ChunkStats(
            total=int(row.total),
            avg_length=int(row.avg_len or 0),
            min_length=int(row.min_len or 0),
            max_length=int(row.max_len or 0),
        )

    async def vector_search(
            self,
            query_embedding: list[float],
            top_k: int,
    ) -> list[tuple[DocumentChunk, float]]:
        """
        按 cosine 距离做 Top-K 向量检索。

        - 仅检索状态为 ready 的文档（避免拿到尚未完成入库的脏 chunk）
        - 返回 (chunk, distance) 列表，distance 越小越相似（pgvector cosine_distance）
        - 用 selectinload 把所属 Document 一并加载，方便上层直接读 document.name 而不会再发 N 次 lazy load 查询

        :param query_embedding: 待检索的浮点型向量列表
        :param top_k: 期望返回的最相关候选分块数量
        :return: (分块实体, 余弦距离) 的元组列表
        """
        # 1. 构建 pgvector 原生余弦距离计算表达式（<=> 运算符对应 cosine_distance）
        distance = DocumentChunk.embedding.cosine_distance(query_embedding)

        # 2. 编排向量检索 SQL 语句
        stmt = (
            select(DocumentChunk, distance.label("distance"))
            # 内连接父级 Document 表以校验入库状态
            .join(Document, Document.id == DocumentChunk.document_id)
            # 状态守卫：仅检索已完成解析、索引并就绪的文档分块
            .where(Document.status == "ready")
            # 按余弦距离升序排列（距离越小，语义相关度越高）
            .order_by(distance.asc())
            # 限制召回最大条数
            .limit(top_k)
            # 预加载父文档实体，防止上层遍历读取 chunk.document.name 时产生 N+1 查询
            .options(selectinload(DocumentChunk.document))
        )

        # 3. 异步执行查询并提取所有匹配行
        rows = (await self.session.execute(stmt)).all()

        # 4. 组装为强类型元组返回，将 distance 标量安全转为 Python float
        return [(chunk, float(dist)) for chunk, dist in rows]

    async def keyword_search(
            self,
            query: str,
            top_k: int,
    ) -> list[tuple[DocumentChunk, float]]:
        """
        基于 PostgreSQL 全文检索召回与用户输入最相关的 Top-K 文档切片。

        核心设计与工作流程：
        1. 中文分词（chinese_zh）：
           - 使用 Alembic 迁移预建好的 'chinese_zh' 全文检索配置（底层集成 zhparser 分词插件）。
        2. 智能容错与语义解析（plainto_tsquery）：
           - 将用户输入当作纯文本处理，切词后自动按逻辑 AND（&）组装；
           - 智能容错：例如无论用户搜「差旅 报销」还是「差旅报销」，都会切成同一组 token；
           - 规避崩溃：自动过滤 &、!、:、* 等特殊字符，防止畸形输入触发 SQL 语法错误。
        3. 状态校验（status = 'ready'）：
           - 严格仅召回入库完成的有效文档，避免拿到正在解析或失败的脏 chunk（与向量检索保持一致）。
        4. 分值与排序（ts_rank）：
           - 基于词频（TF）和词项稀缺度打分，分数越大代表字面匹配度越高；
           - 【注意】ts_rank 与向量检索的余弦相似度数值体系完全不同，不能直接比大小，
             多路混合检索时需在后续业务层统一交由 RRF（倒数排名融合）算法排序。

        :param query: 用户原始输入的搜索文本（支持任意特殊字符与空格）
        :param top_k: 期望返回的最相关切片最大条数
        :return: (DocumentChunk 实体, ts_rank 相关度得分) 的元组列表，按分值降序排列
        """
        # 1. 查询词解析：切词并生成数据库匹配表达式（tsquery）
        #    【说明】func 是 SQLAlchemy 的动态工厂（通过 __getattr__ 映射底层 SQL 函数），
        #    IDE 提示“找不到要转到的声明”属正常现象，不影响运行。
        tsquery = func.plainto_tsquery("chinese_zh", query)

        # 2. 构造评分表达式：计算切片全文向量与查询表达式的匹配得分
        #    【避坑】SQLAlchemy 未在方言顶层直接封装 ts_rank 函数，必须通过 func.ts_rank 动态调用，
        #    否则显式 import 会抛出 ImportError。
        rank_expr = func.ts_rank(DocumentChunk.content_tsv, tsquery)

        # 3. 编排检索 SQL 语句
        stmt = (
            select(DocumentChunk, rank_expr.label("rank"))
            # 关联父级 Document 表，用于校验整篇文档的状态
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(
                # 状态守卫：仅检索就绪状态的文档，过滤脏数据
                Document.status == "ready",
                # 全文命中：@@ 操作符判断切片分词向量（content_tsv）是否满足 tsquery
                DocumentChunk.content_tsv.op("@@")(tsquery),
            )
            # 按全文匹配分降序排列（最相关的排在最前）
            .order_by(rank_expr.desc())
            # 限制召回最大条数
            .limit(top_k)
            # 预加载父文档实体：通过 JOIN 一并查出 document，防止后续访问 chunk.document 时产生 N+1 查询
            .options(selectinload(DocumentChunk.document))
        )

        # 4. 异步执行查询并取出所有命中行
        rows = (await self.session.execute(stmt)).all()

        # 5. 组装返回：将数据库数值安全转为 Python 原生 float
        return [(chunk, float(rank)) for chunk, rank in rows]