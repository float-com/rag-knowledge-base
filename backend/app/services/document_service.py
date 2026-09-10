"""
【模块职责说明】
本模块为文档核心业务服务层（Document Service），作为文档全生命周期管理的中央调度中心，核心职责如下：

1. 业务流程总编排与外观整合：
   作为 Controller/Router 与底层细节之间的桥梁，整合数据访问层（Repository）、
   对象存储服务（FileService）与异步流水线（Ingestion Pipeline），
   对外暴露完整的业务用例：文件上传、列表分页、批量删除、失败重试与分块详情浏览。

2. 格式校验与类型安全收敛：
   维护文档格式白名单与后缀判定机制，采用“后缀优先”的双重防错策略，
   拦截不受支持的恶意或非法媒体文件，保障底层解析器（Docling）的安全运行。

3. 异步后台调度与生命周期管控：
   协调 FastAPI 的 BackgroundTasks，安全触发长耗时的文档解析与向量化后台任务；
   同时实施严格的状态机约束（如仅允许删除终态文档），确保流水线运行期的数据一致性。

通俗来讲：
这就是整个文档模块的“业务大总管”——前端发来的上传、看列表、删文档、重新入库等需求全归它管，
它负责指挥云存储存文件、使唤数据库改状态，并在后台把繁重的 AI 解析任务派发出去。
"""

import hashlib
from pathlib import PurePath
from uuid import UUID

from fastapi import BackgroundTasks, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.db.models import Document, DocumentChunk, DocumentStatus
from app.db.repositories.chunk_repo import (
    ChunkStats,
    DocumentChunkRepository,
)
from app.db.repositories.document_repo import DocumentRepository
from app.ingestion.pipeline import ingest_document
from app.storage.file_service import FileService, get_file_service

# =============================================================================
# 1. MIME 类型与文件后缀双向映射白名单
# =============================================================================

# 语法（类型标注字典常量）：dict[str, str]
#   特性：维护受支持的合法 MIME-Type 到标准文件后缀名（包含点号）的正向映射表
#   通俗来讲：这是“类型认后缀”的对照表，如果知道浏览器给的文件类型，就能查出它应该叫什么后缀名。
_ACCEPTED_MIME_TYPES: dict[str, str] = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "text/markdown": ".md",
    "text/x-markdown": ".md",
    "text/html": ".html",
    "application/xhtml+xml": ".html",
}

# 语法（类型标注字典常量）：dict[str, str]
#   特性：维护受支持的标准文件后缀名（全小写）到标准 MIME-Type 的反向映射表，包含同义扩展名（如 .htm、.markdown）
#   通俗来讲：这是“后缀查类型”的对照表，如果只知道文件名结尾（如 .md），就能反查出对应的官方网络数据类型。
_ACCEPTED_SUFFIXES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".html": "text/html",
    ".htm": "text/html",
}


# =============================================================================
# 文件格式与 MIME 判定核心辅助函数
# =============================================================================
# 语法（私有辅助函数定义）：def _resolve_mime_and_suffix(file: UploadFile) -> tuple[str, str]
#   命名规范：以单下划线 `_` 开头，声明为本模块内部私有逻辑，外部路由或上层服务严禁跨层直接引用
#   参数说明：file (UploadFile) -> FastAPI 封装的文件上传对象，底层包装临时文件流（小文件放内存，大文件自动溢出写本地临时磁盘防撑爆）
#     * 内置属性与方法支持：
#       1. file.filename: 上传文件的原始名称（如 "resume.pdf"）
#       2. file.content_type: 客户端/浏览器上报的 MIME 媒体类型（如 "application/pdf"）
#       3. await file.read(): 异步读取文件二进制原始字节流（Payload），供后端直接流式处理或转存
#   返回规范：返回类型标注为 tuple[str, str]，解包即得到 (标准 MIME 类型, 小写标准化后缀)
#   通俗来讲：这是本模块内部专属的“格式安检员”，它拿着 FastAPI 打包好的文件对象（里面自带了文件名、文件类型和异步读文件的工具），
#            专门给传进来的文件验明正身，外面的接口不能随便越级调用它。
def _resolve_mime_and_suffix(file: UploadFile) -> tuple[str, str]:
    """根据 UploadFile 的 content_type 和文件后缀名共同判定最终格式。

    【核心机制与优先级判定】：
    - 浏览器上传 .md 或部分文本文件时常会给出通用的 `application/octet-stream`（二进制流），
      若单纯依赖 content_type 会导致误判拦截；
    - 因此本函数实施“后缀名优先（Suffix First）”策略，后缀命中即校验通过；
    - 若后缀未命中，再降级去匹配其 MIME 类型。

    【返回值说明】：
    - tuple[str, str]: 二元组 (规范化的 MIME-Type, 规范化的后缀扩展名 如 '.md')
    """
    # 语法（跨平台路径解析）：PurePath(file.filename or "").suffix.lower()
    #   特性：防御 filename 为 None 的边界情况，提取末尾扩展名并统一转全小写（如 '.PDF' -> '.pdf'）
    #   通俗来讲：不管传上来的文件名多奇怪或者后缀是不是大写，都把最后那截后缀名揪出来并全换成小写。
    suffix = PurePath(file.filename or "").suffix.lower()

    # 语法（字典键包含性检查）：if suffix in _ACCEPTED_SUFFIXES
    #   特性：优先校验后缀名白名单；若命中，直接从映射表取出规范 MIME 并返回
    #   通俗来讲：先看文件后缀是不是我们支持的，如果是（比如 .docx），直接敲定通过，并配对好它对应的网络类型返回。
    if suffix in _ACCEPTED_SUFFIXES:
        return _ACCEPTED_SUFFIXES[suffix], suffix

    # 语法（默认值兜底提取）：file.content_type or ""
    #   特性：防范 content_type 属性可能返回 None 的极端情况
    #   通俗来讲：如果看后缀没认出来，就去拿浏览器上报的文件类型；要是浏览器连类型都没给，就兜底当成空字符串防报错。
    mime = file.content_type or ""

    # 语法（字典键包含性检查）：if mime in _ACCEPTED_MIME_TYPES
    #   特性：降级匹配 MIME 类型白名单；若命中，获取对应的标准后缀名并返回
    #   通俗来讲：降级看看浏览器报的类型在不在白名单里，在的话也能救回来，顺便把标配的后缀名一起打包带走。
    if mime in _ACCEPTED_MIME_TYPES:
        return mime, _ACCEPTED_MIME_TYPES[mime]

    # 语法（主动抛出业务领域异常）：raise ValidationError(...)
    #   特性：当后缀与 MIME 均未命中合法白名单时触发，携带错误上下文供外层全局异常处理器格式化为 HTTP 400
    #   通俗来讲：后缀和网络类型两个表都没查到，说明传了个不支持的格式（比如 exe 或 mp4），直接掀桌子报错告诉用户“不支持这破玩意”。
    raise ValidationError(
        f"不支持的文件类型: {file.filename} ({mime or '未知'})。"
        "当前仅支持 PDF、DOCX、Markdown、HTML"
    )

# =============================================================================
# 2.文档删除操作受支持的状态白名单
# =============================================================================
# 语法（Python 原生不可变冻结集合）：frozenset({...})
#   特性：
#     1. 物理不可变性：区别于普通 set，frozenset 一旦创建严禁动态 add/remove，真正实现常量安全防护；
#     2. 哈希查找效率：底层基于哈希表实现，在执行 `status in _DELETABLE_STATUSES` 包含性断言时达到 O(1) 极速校验；
#   业务状态约束策略（状态机安全锁）：
#     - 允许删除：终态（READY 成功入库、FAILED 失败放弃）以及初始态（UPLOADING 尚未开始切分落盘）；
#     - 绝对禁止：PARSING（文档解析中）、INDEXING（向量化与写库中）等中间运行态。
#       原因：此时后台异步任务 BackgroundTasks 正在高频对切片表执行写入/落盘事务，
#             强行并发删除文档会导致主外键孤儿数据、死锁或引发不可控的数据写写竞争冲突。
#   通俗来讲：这是文档删除的“免死金牌/安全白名单”——只能删已经办完事的、办砸了的和还没动工的；
#            要是后台正热火朝天切分向量（PARSING/INDEXING）时你去删，数据库两边打架非崩溃不可，所以用不可变集合死死锁住。
_DELETABLE_STATUSES: frozenset[DocumentStatus] = frozenset(
    {
        DocumentStatus.READY,
        DocumentStatus.FAILED,
        DocumentStatus.UPLOADING,
    }
)

# =============================================================================
# 3.Service 主体 - 初始化与上传
# =============================================================================

#模块日志器初始化
# 语法（获取模块专属日志器）：get_logger(__name__)
#   特性：使用内置 __name__ 变量为当前模块绑定独立的 Logger 命名空间，便于按调用链精确定位日志源
#   通俗来讲：给当前文件办个专属的“广播喇叭”，打印日志时一眼就能看出是 document_service 发出的。
logger = get_logger(__name__)


# =============================================================================
# 文档领域服务主类
# =============================================================================
class DocumentService:
    """文档生命周期业务服务层。

    负责封装文档的上传入库、秒传幂等判定、对象存储交互以及异步任务派发等全套业务逻辑。
    """

    def __init__(
        self,
        session: AsyncSession,
        file_service: FileService | None = None,
    ) -> None:
        """初始化 DocumentService 实例。

        【参数说明】：
        - session: 当前请求生命周期内的 SQLAlchemy 异步数据库会话
        - file_service: 可选的对象存储服务实例；若未显式传入，则通过工厂函数自动获取

        【内部装配机制】：
        - 挂载底层仓储层（DocumentRepository、DocumentChunkRepository）以处理单表 CRUD；
        - 使用短路或表达式绑定 FileService，保障外部单测时可自由传入 Mock 对象。
        """
        # 语法（会话上下文绑定）：self.session = session
        #   特性：持久化持有外部注入的数据库异步会话，确保同一 Service 调用范围内的事务一致性
        #   通俗来讲：把这次 HTTP 请求分配的数据库通道保存起来，后面写库、提交事务都用它。
        self.session = session

        # 语法（仓储层实例化装配）：DocumentRepository(session)
        #   特性：将数据库连接句柄注入文档主表仓储层
        #   通俗来讲：招募一个专门操作 documents 文档主表的“数据库账房先生”。
        self.repo = DocumentRepository(session)

        # 语法（仓储层实例化装配）：DocumentChunkRepository(session)
        #   特性：将数据库连接句柄注入文档切片表仓储层
        #   通俗来讲：招募一个专门操作 document_chunks 切片表的“数据库账房先生”。
        self.chunk_repo = DocumentChunkRepository(session)

        # 语法（默认值兜底与依赖注入）：file_service or get_file_service()
        #   特性：若外部未手动传入云存储服务实例，则自动触发内部工厂函数完成实例化
        #   通俗来讲：有现成的云存储工具就直接用，没有就临时造一个（便于写单元测试时换成假对象）。
        self.file_service = file_service or get_file_service()

    async def upload(
        self,
        file: UploadFile,
        background_tasks: BackgroundTasks,
    ) -> Document:
        """处理文档上传核心用例。

        【业务阶段流转】：
        1. 格式校验：双向比对后缀与 MIME，拦截非法类型；
        2. 大小校验：读取字节流并严格限制在配置阈值以内；
        3. 秒传幂等：计算 SHA-256 哈希指纹，若已有完全相同的文档则直接复用；
        4. 对象上云：写入腾讯云 COS 存储桶并获取 Object Key；
        5. 元数据持久化：生成主表 Document 记录并提交事务；
        6. 异步任务编排：将 document_id 投递至后台任务队列，解耦繁重的 AI 解析切分流水线。
        """
        # ---------------------------------------------------------------------
        # 阶段 1：格式安检（MIME 与后缀校验）
        # ---------------------------------------------------------------------
        # 语法（私有解包断言）：_resolve_mime_and_suffix(file)
        #   特性：执行“后缀优先”的双重解析，不合法时内部直接抛出 ValidationError (HTTP 400)
        #   通俗来讲：先找安检员看文件后缀和网络类型对不对，不支持的格式当场打回。
        mime_type, suffix = _resolve_mime_and_suffix(file)

        # ---------------------------------------------------------------------
        # 阶段 2：数据读取与尺寸安检
        # ---------------------------------------------------------------------
        # 语法（异步流式读取）：await file.read()
        #   特性：从 FastAPI UploadFile 缓冲中完整抽取二进制原始字节流
        #   通俗来讲：把用户传上来的文件内容一口气完整读进内存里。
        content = await file.read()

        # 语法（单位换算）：settings.upload_max_size_mb * 1024 * 1024
        #   特性：将全局配置中的 MB 单位换算为字节数（Bytes）
        #   通俗来讲：把系统配置的最大 MB 数（比如 20MB）乘两次 1024 换算成字节数，方便做对比。
        max_bytes = settings.upload_max_size_mb * 1024 * 1024

        # 语法（空内容防御）：len(content) == 0
        #   特性：防止用户上传 0 字节的空文件损坏下游解析器
        #   通俗来讲：要是传了个 0 字节的空文件，直接报错拦截，不让它进系统浪费资源。
        if len(content) == 0:
            raise ValidationError("上传文件为空")

        # 语法（超限防御）：len(content) > max_bytes
        #   特性：保障文件尺寸不超出系统承受阈值
        #   通俗来讲：要是文件比配置的最大上限还要大，果断掀桌子报错。
        if len(content) > max_bytes:
            raise ValidationError(
                f"文件超过 {settings.upload_max_size_mb} MB 上限"
            )

        # ---------------------------------------------------------------------
        # 阶段 3：计算文件哈希与秒传幂等检测
        # ---------------------------------------------------------------------
        # 语法（内置加密哈希计算）：hashlib.sha256(content).hexdigest()
        #   特性：为文件全部二进制内容计算全局唯一的 64 位十六进制 SHA-256 摘要指纹
        #   通俗来讲：给文件算一个唯一的“数字指纹”，只要内容有 1 个标点不一样，算出来的这串码就彻底不同。
        file_hash = hashlib.sha256(content).hexdigest()

        # 语法（仓储层条件查询）：await self.repo.get_by_hash(file_hash)
        #   特性：通过哈希索引快速反查数据库是否已存在同源文件
        #   通俗来讲：拿着指纹去数据库搜一搜，看看以前有没有人上传过一模一样的文件。
        existing = await self.repo.get_by_hash(file_hash)

        # 语法（幂等短路返回）：if existing is not None
        #   特性：命中哈希即实现“秒传”，跳过上传云存储与耗时的解析切分，杜绝存储与算力浪费
        #   通俗来讲：如果数据库里早就有了，直接把旧记录拿出来返回给前端，实现秒传，既省钱又省时间。
        if existing is not None:
            logger.info("file_hash hit, reuse document: %s", existing.id)
            return existing

        # ---------------------------------------------------------------------
        # 阶段 4：上传到对象存储（COS）
        # ---------------------------------------------------------------------
        # 语法（异步云存储转存）：await self.file_service.upload(...)
        #   特性：将二进制流、哈希、后缀和媒体类型上传至腾讯云 COS 存储桶，返回生成的对象寻址 Key
        #   通俗来讲：把正文内容和后缀推给腾讯云对象存储存好，拿到一个用来定位下载的存储路径（object_key）。
        object_key = await self.file_service.upload(
            content=content,
            file_hash=file_hash,
            suffix=suffix,
            mime_type=mime_type,
        )

        # ---------------------------------------------------------------------
        # 阶段 5：持久化文档元数据至数据库
        # ---------------------------------------------------------------------
        # 语法（ORM 实体组装）：Document(...)
        #   特性：构造文档主表实体，初始状态标记为 UPLOADING（正在入库准备中）
        #   通俗来讲：把文件的名字、大小、指纹、云存储地址以及初始状态（UPLOADING）打包成数据库的一行记录。
        document = Document(
            name=file.filename or f"{file_hash}{suffix}",
            file_hash=file_hash,
            mime_type=mime_type,
            size=len(content),
            storage_provider="cos",
            cos_bucket=self.file_service.bucket,
            cos_object_key=object_key,
            cos_region=self.file_service.region,
            status=DocumentStatus.UPLOADING,
        )

        # 语法（仓储层入库挂载）：await self.repo.add(document)
        #   特性：将新文档模型附加至当前会话中
        #   通俗来讲：告诉数据库准备保存这一条新文档。
        await self.repo.add(document)

        # 语法（事务提交与持久化落盘）：await self.session.commit()
        #   特性：触发数据库 COMMIT 操作，释放当前短事务连接
        #   通俗来讲：盖章确认，把文档数据真正物理写入数据库磁盘。
        await self.session.commit()

        # 语法（实体状态刷新）：await self.session.refresh(document)
        #   特性：从数据库重新拉取该记录（自动回填数据库自增或由数据库端生成的主键 id、创建时间等字段）
        #   通俗来讲：让数据对象和数据库同步一下，把数据库刚生成的 ID 和时间戳拉回内存里。
        await self.session.refresh(document)

        # ---------------------------------------------------------------------
        # 阶段 6：调度异步后台任务（Ingestion Pipeline）
        # ---------------------------------------------------------------------
        # 语法（FastAPI 后台任务投递）：background_tasks.add_task(...)
        #   核心机制：
        #     1. 严禁在 commit 前调度：后台流水线内部会开辟独立全新的数据库 Session 查询该记录，
        #        若未 commit 提前投递，后台任务可能因为读未提交而查不到文档抛出 404；
        #     2. 非阻塞响应：注册后台任务后，upload 方法立即结束并向前端返回 HTTP 200，
        #        解析、切分、向量化等重活在后台默默异步执行。
        #   通俗来讲：等数据库真正落盘确认后，派发后台小工去执行耗时的 AI 提取切片任务；
        #            这样接口不用等漫长的 AI 计算，能瞬间给前端返回成功，体验极快。
        background_tasks.add_task(ingest_document, document.id)

        return document


    # =========================================================================
    # 单文档详情检索接口
    # =========================================================================
    async def get(self, document_id: UUID) -> Document:
        """根据文档全局唯一标识符（UUID）查询单条文档详情。

        【业务判定规则】：
        - 委托文档主表仓储层按主键查找；
        - 若对应主键无任何记录返回，则主动抛出 NotFoundError 业务异常（映射为 HTTP 404）；
        - 若记录存在，则直接向调用方透传对应的 ORM Document 实体。

        【参数说明】：
        - document_id (UUID): 目标文档的主键 UUID 标识符

        【异常说明】：
        - NotFoundError: 当数据库未查到匹配文档时触发
        """
        # 语法（异步主键精确查找）：await self.repo.get_by_id(document_id)
        #   特性：调用仓储层的非阻塞主键检索，底层执行对应的 SQL WHERE id = :document_id 语句
        #   通俗来讲：拿着文档的身份证号（UUID）去数据库里查这一行记录。
        doc = await self.repo.get_by_id(document_id)

        # 语法（空值存在性拦截）：if doc is None
        #   特性：针对空结果主动触发领域异常，配合全局异常处理器返回标准 404 响应
        #   通俗来讲：如果数据库翻了个遍都没找到，直接掀桌子报错“文档不存在”，告诉前端这是 404 错误。
        if doc is None:
            raise NotFoundError("文档不存在")

        # 语法（实体直接透传）：return doc
        #   特性：返回成功命中的持久化 Document 对象
        #   通俗来讲：找到了就大大方方把文档的所有信息打包交出去。
        return doc

    # =========================================================================
    # 文档多条件分页查询接口
    # =========================================================================
    async def list_documents(
        self,
        page: int,
        page_size: int,
        *,
        status: DocumentStatus | None = None,
    ) -> tuple[list[Document], int]:
        """按分页参数和状态过滤条件批量查询文档列表。

        【核心机制】：
        - 支持基于页码（page）与每页容量（page_size）的标准物理分页（OFFSET / LIMIT）；
        - 支持按文档生命周期状态（status，如 READY/FAILED/UPLOADING）进行可选过滤；
        - 返回标准二元组结构，分别提供“当前页记录切片”与“符合条件的记录总数”，便于前端分页控件渲染。

        【参数说明】：
        - page (int): 当前请求的页码序号（通常从 1 开始计）
        - page_size (int): 每页展示的最大条目数量限制
        - * (语法强制分隔符): 强制要求其后的 status 必须作为关键字参数传递，避免位置传参混淆
        - status (DocumentStatus | None): 可选的状态过滤枚举，为 None 时表示不限制状态全量查询

        【返回值说明】：
        - tuple[list[Document], int]: 包含 (文档实体列表, 匹配记录总行数) 的二元组
        """
        # 语法（仓储层分页代理）：await self.repo.list_paginated(...)
        #   特性：将物理分页偏移量与过滤条件统一下推至仓储层与底层 SQL 引擎执行
        #   通俗来讲：指挥仓储层账房先生去翻账本，把第几页、每页几条、想要什么状态的文档捞出来，连同总条数一起打包带回。
        return await self.repo.list_paginated(page, page_size, status=status)

    # =========================================================================
    # 文档删除操作接口（DB 优先 + COS 容错策略）
    # =========================================================================
    async def delete(self, document_id: UUID) -> None:
        """删除文档记录及其在云存储中的物理对象。

        【执行顺序与容错设计哲学】：
        - 采用“先删 DB，再删 COS”的策略；
        - 原因：数据库是业务真相之源（Single Source of Truth）。若先删 COS 再删 DB，
          一旦 DB 阶段报错回滚，会导致数据库记录指向一个已不存在的物理对象（产生严重业务幽灵）；
        - 反过来，先删 DB 成功后，该文档在用户视角下已彻底消失；即便后续删除 COS 失败，
          也仅仅是在存储桶留下了一个待清理的孤儿对象（打 warning 日志，由定时批处理清理即可），
          用户体验和业务一致性均更连贯。

        【状态机约束】：
        - 仅允许删除处于 `_DELETABLE_STATUSES` 白名单中的文档（READY, FAILED, UPLOADING）；
        - 处于 PARSING 或 INDEXING 中间态的文档禁止删除，防范与 BackgroundTasks 的写冲突。
        """
        # 语法（主键检索与存在性断言）：await self.repo.get_by_id(document_id)
        #   特性：校验目标文档是否存在，缺失则主动抛出 NotFoundError 映射为 HTTP 404
        #   通俗来讲：先去数据库翻一下，看看要删的文档在不在，不在直接报 404“查无此人”。
        doc = await self.repo.get_by_id(document_id)
        if doc is None:
            raise NotFoundError("文档不存在")

        # 语法（集合成员检查）：if doc.status not in _DELETABLE_STATUSES
        #   特性：利用不可变白名单拦截处于解析、向量化过程中的中间态任务
        #   通俗来讲：如果后台还在紧锣密鼓地切片或写向量，坚决不让删，免得数据库两头打架打乱账。
        if doc.status not in _DELETABLE_STATUSES:
            raise ValidationError("文档处理中，请等待完成或失败后再删除")

        # 语法（变量暂存）：object_key = doc.cos_object_key
        #   特性：在执行 ORM 删除之前将物理存储寻址路径提取出来，防止记录删除后上下文丢失
        #   通俗来讲：先把云存储路径拿在手里记下来，因为等会数据库记录删了就查不到了。
        object_key = doc.cos_object_key

        # 语法（仓储层标记删除）：await self.repo.delete(doc)
        #   特性：将文档主表实体从数据库会话中标记为待删除状态
        #   通俗来讲：通知数据库账房先生把这笔记录划掉。
        await self.repo.delete(doc)

        # 语法（事务提交）：await self.session.commit()
        #   特性：持久化确认 DB 级删除，彻底对所有前端与上层业务屏蔽该数据
        #   通俗来讲：盖章生效，数据库里这篇文档已经彻底没有了。
        await self.session.commit()

        # 语法（物理云文件清理）：await self.file_service.delete(object_key)
        #   特性：向腾讯云 COS 发送对象清理请求；即使偶发网络超时，也只会在底层打 warning，不影响主流程已删除的事实
        #   通俗来讲：回过头去把腾讯云上存的实际文件删掉；就算腾讯云接口偶尔抽风没删掉，也就多占点空间，用户那边已经感知不到它了。
        await self.file_service.delete(object_key)
        logger.info("document deleted: id=%s", document_id)

    # =========================================================================
    # 文档失败重试接口
    # =========================================================================
    async def retry(
        self,
        document_id: UUID,
        background_tasks: BackgroundTasks,
    ) -> Document:
        """针对处理失败的文档重新触发异步解析入库流水线。

        【业务核心防线与细节处理】：
        - 仅允许对状态明确为 FAILED 的文档触发重试；
        - 防御性清理残留切块（chunks）：虽然理论上失败的文档不应该有 chunks，
          但若当初任务是在“写 chunk 事务提交的一半”时崩溃，底层表中可能已污染了残余切片；
          因此在重跑前必须显式执行清理，杜绝产生脏分块或重复向量数据；
        - 重置状态为 UPLOADING 并清空原有的 error_message，随后重新投递到后台流水线。
        """
        # 语法（主键精确查找）：await self.repo.get_by_id(document_id)
        #   特性：确保目标重试文档客观存在
        #   通俗来讲：找一找这个要重试的文档在哪，找不到就报 404。
        doc = await self.repo.get_by_id(document_id)
        if doc is None:
            raise NotFoundError("文档不存在")

        # 语法（枚举精确状态断言）：if doc.status != DocumentStatus.FAILED
        #   特性：强制业务只对失败终态开放重试，防止对处理中或已成功的文档误触产生重复计算
        #   通俗来讲：只有明确办砸了（FAILED）的文档才能申请重来，正常在跑或者已经搞定的不准乱试。
        if doc.status != DocumentStatus.FAILED:
            raise ValidationError("仅失败状态的文档支持重试")

        # 语法（级联数据防御性清洗）：await self.chunk_repo.delete_by_document(document_id)
        #   特性：彻底抹除上次执行时可能留下的脏切块数据，避免后续追加写入导致数据重叠翻倍
        #   通俗来讲：打扫战场，把上次失败可能残留的半成品切片碎片统统扫地出门。
        await self.chunk_repo.delete_by_document(document_id)

        # 语法（属性状态回滚）：doc.status = DocumentStatus.UPLOADING / doc.error_message = None
        #   特性：将文档生命周期回滚至初始入库排队状态，同时擦除旧的报错堆栈信息
        #   通俗来讲：把状态重新调回“准备入库”，把以前的报错日志擦干净，给它一次改过自新的机会。
        doc.status = DocumentStatus.UPLOADING
        doc.error_message = None

        # 语法（事务提交流水线更新）：await self.session.commit()
        #   特性：立刻提交状态变更，确保后续独立的后台任务协程能读到 UPLOADING 的最新状态
        #   通俗来讲：先把这次状态改动落盘记在数据库里。
        await self.session.commit()
        await self.session.refresh(doc)

        # 语法（派发异步后台任务）：background_tasks.add_task(ingest_document, doc.id)
        #   特性：在事务提交安全落盘后，重新唤起异步流水线
        #   通俗来讲：派工单给后台流水线，通知小工“带上这个文档的 ID，重新给我跑一遍 AI 切片和入库”。
        background_tasks.add_task(ingest_document, doc.id)
        logger.info("document retry scheduled: id=%s", document_id)
        return doc

    # =========================================================================
    # 文档分块（Chunk）浏览与统计接口
    # =========================================================================
    async def list_chunks(
        self,
        document_id: UUID,
        page: int,
        page_size: int,
    ) -> tuple[list[DocumentChunk], int, ChunkStats | None]:
        """分页获取指定文档的所有文本切块明细及切块统计信息。

        【核心机制】：
        - 先调用 self.get 验证父文档是否存在，防御空文档与“文档不存在”两类边界混淆；
        - 并行下推两个仓储操作：物理分页查询指定切片列表、聚合统计总切块数与平均字符数；
        - 返回 (切片列表, 总数, 汇总统计指标)，供前端全景展示切片质量。
        """
        # 语法（先行存在性守卫）：await self.get(document_id)
        #   特性：借助 get 方法自带的 NotFoundError 校验机制，防止用户拿不存在的 ID 查询产生误导性的空列表
        #   通俗来讲：先查这个主文档到底存不存在，避免把“文档根本没有”和“文档有但还没切块”这两种情况搞混了。
        await self.get(document_id)

        # 语法（仓储层分页查询）：await self.chunk_repo.list_paginated_by_document(...)
        #   特性：按所属文档的外键 ID 进行针对性分页抽取
        #   通俗来讲：去切片库里把第几页的文本块捞出来，同时数一数一共切出了多少块。
        items, total = await self.chunk_repo.list_paginated_by_document(
            document_id, page, page_size
        )

        # 语法（聚合指标统计）：await self.chunk_repo.get_stats(document_id)
        #   特性：拉取切块汇总分析（如平均 Token 数、分块总容量等），若无切块则为 None
        #   通俗来讲：顺便查一下这堆切块的体检报告（比如平均切了多少字、整体指标怎么样）。
        stats = await self.chunk_repo.get_stats(document_id)
        return items, total, stats

    async def get_chunk(
        self,
        document_id: UUID,
        chunk_id: UUID,
    ) -> DocumentChunk:
        """根据文档 ID 与切片 ID 获取唯一的切块详情。

        【双主键防御机制】：
        - 查询时同时要求匹配 document_id 与 chunk_id，防范跨文档越权越界读取；
        - 未命中时抛出 NotFoundError。
        """
        # 语法（组合唯一校验查询）：await self.chunk_repo.get_for_document(document_id, chunk_id)
        #   特性：双重约束下推，确保所查切片确实属于传入的目标文档
        #   通俗来讲：拿着文档 ID 和切片 ID 两把钥匙去开锁，必须两个都对上才把切片内容拿出来。
        chunk = await self.chunk_repo.get_for_document(document_id, chunk_id)

        # 语法（空结果抛错）：if chunk is None
        #   特性：切块不存在时返回 404 业务错误
        #   通俗来讲：要是没找到对应的切块，直接掀桌子报错“该切片不存在”。
        if chunk is None:
            raise NotFoundError("Chunk 不存在")
        return chunk