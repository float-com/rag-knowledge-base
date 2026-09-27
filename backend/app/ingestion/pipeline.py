"""
【模块职责说明】
本模块为知识库离线摄取引擎的核心编排中枢（Ingestion Pipeline Coordinator）。
负责将文件下载、Docling 解析、文本切分、向量嵌入（Embedding）及数据库落库完整串联。

全流程严格对齐 7 大标准阶段：
1. 查询文档 (独立短事务)
2. 下载文件 (COS 原始数据读取)
3. 解析内容 (Docling 多模态提取)
4. 切分 chunks (语义分块与元数据注入)
5. 生成 embeddings (批量向量化计算)
6. 写入数据库 (独立短事务批量持久化)
7. 状态收尾与异常处理 (状态机 READY / FAILED 兜底)

核心架构机制：
1. 短事务与连接释放（Connection Pool Protection）：
   解析与向量化等外部网络/模型推理耗时数秒至数十秒，期间绝不持有数据库事务与连接，
   仅在状态流转与最终落库时开启独立短事务，防止撑爆连接池。
2. 状态机全链路追溯（State Machine & Traceability）：
   推进 PARSING -> INDEXING -> READY/FAILED，支持前端轮询实时进度，
   并在失败时自动拦截截断错误堆栈写入数据库。

【第 12 期改造：从 BackgroundTasks 迁到 Celery】
本模块从"一个同步等待的函数"拆成了三个部分：
    第一部分 任务状态管理辅助方法  —— 往 ingestion_tasks 台账表写状态与进度
    第二部分 _embed_with_progress   —— 分批向量化，每批上报一次进度
    第三部分 _run_ingest            —— 首次入库的完整主流程（原来的 ingest_document）
    最后    sync 入口              —— run_ingest_sync / run_reindex_sync，交给 Celery worker 调

【为什么要拆出第一部分】
原来的 ingest_document 只维护 documents.status 一个状态。
迁到 Celery 后，多了一层"任务"概念，而 Celery 自己的 result backend 只适合临时排查
（配置里 result_expires 之类），不适合当业务台账。所以状态真相落在 ingestion_tasks 表里，
这一组辅助方法就是唯一写它的地方 —— 每个辅助方法都开一个独立短事务，
原因与 _set_status 相同：让前端轮询能立刻看到中间态，而不是等整个流程跑完才一起提交。

【⚠️ 本模块没有 ingest_document 这个名字了】
它被 _run_ingest 取代。所有调用点（document_service / document_upload_service）
都必须改用 Celery 的 .delay()，否则模块 import 会直接失败。
"""

import asyncio
from typing import Any
from uuid import UUID

from app.core.logging import get_logger
from app.db.models import (
    DocumentChunk,
    DocumentStatus,
)
from app.db.repositories.chunk_repo import DocumentChunkRepository
from app.db.repositories.document_repo import DocumentRepository
from app.db.repositories.ingestion_task_repo import IngestionTaskRepository
from app.db.session import AsyncSessionLocal, run_worker_coro
from app.ingestion import embedder, parser, splitter
from app.storage.file_service import get_file_service

# 语法（业务日志记录器封装）：get_logger(__name__)
#   参数1 (__name__: str)：Python 原生模块内置属性，传入当前模块层级路径作为日志器标识
logger = get_logger(__name__)


# ==============================================================================
# 阶段状态流转：写 documents.status
# ==============================================================================
async def _set_status(
    document_id: UUID,
    status: DocumentStatus,
    *,
    error_message: str | None = None,
) -> None:
    """状态变更独立事务：避免长事务、保证前端轮询能立即看到中间态。"""
    """通俗讲就是：专门借几毫秒的数据库连接，把文档的新状态（比如解析中、索引中、失败）立刻提交存盘，让前端能实时看到进度，存完就马上把连接还回连接池。"""
    # 语法（SQLAlchemy 异步上下文管理器）：async with AsyncSessionLocal() as session
    #   特性：独立从连接池借出连接并开启局部短事务，退出上下文代码块时自动触发连接释放归还池中
    async with AsyncSessionLocal() as session:
        # 语法（业务仓储实例化）：DocumentRepository(session)
        #   参数1 (session: AsyncSession)：将当前局部会话注入仓储层
        repo = DocumentRepository(session)

        # 语法（业务方法调用）：执行状态字段与报错信息的原子更新
        await repo.update_status(document_id, status, error_message=error_message)

        # 语法（SQLAlchemy 异步事务提交）：await session.commit()
        #   特性：立即向数据库下推 COMMIT 指令，使得前端或外部查询能即时读取到状态变更
        await session.commit()


# ==============================================================================
# 第一部分：任务状态管理的辅助方法（写 ingestion_tasks 台账表）
# ==============================================================================
# 【设计要点：每个方法各自开一个独立短事务】
# 与 _set_status 同一思路。若把状态与进度都攒到最后一起提交，
# 前端在整个向量化期间（本项目实测几十秒）都只能看到 pending，
# 用户无法区分"在跑"和"worker 没起来"。
async def _mark_task(task_id: UUID, *, running: bool = False) -> None:
    """把任务标记为 running（真正开始执行）。"""
    async with AsyncSessionLocal() as session:
        repo = IngestionTaskRepository(session)
        if running:
            await repo.mark_running(task_id)
        await session.commit()


async def _mark_task_failed(task_id: UUID, error_message: str) -> None:
    """把任务标记为 failed 并落错误原因。"""
    async with AsyncSessionLocal() as session:
        repo = IngestionTaskRepository(session)
        await repo.mark_failed(task_id, error_message)
        await session.commit()


async def _mark_task_success(task_id: UUID) -> None:
    """把任务标记为 success（终态）。"""
    async with AsyncSessionLocal() as session:
        repo = IngestionTaskRepository(session)
        await repo.mark_success(task_id)
        await session.commit()


async def _set_task_total(task_id: UUID, total: int) -> None:
    """定下进度分母 = 切分后的总 chunk 数。"""
    async with AsyncSessionLocal() as session:
        repo = IngestionTaskRepository(session)
        await repo.set_progress_total(task_id, total)
        await session.commit()


async def _increment_task_progress(task_id: UUID, delta: int) -> None:
    """推进进度分子。用"增量"而不是"绝对赋值"，是为了让 _embed_with_progress
    不必自己维护一个计数器 —— 每批跑完加一批的条数即可。"""
    async with AsyncSessionLocal() as session:
        repo = IngestionTaskRepository(session)
        await repo.increment_progress(task_id, delta)
        await session.commit()


# ==============================================================================
# 第二部分：分批向量化 + 进度上报
# ==============================================================================
async def _embed_with_progress(
    texts: list[str], task_id: UUID
) -> list[list[float]]:
    """按 EMBEDDING_BATCH_SIZE 分批 embedding，逐批写入任务进度。

    LangChain `OpenAIEmbeddings.aembed_documents` 内部也会分批，但回调粒度藏在
    SDK 里；这里手动分批是为了让 `progress_done` 跟着每个批次走，前端轮询有连续反馈。
    """
    # 语法（函数内延迟导入）：from app.core.config import settings
    #   写在这里而不是模块顶部，避免模块级 import 在某些路径下触发循环。
    from app.core.config import settings

    # 语法（空输入短路）：空列表直接返回空结果，避免下面 range 步进时空转
    if not texts:
        return []

    embeddings_client = embedder.get_embeddings()
    # 语法（最大值兜底）：max(1, ...) 防止配置被误设为 0 导致 range 步进为 0 而抛异常
    batch_size = max(1, settings.embedding_batch_size)

    results: list[list[float]] = []
    # 语法（带步进的区间遍历）：range(0, len(texts), batch_size)
    #   产出的是每批的【起始下标】，因此下面要用 texts[start : start + batch_size] 切片
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        vectors = await embeddings_client.aembed_documents(batch)
        results.extend(vectors)
        # 语法（进度上报）：每批结束立即落库，让前端轮询看到进度在动
        await _increment_task_progress(task_id, len(batch))
    return results


# ==============================================================================
# 第三部分：首次入库主流程
# ==============================================================================
async def _run_ingest(document_id: UUID, task_id: UUID) -> None:
    """首次入库：全量解析 → 切分 → embedding → 写库。

    【参数说明】：
    - document_id (UUID): 待处理的目标文档主键 ID
    - task_id (UUID): ingestion_tasks 台账行 ID，用于回写任务状态与进度
    """
    logger.info("ingest start: document_id=%s task_id=%s", document_id, task_id)
    await _mark_task(task_id, running=True)

    try:
        # ---------------------------------------------------------------------
        # 阶段 1：查询文档（独立短事务）
        # ---------------------------------------------------------------------
        # 语法（SQLAlchemy 异步上下文管理器）：async with AsyncSessionLocal() as session
        #   特性：独立从连接池借出连接并开启局部短事务，退出上下文代码块时自动触发连接释放归还池中
        async with AsyncSessionLocal() as session:
            # 语法（业务仓储实例化）：DocumentRepository(session)
            #   参数1 (session: AsyncSession)：将当前局部会话注入仓储层
            document = await DocumentRepository(session).get_by_id(document_id)

            # 语法（空值防御与提早退出）：if document is None: return
            #   特性：若记录不存在则打 Warning 日志、把任务标为失败并退出，不触发外层异常捕获
            if document is None:
                logger.warning("document not found, skip ingest: %s", document_id)
                await _mark_task_failed(task_id, "文档不存在")
                return

            # 语法（对象属性提取与状态解耦）：提前从 ORM 实体提取标量属性值
            #   特性：由于退出 with 块后 session 会关闭，必须提前提取属性，防止后续触发 DetachedInstanceError（实体脱钩异常）
            object_key = document.cos_object_key
            filename = document.name

        # ---------------------------------------------------------------------
        # 阶段 2：下载文件（COS 原始数据读取）
        # ---------------------------------------------------------------------
        # 更新状态 ➔ PARSING：推进状态机，独立短事务提交供前端实时感知
        await _set_status(document_id, DocumentStatus.PARSING)
        # 从 COS 下载原文字节流：根据对象存储路径获取二进制文件 Payload
        content = await get_file_service().download(object_key)

        # ---------------------------------------------------------------------
        # 阶段 3：解析内容（Docling 多模态提取）
        # ---------------------------------------------------------------------
        # Docling 离线解析：解析文档版面、抽取文本正文并输出 LangChain Document 集合
        parsed = await parser.parse(filename, content)

        # ---------------------------------------------------------------------
        # 阶段 4：切分 chunks（语义分块与元数据增强）
        # ---------------------------------------------------------------------
        # 更新状态 ➔ INDEXING：推进状态机至索引分块阶段，独立短事务提交
        await _set_status(document_id, DocumentStatus.INDEXING)
        # 递归分块与增强：执行中文标点语义切分，并注入 chunk_index 序号与 chunk_hash 指纹
        chunks = splitter.split(parsed)
        if not chunks:
            # 判空校验：若切片为空抛出 ValueError，中断流程并由顶层捕获标记失败
            raise ValueError("切分后没有任何 chunk，请检查文档内容")

        # ---------------------------------------------------------------------
        # 阶段 5：生成 embeddings（批量向量化计算 + 逐批进度上报）
        # ---------------------------------------------------------------------
        # 先定进度分母：切分完成才知道总共有多少 chunk 要向量化
        await _set_task_total(task_id, len(chunks))
        # 语法（Python 原生列表推导式）：[c.page_content for c in chunks]
        #   特性：遍历切片实体集合，剥离 LangChain Document 上的元数据，仅抽取 page_content 正文字符串组成纯文本数组
        embeddings = await _embed_with_progress(
            [c.page_content for c in chunks], task_id
        )

        # ---------------------------------------------------------------------
        # 阶段 6：写入数据库（独立短事务）
        # ---------------------------------------------------------------------
        # ⚠️ 【这里的写库目前不幂等】只做 bulk_add，不先清空该文档已有的 chunks。
        #    现在之所以不出问题，是因为三条入口都在投递前保证了"该文档没有切片"：
        #      · upload / finalize_upload —— 刚建出来的新文档，本来就没有 chunks；
        #      · retry                   —— 投递前已显式 delete_by_document 清过一次。
        #    也就是说：幂等性目前是【靠外部前提撑着的】，并不是这段代码自带的。
        #    这一点与 celery_app.py 里 task_acks_late=False 直接绑定 ——
        #    正因为任务不幂等，才不敢开"跑完再 ACK"（否则重投会写出两套切片）。
        #    如果将来在这里补上"同一事务内先删旧切片、再写新切片"，
        #    任务才算真正幂等，那时才该回去重新评估 task_acks_late。
        # 开启独立的数据库短事务会话，执行批量落盘
        async with AsyncSessionLocal() as session:
            chunk_repo = DocumentChunkRepository(session)
            # 语法（仓储层批量持久化接口）：await chunk_repo.bulk_add(...)
            #   特性：一次性把装有所有切片实体的列表提交给仓储，内部执行 session.add_all()，杜绝在循环中逐条 INSERT 造成的网络往返浪费
            # 语法（Python 并行遍历 + 列表推导）：zip(chunks, embeddings)
            #   - 核心机制：像拉链一样将两个等长列表按位置一一配对：(第 0 个文本块, 第 0 个向量), (第 1 个文本块, 第 1 个向量)...
            #   - strict=True：【第 12 期新增】要求两个列表长度严格相等，不等就抛 ValueError。
            #     不加的话 zip 会"按短的截断"，一旦 embedding 少返回一条，整份文档就会
            #     【静默少存一个切片】—— 检索时表现为"某段内容永远搜不到"，极难排查。
            await chunk_repo.bulk_add(
                [
                    _make_chunk(document_id, chunk, vector)
                    for chunk, vector in zip(chunks, embeddings, strict=True)
                ]
            )
            # 语法（事务落盘提交）：执行 SQL COMMIT 指令，将数据真正物理写入磁盘并持久化
            await session.commit()

        # ---------------------------------------------------------------------
        # 阶段 7：状态收尾与异常处理 - 成功路径
        # ---------------------------------------------------------------------
        # 更新状态 ➔ READY：成功结束流水线，清空历史报错并提交事务
        await _set_status(document_id, DocumentStatus.READY, error_message=None)
        await _mark_task_success(task_id)
        logger.info("ingest done: document_id=%s chunks=%d", document_id, len(chunks))

    except Exception as exc:
        # ---------------------------------------------------------------------
        # 阶段 7：状态收尾与异常处理 - 失败路径
        # ---------------------------------------------------------------------
        # 全局异常捕获：防御性截断错误栈前 500 字符，避免撑爆数据库错误列
        logger.exception("ingest failed: document_id=%s", document_id)
        # 语法（短路或兜底文案）：str(exc).strip() or exc.__class__.__name__
        #   有些异常（如 ValueError() 不带参数）转成字符串是空的，
        #   若直接落库，前端会看到一个没有任何信息的"失败"—— 至少有类名可查。
        message = str(exc).strip() or exc.__class__.__name__
        # 文档状态与任务台账【都要写】：前者给文档列表页用，后者给任务详情卡片用。
        await _set_status(
            document_id, DocumentStatus.FAILED, error_message=message[:500]
        )
        await _mark_task_failed(task_id, message)


def _make_chunk(
    document_id: UUID, chunk: Any, embedding: list[float]
) -> DocumentChunk:
    """把「切片文本 + 对应向量」组装成一行 DocumentChunk ORM 实体。

    【为什么单独抽一个函数，而不是像改造前那样在列表推导里直接 new】
    内联写法在推导式里要写 5 个字段，一行下来几十个字符，可读性差且容易看漏字段；
    抽出来之后，字段映射关系集中在一处，将来加字段（比如第 5 节要用的
    content_hash 对齐信息）只改这里。

    【参数说明】：
    - document_id (UUID): 外键，绑定所属主文档
    - chunk: LangChain Document 对象，page_content 是正文，metadata 里带 chunk_index / chunk_hash
    - embedding (list[float]): 与该切片一一对应的稠密向量
    """
    return DocumentChunk(
        document_id=document_id,
        content=chunk.page_content,
        chunk_index=chunk.metadata["chunk_index"],
        chunk_hash=chunk.metadata["chunk_hash"],
        embedding=embedding,
    )


# ==============================================================================
# 增量重建索引（占位）
# ==============================================================================
async def _run_reindex(document_id: UUID, task_id: UUID) -> None:
    """增量重建索引：按 chunk_hash 对齐，仅对变化 chunk 重新 embedding。

    ⚠️ 本函数将在下面「5、增量索引」一节实现。
    这里先留一个显式的占位，是为了让下面的 run_reindex_sync 有一个明确的落点，
    而不是引用一个不存在的名字（那样报错会是一句很难懂的 NameError）。
    """
    raise NotImplementedError("增量索引将在下一节实现")


# ==============================================================================
# sync 入口（给 Celery worker 调）
# ==============================================================================
# 【为什么需要这一层薄薄的包装】
# 上面的业务流程全是 async，而 Celery 的任务函数必须是同步可调用对象。
# 因此在 worker 进程里起一个事件循环，把协程跑到底。
#
# 【为什么用 run_worker_coro 而不是直接 asyncio.run】
# 教程写的是 asyncio.run(...)，它在【只有一次任务】时没问题，但实测第二个任务必崩：
# asyncio.run 每次都会新建并关闭事件循环，而 worker 进程里被缓存的 I/O 资源
# （SQLAlchemy 连接池、embedder 单例里的 httpx 连接池）是跨任务复用的 ——
# 第二个任务取出来一用，底层 socket 却绑在上一个【已关闭】的循环上，
# 报出 AttributeError: 'NoneType' object has no attribute 'send'。
# run_worker_coro 用一个常驻事件循环跑所有任务，从根上让"资源与循环的绑定"一直成立。
# 完整现象、根因与并发前提见 app/db/session.py 中 run_worker_coro 的注释。
def run_ingest_sync(document_id: UUID, task_id: UUID) -> None:
    run_worker_coro(_run_ingest(document_id, task_id))


def run_reindex_sync(document_id: UUID, task_id: UUID) -> None:
    run_worker_coro(_run_reindex(document_id, task_id))
