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

from typing import Any
from uuid import UUID

# LangChain 的切片对象。
# 【为什么要重命名成 LangChainDocument】
# 本模块同时用到两个都叫 Document 的东西：
#   · app.db.models.Document        —— 数据库里的"文档主表"ORM 实体
#   · langchain_core.documents.Document —— 解析/切分产出的"文本切片"对象
# 直接都叫 Document 会撞名，所以把后者显式改成 LangChainDocument。
from langchain_core.documents import Document as LangChainDocument

from app.core.logging import get_logger
from app.db.models import (
    Document,
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
        #    📌 第 5 节复查：增量索引那条路径（_run_incremental / _run_full_rebuild）
        #    已经做到了"先删后插、同一事务"，所以 reindex_document 任务是幂等的；
        #    但【这一段（阶段 6）始终没改】，于是 ingest_document 仍然不幂等 ——
        #    这正是 task_acks_late 必须继续维持 False 的原因。
        #    哪天想让这里也变成"先删后插"，记得同时回 celery_app.py 更新那段结论。
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
# 增量重建索引（第 5 节）
# ==============================================================================
# 【增量索引要解决什么问题】
# 文档改了一小段，如果走全量重建，就得把整份文档重新解析、切分，并把【每一个切片】
# 都重新算一遍 embedding —— 而 embedding 是按量计费的，一份十万字的文档
# 只改了一个错别字，却要重新烧掉全部算力。
#
# 增量索引的思路：把 chunk_hash 当作每个切片的"身份证"。
#   · 某个 hash 在【新切片集合】里、也在【旧集合】里 → 内容没变，embedding 直接复用
#   · 只在新的里有 → 是新增内容，必须算 embedding
#   · 只在旧的有   → 内容被删了，把这一行删掉
# 于是需要算 embedding 的切片数，从"整篇的切片数"降到"真正变化的切片数"。
async def _run_reindex(document_id: UUID, task_id: UUID) -> None:
    """增量重建：按 chunk_hash 对齐，仅对变化部分重新 embedding。

    与 _run_ingest 的区别：
    - 命中的 chunk 不重新计算 embedding，仅更新 chunk_index / metadata
    - 新增 chunk 才做 embedding；progress_total 也只统计新增数量
    - 全删全插作为 hash 冲突场景的兜底
    """
    logger.info("reindex start: document_id=%s task_id=%s", document_id, task_id)
    await _mark_task(task_id, running=True)

    try:
        # 阶段 1：查文档（独立短事务）—— 与 _run_ingest 完全一致
        async with AsyncSessionLocal() as session:
            document = await DocumentRepository(session).get_by_id(document_id)
            if document is None:
                logger.warning("document not found, skip reindex: %s", document_id)
                await _mark_task_failed(task_id, "文档不存在")
                return
            object_key = document.cos_object_key
            filename = document.name

        # 阶段 2-3：下载 + 解析
        #   ⚠️ 这里下载到的是【新版本】的内容 —— 本任务被投递之前，
        #      DocumentService.reindex 已经把新文件传上 COS、
        #      并把 doc.cos_object_key 换成新 key 了。
        await _set_status(document_id, DocumentStatus.PARSING)
        content = await get_file_service().download(object_key)
        parsed = await parser.parse(filename, content)

        # 阶段 4：切分
        await _set_status(document_id, DocumentStatus.INDEXING)
        new_chunks = splitter.split(parsed)
        if not new_chunks:
            raise ValueError("切分后没有任何 chunk，请检查文档内容")

        # 阶段 5：拉出旧切片，准备按 chunk_hash 对齐
        async with AsyncSessionLocal() as session:
            old_chunks = await DocumentChunkRepository(
                session
            ).list_all_by_document(document_id)

        if _has_duplicate_hash(new_chunks):
            # 新切片集合内部就有重复 hash → 增量对齐的语义不成立
            # （见 _has_duplicate_hash 的注释），退化为「全删全插」是最稳的兜底
            logger.warning(
                "reindex fallback to full rebuild due to duplicate chunk_hash: %s",
                document_id,
            )
            await _run_full_rebuild(document_id, task_id, new_chunks)
        else:
            await _run_incremental(document_id, task_id, old_chunks, new_chunks)

        # 阶段 6：内容已经真正重建完成 → 版本号 +1
        #   ⚠️ 只有走到这里才 +1。失败路径【不】+1 ——
        #   否则前端列表里会出现"版本号变了、内容却没变"的假象。
        #   这也解释了版本号为什么不在 Service.reindex 里加：
        #   那边只是"提交任务"，还没真正重建成功。
        async with AsyncSessionLocal() as session:
            doc = await DocumentRepository(session).get_by_id(document_id)
            if doc is not None:
                doc.version += 1
            await session.commit()

        # 阶段 7：状态收尾
        await _set_status(document_id, DocumentStatus.READY, error_message=None)
        await _mark_task_success(task_id)
        logger.info("reindex done: document_id=%s", document_id)

    except Exception as exc:
        logger.exception("reindex failed: document_id=%s", document_id)
        message = str(exc).strip() or exc.__class__.__name__
        await _set_status(
            document_id, DocumentStatus.FAILED, error_message=message[:500]
        )
        await _mark_task_failed(task_id, message)


def _has_duplicate_hash(chunks: list[LangChainDocument]) -> bool:
    """预检：同一批新切片内部是否存在重复的 chunk_hash。

    【为什么必须先检这一下 —— 这是增量算法的前提条件】
    对齐逻辑的核心是拿 chunk_hash 当唯一键：
        old_by_hash = {c.chunk_hash: c for c in old_chunks}
    如果【同一批新切片】里有两个切片 hash 相同（例如文档里有完全重复的段落，
    或者极短的文档被切成了若干内容相同的小片），后果是：
      · 新的 hash 集合是用 set 建的，重复值会被【静默去重】，
        于是"本该插入 2 条"变成"只插入 1 条"，切片表数量对不上；
      · 配对时"哪个新切片对应哪条旧记录"也出现歧义。
    这种情况下增量对齐的语义已经不成立了，所以直接退化成"全删全插" ——
    宁可多花一次 embedding 的钱，也不要写出一份数量对不上的切片表。
    """
    seen: set[str] = set()
    for c in chunks:
        h = c.metadata["chunk_hash"]
        if h in seen:
            return True
        seen.add(h)
    return False


async def _run_incremental(
    document_id: UUID,
    task_id: UUID,
    old_chunks: list[DocumentChunk],
    new_chunks: list[LangChainDocument],
) -> None:
    """按 chunk_hash 对齐增删改。

    【分类规则】（整个增量索引的核心，就三句话）
      old 里有、new 里没有的 hash   →  DELETE    这段内容被删了
      new 里有、old 里也有的 hash   →  UPDATE    内容没变，只更新位置与元数据
      new 里有、old 里没有的 hash   →  INSERT    新增内容，需要算 embedding

    【为什么"内容没变"也要 UPDATE】
    常见场景：文档改了一小段，导致后面某段被挪到了别的章节、页码变了，
    但那段的文字一字未改 —— 它的 hash 不变，所以不必重算 embedding，
    可它的 chunk_index / page_no / section_path 变了。
    这几个字段是检索结果回显"这段出自哪一页、哪一章"的依据，
    不更新的话，用户点开引用会跳到错误的章节。所以只更新这些定位字段。
    """
    old_by_hash: dict[str, DocumentChunk] = {c.chunk_hash: c for c in old_chunks}
    new_hashes: set[str] = {c.metadata["chunk_hash"] for c in new_chunks}

    # ① 要删的：旧切片里 hash 已经不在新集合中的那些
    to_delete_ids: list[UUID] = [
        c.id for c in old_chunks if c.chunk_hash not in new_hashes
    ]

    # ② 把新切片分成"要插入"与"要更新"两堆
    to_insert: list[LangChainDocument] = []
    to_update: list[tuple[DocumentChunk, LangChainDocument]] = []
    for nc in new_chunks:
        h = nc.metadata["chunk_hash"]
        existing = old_by_hash.get(h)
        if existing is None:
            to_insert.append(nc)
        else:
            to_update.append((existing, nc))

    # ③ 进度分母只统计【真正需要 embedding 的新增切片】
    #    ⚠️ 不是 len(new_chunks)。前端进度条上的"已完成 / 总数"反映的是
    #    【embedding 的推进进度】，而不是"本次任务一共处理了多少切片"——
    #    命中的切片只是改几个字段、几乎不耗时，把它们算进分母
    #    会让进度条显得很慢，与真实耗时完全不成比例。
    await _set_task_total(task_id, len(to_insert))
    new_embeddings = await _embed_with_progress(
        [c.page_content for c in to_insert], task_id
    )

    # ④ 三种操作放进【同一个事务】一次提交
    #    删除、更新、插入必须原子：中途出错就整体回滚，
    #    否则会留下"旧的删了、新的还没写进去"的空档 ——
    #    那份文档会短暂地检索不到任何内容。
    async with AsyncSessionLocal() as session:
        chunk_repo = DocumentChunkRepository(session)
        await chunk_repo.delete_by_ids(to_delete_ids)

        # 更新命中切片的位置 / 段落元数据：内容（即 hash）未变，所以不动 embedding 列
        for old, nc in to_update:
            old.chunk_index = nc.metadata["chunk_index"]
            old.page_no = nc.metadata.get("page_no")
            old.section_path = nc.metadata.get("section_path")
            old.extra_metadata = nc.metadata

        await chunk_repo.bulk_add(
            [
                _make_chunk(document_id, c, vec)
                for c, vec in zip(to_insert, new_embeddings, strict=True)
            ]
        )
        await session.commit()


async def _run_full_rebuild(
    document_id: UUID,
    task_id: UUID,
    new_chunks: list[LangChainDocument],
) -> None:
    """hash 冲突场景的兜底：清空旧切片、全量 embedding 后写入。

    【它与 _run_ingest 的写库阶段长得几乎一样，为什么还要单独一个函数】
    因为两者的【语义】不同，将来会分头演化：
      · _run_ingest        是"首次入库"，那时文档里本来就不该有切片；
      · _run_full_rebuild  是"重建"，它【必须】显式 delete_by_document 清掉旧数据 ——
        这一步正是它存在的理由，也是让 reindex 任务【幂等】的关键
        （同一个任务跑第二遍不会写出两套切片）。
    分开写之后，将来谁往这里加逻辑，都不会波及首次入库那条路径。
    """
    await _set_task_total(task_id, len(new_chunks))
    embeddings = await _embed_with_progress(
        [c.page_content for c in new_chunks], task_id
    )

    async with AsyncSessionLocal() as session:
        chunk_repo = DocumentChunkRepository(session)
        await chunk_repo.delete_by_document(document_id)
        await chunk_repo.bulk_add(
            [
                _make_chunk(document_id, c, vec)
                for c, vec in zip(new_chunks, embeddings, strict=True)
            ]
        )
        await session.commit()


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
