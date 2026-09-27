"""文档预签名直传业务编排服务。

【模块职责说明】：
1. 领域用例编排（Application Service Pattern）：
   编排 UploadSession、FileService、DocumentRepository 与 ingestion 摄取流水线，封装文档直传完整生命周期。
2. 状态机流转与契约校验（State Machine & Validation）：
   承担直传全链路的状态扭转（INITIATED -> FINALIZING -> COMPLETED/FAILED/ABORTED/EXPIRED）、租期超时判定、云存储对象元数据比对与内容指纹去重。
3. 架构分层与依赖倒置（Separation of Concerns）：
   使上层 API 路由仅聚焦于 HTTP 协议解析与响应序列化，完全隔离底层数据库实体交互、存储 SDK 细节与异步后台任务调度。

【与 FileService 的边界】：
- FileService：纯底层存储适配器，专注提供 COS Key 路径规范、PUT 预签名 URL 签发、HEAD 元数据校验、流式计算哈希、对象删除等通用存储能力；
- DocumentUploadService：业务编排服务，掌控“何时触发存储能力”、“校验不一致如何降级回滚”以及“校验通过后如何创建持久化 Document 实体并派发解析任务”。

【第 12 期：整条直传链路也搬到了 Celery（教程未覆盖，本项目自己补的）】
改造前的调用关系是「进程内串行」的：
    complete_upload ──BackgroundTasks──> finalize_upload ──await──> ingest_document（跑几十秒）
改造后变成「两个 Celery 任务串联」：
    complete_upload ──.delay()──> finalize_upload（worker）
                                      └──建档 + 落任务台账──> .delay() ──> ingest_document（worker）
这样 API 进程彻底不碰重活。更重要的是：finalize 以前挂在 FastAPI 的
BackgroundTasks 上，一旦开发时 uvicorn --reload 重启，正在跑的 finalize 会连人带活
一起消失，会话永远卡在 FINALIZING；搬到 Celery 之后它由独立 worker 持有，不再受影响。
"""

from datetime import datetime, timedelta, timezone
from pathlib import PurePath
from uuid import UUID, uuid4

from qcloud_cos.cos_exception import CosClientError, CosServiceError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas.document_uploads import (
    InitUploadRequest,
    InitUploadResponse,
    UploadSessionRead,
)
from app.core.config import settings
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.tags import normalize_tags
from app.db.models import (
    Document,
    DocumentStatus,
    IngestionTaskType,
    UploadSession,
    UploadSessionStatus,
)
from app.db.repositories.document_repo import DocumentRepository
from app.db.repositories.ingestion_task_repo import IngestionTaskRepository
from app.db.repositories.upload_session_repo import UploadSessionRepository
from app.db.session import run_worker_coro
from app.ingestion.tasks import finalize_upload_task, ingest_document_task
from app.storage.file_service import FileService

# 语法（模块级常量配置）：文件后缀与标准 MIME Type 白名单映射字典
_ACCEPTED_SUFFIXES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".html": "text/html",
    ".htm": "text/html",
}

# 语法（模块级常量配置）：直传预签名 URL 默认有效生命周期（秒）
_PRESIGNED_EXPIRES_SECONDS = 300


def _resolve_upload_type(file_name: str, mime_type: str) -> tuple[str, str]:
    """按后缀白名单校验文件合法性，并解析返回服务端规范 MIME 与小写后缀。

    【参数说明】：
    - file_name (str): 客户端上传的原始文件名（包含扩展名）
    - mime_type (str): 客户端请求头宣称的 Content-Type 媒体类型

    【返回值】：
    - tuple[str, str]: (规范化后的 MIME 类型字符串, 统一小写的文件后缀)

    【异常说明】：
    - ValidationError: 当提取出的文件后缀不在系统白名单支持范围内时抛出
    """
    # 语法（标准库跨平台路径解析）：PurePath(path).suffix 提取文件扩展名，.lower() 归一化为小写
    suffix = PurePath(file_name).suffix.lower()

    # 语法（字典键成员测试）：校验提取到的扩展名是否命中系统预置白名单
    if suffix in _ACCEPTED_SUFFIXES:
        # 语法（元组构造）：返回映射的标准 MIME 与规范后缀，防止客户端伪造不安全媒体类型
        return _ACCEPTED_SUFFIXES[suffix], suffix

    # 语法（自定义业务异常抛出）：针对不合规文件类型阻断流程，提供详细拒绝上下文
    raise ValidationError(
        f"不支持的文件类型: {file_name} ({mime_type or '未知'})。"
        "当前仅支持 PDF、DOCX、Markdown、HTML"
    )


class DocumentUploadService:
    """文档直传用例服务，驱动预签名初始化、完工校验、状态推进与归档清理。"""

    def __init__(
        self,
        session: AsyncSession,
        *,
        file_service: FileService | None = None,
    ) -> None:
        """注入数据库会话和可替换的文件服务，支撑依赖倒置与单元测试隔离。

        【参数说明】：
        - session (AsyncSession): 当前工作单元内的异步数据库会话
        - * (Python 仅限关键字实参语法): 强制后续参数以命名参数形式传入
        - file_service (FileService | None): 云存储底层适配服务，未注入时默认实例化生产单例
        """
        # 语法（Python 原生实例绑定）：持久化当前请求上下文绑定的数据库会话
        self.session = session

        # 语法（仓储模式组装）：为会话与文档分别初始化独立的 Repository 实例，复用同一事务连接
        self.upload_repo = UploadSessionRepository(session)
        self.document_repo = DocumentRepository(session)

        # 【第 12 期】入库任务台账仓储：建档后要落一条 ingestion_tasks 行再投递 Celery
        self.task_repo = IngestionTaskRepository(session)

        # 语法（短路求值与默认依赖注入）：外部未提供 mock 服务时降级回退至真实云存储服务
        self.file_service = file_service or FileService()

    async def init_upload(self, payload: InitUploadRequest) -> InitUploadResponse:
        """校验上传元数据、创建 UploadSession 待命记录并向 COS 签发预签名写入凭证。

        【参数说明】：
        - payload (InitUploadRequest): 包含文件名、大小预估、权限标签等元数据的入参契约

        【返回值】：
        - InitUploadResponse: 包含会话完整信息及供前端直接 PUT 的预签名直传凭证
        """
        # 语法（配置项单位转换与阈值推算）：将配置的 MB 级容量转为纯字节上限（Byte）
        max_bytes = settings.upload_max_size_mb * 1024 * 1024

        # 语法（业务前置防御性检查）：拦截声明文件体积超出配置上限的非法请求
        if payload.size > max_bytes:
            raise ValidationError(f"文件超过 {settings.upload_max_size_mb} MB 上限")

        # 语法（内部辅助校验调用）：解析并断言文件后缀合法性，提取规整的 MIME 与后缀
        mime_type, suffix = _resolve_upload_type(payload.file_name, payload.mime_type)

        # 语法（ORM 瞬态模型构建）：组装预签名上传会话实体
        #   id: 使用全局唯一 UUID 作为会话标识
        #   original_name: 使用 PurePath 剔除绝对路径前缀，防范目录遍历安全隐患
        #   expires_at: 以 UTC 基准时间推算直传窗口过期时间点
        #   object_key: 初始化一个具有全局唯一散列前缀的临时对象路径
        upload_session = UploadSession(
            id=uuid4(),
            original_name=PurePath(payload.file_name).name,
            mime_type=mime_type,
            suffix=suffix,
            expected_size=payload.size,
            # 【第 11 期补漏】init 阶段就清洗一次：让会话里暂存的就是"干净的标签"。
            #   这样 finalize 搬过去时口径一致，也避免历史脏数据（带空格/重复项）
            #   在库里流转。两处都清洗是刻意的"双保险"，不是重复劳动。
            permission_tags=normalize_tags(payload.permission_tags or []),
            status=UploadSessionStatus.INITIATED,
            expires_at=datetime.now(timezone.utc)
            + timedelta(seconds=_PRESIGNED_EXPIRES_SECONDS),
            object_key=f"document-uploads/{uuid4()}/pending",
        )

        # 语法（仓储异步暂存）：将新会话纳入当前 Session 追踪并通过 flush 生成持久态
        await self.upload_repo.add(upload_session)

        # 语法（第三方服务异步调用）：请求 COS 存储底层按特定策略签发有时效限制的上传 URL
        object_key, presigned_url = await self.file_service.create_presigned_upload(
            upload_id=upload_session.id,
            filename=upload_session.original_name,
            mime_type=mime_type,
        )

        # 语法（属性状态同步）：将存储服务最终确定的实际云存储 Object Key 回写至会话实体
        upload_session.object_key = object_key

        # 语法（事务统一提交与实体快照刷新）：将初始化的会话持久化至物理数据库，并回读最新状态
        await self.session.commit()
        await self.session.refresh(upload_session)

        # 语法（Pydantic 模型转换与字典展开解包）：
        #   UploadSessionRead.model_validate(upload_session)：将 ORM 模型转换为输出 DTO
        #   .model_dump()：序列化为基础 Python 字典
        #   **解包结合 presigned_url：合并组装最终返回给调用方的初始化响应载荷
        return InitUploadResponse(
            **UploadSessionRead.model_validate(upload_session).model_dump(),
            presigned_url=presigned_url,
        )

    async def complete_upload(
        self,
        upload_id: UUID,
    ) -> UploadSessionRead:
        """接收前端上传完成通知，校验云端对象合规性并把收尾任务投递给 Celery。

        【参数说明】：
        - upload_id (UUID): 客户端声明已完成上传操作的目标会话主键 ID

        【返回值】：
        - UploadSessionRead: 状态流转为 FINALIZING 后的上传会话最新只读视图

        【第 12 期】原先第三个形参 `background_tasks: BackgroundTasks` 已删除：
        收尾任务改为投递给独立 Celery worker。这里有一个容易被忽略的连带影响 ——
        worker 是独立进程，`self.finalize_upload` 这个【绑定方法】再也传不过去了
        （JSON 序列化不了对象，而且另一个进程里也没有这个 self 实例）。
        所以 finalize 必须先"降级"成一个模块级可导入的任务函数
        （见文件末尾的 run_finalize_upload_sync）。

        【异常说明】：
        - ConflictError: 当会话处于不可完成状态（如非法重入或已过期）时抛出
        - ValidationError: 当对象在云存储中不存在、大小不符或 MIME 类型异常时抛出
        """
        # 语法（内部私有安全查询）：读取目标会话实体，不存在时抛出 NotFoundError
        upload_session = await self._get_session(upload_id)

        # 语法（前置时效熔断）：校验会话是否超时，若过期则就地更新状态并抛出 ConflictError
        self._ensure_not_expired(upload_session)

        # 语法（幂等性保护控制流）：若当前任务已被推入后台或已完成，直接返回当前快照，防止任务重复调度
        if upload_session.status in {
            UploadSessionStatus.FINALIZING,
            UploadSessionStatus.COMPLETED,
        }:
            return UploadSessionRead.model_validate(upload_session)

        # 语法（状态机强约束校验）：仅允许处于初始已发起（INITIATED）状态的会话继续完工流转
        if upload_session.status != UploadSessionStatus.INITIATED:
            raise ConflictError("当前上传会话状态不允许完成")

        # 语法（存储端真实性校验与异常包装）：
        #   向 COS 触发 HEAD 请求，对比实际 Content-Length 与 Content-Type 是否与声明预期严丝合缝
        try:
            await self.file_service.verify_uploaded_object(
                object_key=upload_session.object_key,
                expected_size=upload_session.expected_size,
                expected_mime_type=upload_session.mime_type,
            )
        except (CosClientError, CosServiceError, ValueError) as exc:
            # 语法（异常链链接 raise ... from exc）：将底层存储异常统一封装为应用层校验失败异常
            raise ValidationError(f"上传对象校验失败: {exc}") from exc

        # 语法（状态推进与事务提交）：将状态推入中间态 FINALIZING，向数据库提交状态锁定
        upload_session.status = UploadSessionStatus.FINALIZING
        await self.session.commit()

        # 语法（Celery 任务投递）：finalize_upload_task.delay(str(upload_id))
        #   【必须 commit 之后再 delay】：worker 拿到任务后会用自己的会话
        #   SELECT upload_sessions，看不到未提交的 FINALIZING 态就会直接 return，
        #   表现为"接口返回成功了，但文档永远没建出来"。
        #   通俗来讲：前台查验无误后把状态盖成"处理中"，再把工单投进邮筒，
        #            由独立厂区的工人去做哈希、去重与建档，这边立刻打发前端走人。
        finalize_upload_task.delay(str(upload_id))

        # 语法（DTO 映射返回）：将持久态实体映射为 API 响应 Schema
        return UploadSessionRead.model_validate(upload_session)

    async def abort_upload(self, upload_id: UUID) -> None:
        """主动取消未完成的上传会话，物理清除已残留在云端的临时对象以释放空间。

        【参数说明】：
        - upload_id (UUID): 待取消的目标上传会话主键 ID

        【异常说明】：
        - ConflictError: 当会话已经完成或正在建档时拒绝取消操作
        """
        # 语法（底层仓储查询）：通过仓储尝试检索上传会话
        upload_session = await self.upload_repo.get_by_id(upload_id)

        # 语法（防御性跳过）：会话不存在时静默忽略，保证取消操作的幂等性
        if upload_session is None:
            return

        # 语法（终态与中间态保护）：严禁中断已归档或正在落库建档的关键事务
        if upload_session.status in {
            UploadSessionStatus.COMPLETED,
            UploadSessionStatus.FINALIZING,
        }:
            raise ConflictError("当前上传会话不允许取消")

        # 语法（存储空间主动释放）：通知存储层物理删除已上传的临时 Object Key 避免冷垃圾积累
        await self.file_service.delete(upload_session.object_key)

        # 语法（会话废弃终态推进）：将状态扭转为已中止（ABORTED）并持久化提交
        upload_session.status = UploadSessionStatus.ABORTED
        await self.session.commit()

    async def finalize_upload(self, upload_id: UUID) -> None:
        """执行哈希指纹提取、秒传去重、生成 Document 实体，并把入库任务投递给 Celery。

        【参数说明】：
        - upload_id (UUID): 待正式归档入库的上传会话主键 ID

        【第 12 期改造：会话来源变了，方法体因此少了一层缩进】
        改造前它由 FastAPI 的 BackgroundTasks 直接调用，所以自己
        `async with AsyncSessionLocal()` 开了一个独立会话。
        现在它由 Celery 任务调用（见文件末尾的 run_finalize_upload_sync），
        会话改由【调用方】创建并注入 —— 这与 init / complete / abort 三个方法的约定一致：
        服务方法只使用 self.session，不自己造会话。
        """
        upload_session = await self.upload_repo.get_by_id(upload_id)
        if upload_session is None:
            return

        try:
            # 语法（流式指纹计算）：通知存储服务拉取流式数据，计算该文件的全局 SHA256 指纹
            file_hash = await self.file_service.hash_object(upload_session.object_key)

            # 语法（秒传/内容去重排查）：根据指纹在文档表中检索是否已有相同内容的记录
            existing = await self.document_repo.get_by_hash(file_hash)
            if existing is not None:
                # 语法（重复文件静默去重）：物理清理刚上传的副本对象，直接标记会话完成以实现空间节约
                await self.file_service.delete(upload_session.object_key)
                upload_session.status = UploadSessionStatus.COMPLETED
                upload_session.completed_at = datetime.now(timezone.utc)
                await self.session.commit()
                return

            # 语法（正式业务领域实体构建）：当文件内容唯一时，正式创建 Document 持久态实体
            document = Document(
                name=upload_session.original_name,
                file_hash=file_hash,
                mime_type=upload_session.mime_type,
                size=upload_session.expected_size,
                storage_provider="cos",
                cos_bucket=self.file_service.bucket,
                cos_object_key=upload_session.object_key,
                cos_region=self.file_service.region,
                status=DocumentStatus.UPLOADING,
                # 【第 11 期补漏】把 init 阶段暂存的权限标签沉淀到正式文档上。
                #
                # 【为什么不加这一行会造成安全事故（而不是"少个字段"）】
                # Document.permission_tags 的空数组语义是【公开】：
                #     permission_tags = []  →  任何登录用户都能看到并检索到这份文档
                # 于是"管理员上传时明确设了 ['hr']"的文档，会因为本行缺失而
                # 【静默变成全员可见】—— 界面上显示成功、没有任何报错，
                # 管理员根本不知道自己的保密设置没生效。
                #
                # 【为什么两处都要清洗】
                # init 阶段已用 normalize_tags 清洗过（写入 UploadSession 时），
                # 这里再清洗一次是"最后一道防线"：防止有人绕过 init 直接写库，
                # 也顺带把 JSONB 里可能存在的历史脏数据（带空格/重复项）挡在库外。
                #
                # 【类型说明】UploadSession 上是 JSONB，Document 上是 varchar[]；
                # SQLAlchemy 会按目标列类型绑定参数，这里只需给出 list[str]，
                # 不需要手工做 JSON↔数组 的转换。
                permission_tags=normalize_tags(upload_session.permission_tags or []),
            )

            # 语法（文档持久化注册）：调用 DocumentRepository 挂载新增记录并获取生成列
            #   add() 内部会 flush 一次，因此下一行就能安全地取到 document.id。
            await self.document_repo.add(document)

            # 【第 12 期】建档与任务台账【同一次 commit】。
            #   为什么要绑在一起：否则可能出现"文档建好了、任务记录没建"的半成品状态 ——
            #   前端看到一份永远没人处理的文档，而且没有任何记录能解释它为什么没动。
            task = await self.task_repo.create(document.id, IngestionTaskType.INGEST)

            # 语法（双边状态同步与事务提交）：置会话为 COMPLETED，锁定完成时间并统一 Commit
            upload_session.status = UploadSessionStatus.COMPLETED
            upload_session.completed_at = datetime.now(timezone.utc)
            await self.session.commit()

            # 语法（Celery 任务投递）：事务成功提交后，把文档移交给下游解析摄取流水线
            #   【第 12 期最关键的一处改动】改造前这里是 `await ingest_document(document.id)`
            #   —— 它会【当场】把下载、解析、切分、向量化、落库全部跑完，
            #   于是 finalize 这个"收尾任务"实际要跑几十秒，而且还是在 API 进程里跑。
            #   现在只往队列投一条消息就返回，重活交给独立 worker。
            #   ⚠️ 这正是教程漏掉的位置：教程只改了 DocumentService.upload / retry，
            #      而前端【实际走的是直传链路】，不改这里等于整个改造没生效。
            ingest_document_task.delay(str(document.id), str(task.id))

        except Exception as exc:
            # 语法（失败主动回滚与孤儿状态标记）：捕获未知异常，回滚未决变更，并将该会话标记为 FAILED 并存入错误堆栈
            await self.session.rollback()
            upload_session = await self.upload_repo.get_by_id(upload_id)
            if upload_session is not None:
                upload_session.status = UploadSessionStatus.FAILED
                # 语法（字符串截断保护）：限制错误堆栈长度不超过 2000 字符，防止溢出数据库列宽
                upload_session.error_message = str(exc)[:2000]
                await self.session.commit()
            # 语法（异常继续冒泡）：供上层监控和日志中间件捕获感知
            raise

    async def cleanup_expired(self) -> int:
        """周期性清理已超时的未完成会话，物理回收孤儿存储对象，并同步更新状态为 EXPIRED。

        【返回值】：
        - int: 本轮任务成功清理并推进为过期状态的会话总数
        """
        # 语法（集合常量定义）：定义纳入过期扫描范围的目标非正常终态及未决状态集合
        statuses = {
            UploadSessionStatus.INITIATED,
            UploadSessionStatus.FAILED,
            UploadSessionStatus.ABORTED,
            UploadSessionStatus.EXPIRED,
        }

        # 语法（批量条件检索）：调用仓储扫描截止当前 UTC 时间已超时满足条件的全部会话
        sessions = await self.upload_repo.list_expired(
            now=datetime.now(timezone.utc),
            statuses=statuses,
        )

        # 语法（遍历资源释放与脏状态推演）：逐一删除对象存储上的孤儿碎片，推进实体状态
        for upload_session in sessions:
            await self.file_service.delete(upload_session.object_key)
            upload_session.status = UploadSessionStatus.EXPIRED

        # 语法（工作单元批量提交）：外层统一提交本轮清理产生的全量更新事务
        await self.session.commit()

        # 语法（统计结果返回）：返回受影响清理条数，用于记录审计日志
        return len(sessions)

    async def _get_session(self, upload_id: UUID) -> UploadSession:
        """根据主键检索上传会话，若不存在则统一阻断并抛出 NotFoundError。

        【参数说明】：
        - upload_id (UUID): 待查找的目标上传会话主键 ID

        【返回值】：
        - UploadSession: 命中的上传会话实体持久态对象

        【异常说明】：
        - NotFoundError: 指定主键记录在数据库中不存在时抛出
        """
        # 语法（仓储底层查询）：向仓储层发起主键定位
        upload_session = await self.upload_repo.get_by_id(upload_id)

        # 语法（统一资源不存在断言）：规范 API 语义，避免返回 None 导致上层空指针异常
        if upload_session is None:
            raise NotFoundError("上传会话不存在")
        return upload_session

    async def _ensure_not_expired(self, upload_session: UploadSession) -> None:
        """断言上传会话处于有效生命周期内；若已超时则即时更新实体状态并拒绝后续操作。

        【参数说明】：
        - upload_session (UploadSession): 待进行时效判断的上传会话实体对象

        【异常说明】：
        - ConflictError: 当会话过期时间早于当前 UTC 时间时抛出
        """
        # 语法（时区感知的时间比较）：比对当前 UTC 时间是否已越过设定的过期阈值
        if upload_session.expires_at < datetime.now(timezone.utc):
            # 语法（状态就地更新与即刻事务提交）：即时锁死状态，防止高并发下继续推进后续流程
            upload_session.status = UploadSessionStatus.EXPIRED
            await self.session.commit()

            # 语法（业务冲突中断抛出）：终止业务链路继续执行
            raise ConflictError("上传会话已过期")


# ==============================================================================
# Celery sync 入口（直传链路专用）
# ==============================================================================
# 【为什么直传链路需要这一层，而旧链路不需要】
# 旧链路的入库重活（解析/切分/向量化）本来就在 pipeline 里，只要给 pipeline
# 加一个 run_ingest_sync 就够了。
# 但直传链路不一样：它的"收尾"是【服务层的一个方法】(finalize_upload)，
# 而 Celery 的任务必须是【模块级、可导入、同步】的可调用对象 ——
#   * 绑定方法 self.finalize_upload 序列化不了，另一个进程里也没有那个 self；
#   * 它本身还是 async 的，而任务函数必须同步。
# 所以这里把它包装成"模块级 async 函数 + 同步入口"两级：
#   finalize_upload_task（app/ingestion/tasks.py）
#       → run_finalize_upload_sync（本文件，同步入口）
#           → _run_finalize_upload（本文件，自己开会话）
#               → DocumentUploadService.finalize_upload（真正的业务方法）
#
# 【同步入口为什么用 run_worker_coro 而不是 asyncio.run】
# 与 pipeline.run_ingest_sync 同一个原因，而且这里正是实测【第一个】踩中的位置：
# 本例的 finalize_upload 与 ingest_document 是串联投递的两个任务，
# 前者用 asyncio.run 关掉循环后，后者复用到池中那条"属于已关闭循环"的连接，
# 直接 AttributeError: 'NoneType' object has no attribute 'send'。
# 完整根因与修法见 app/db/session.py 中 run_worker_coro 的注释。
async def _run_finalize_upload(upload_id: UUID) -> None:
    """在【独立数据库会话】中执行 finalize。

    worker 进程与 API 进程完全隔离，没有任何请求级会话可以复用，
    因此必须在这里自己开一个 —— 与 pipeline._run_ingest 的做法一致。
    """
    # 语法（延迟按需导入）：与改造前 finalize_upload 内部的写法保持一致，
    #   目的是隔离"后台异步上下文"，同时避免模块顶层形成潜在循环依赖。
    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        await DocumentUploadService(session).finalize_upload(upload_id)


def run_finalize_upload_sync(upload_id: UUID) -> None:
    """同步入口：由 Celery worker 的任务函数调用。"""
    run_worker_coro(_run_finalize_upload(upload_id))