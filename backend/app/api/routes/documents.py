"""
【模块职责：API 传输与路由控制层 (Transport / Controller Layer)】

1. 核心定位：
   作为文档模块对外暴露的 RESTful API 统一路由中心，涵盖文档主表 CRUD、
   底层文件流下载/预览，以及切块（Chunk）多级检索与质量统计。
   严格承担协议解析、参数安检、依赖注入调度与 DTO 序列化脱敏，不编写具体持久化与算法。

2. 核心架构与设计亮点：
   - 依赖注入统一管控：通过 DbSession 管理异步会话生命周期，利用 BackgroundTasks 承接长耗时异步流水线；
   - 协议与状态码严格对齐：
     * 新建落库 201 Created，常规查询 200 OK，删除 204 No Content；
     * 文件流采用原生二进制 Response 响应，遵循 RFC 5987 标准化文件名编码彻底杜绝中文乱码；
   - 响应与渲染策略分流：
     * 对 PDF/HTML/Markdown 开放 inline 浏览器原位预览；
     * 对不可直接渲染的 DOCX 强制转 attachment 下载，保障客户端交互一致性；
   - 阶梯式数据安检与防爆：
     * 出参 DTO 严格隐蔽底层 COS 存储桶、物理路径与高维 Embedding 向量；
     * 切片列表强制裁剪为 100 字符摘要避免网络拥塞，单切片详情接口方交付完整正文；
     * 单切片查询实施 document_id 与 chunk_id 级联双校验，防止越权拉取。

通俗来讲：
这是整个文档系统的“前台接待大厅与安检海关”——
负责查验客户身份证（UUID 与分页参数），指挥后台总管（Service）去干活或搬文件，
给文件贴上防乱码标签，对发给前端的数据严密搜身（剥除云存储路径与庞大向量），
看切片时先给 100 字缩略卡片，要看全文才递完整切片，既规范又安全。
"""

# =============================================================================
# 模块导入与环境初始化
# =============================================================================
from urllib.parse import quote
from uuid import UUID

import json

from fastapi import (
    APIRouter,
    BackgroundTasks,
    File,
    Form,
    Header,
    Query,
    Response,
    UploadFile,
)

from app.api.deps import CurrentAdmin, CurrentUser, DbSession, get_current_user
from app.api.schemas.documents import (
    DocumentChunkDetail,
    DocumentChunkListResponse,
    DocumentChunkRead,
    DocumentChunkStats,
    DocumentListResponse,
    DocumentPermissionTagsUpdate,
    DocumentRead,
    DocumentStatusValue,
)
from app.db.models import DocumentStatus, User
from app.services.document_service import DocumentService
from app.services.permission_service import compute_user_permission_tags, is_admin

# 语法（APIRouter 路由分组）：APIRouter(prefix="/documents", tags=["documents"])
#   特性：统一定义路由前缀为 `/documents`，并打上 `documents` 标签便于 OpenAPI / Swagger 聚合展示
#   通俗来讲：给所有文档操作接口划定一个统一的对外入口大门，自动加上 `/documents` 门牌号。
router = APIRouter(prefix="/documents", tags=["documents"])


# =============================================================================
# 权限辅助函数（第 11 期）
# =============================================================================
def _viewer_tags(user: User) -> list[str] | None:
    """把"当前用户"换算成"该传给仓储的权限标签"。

    :param user: 当前登录用户
    :return: 普通用户返回其有效权限标签列表；admin 返回 **None**

    【⭐ 返回 None 而不是 ["*"] 是关键设计】
    仓储层把 None 解释为"**不做权限过滤**"（`build_permission_filter` 直接返回 None，
    连 SQL 条件都不拼）。若这里返回 `["*"]`，仓储还要再判断一次"是不是通配" ——
    多一次字符串比较，而且会把"通配"这个业务概念扩散到仓储里。

    在**这一处**做转换（admin → None），等于把业务语义翻译成通用信号，
    仓储只需要理解"None = 不过滤"这一件事。这也正是第 11 期把权限常量
    提到 `core/permissions.py` 之外的另一种"分层收敛"手法：
    - 常量共享 → 放 core
    - 语义翻译 → 放在最靠近"知道用户是谁"的那一层（本函数所在的路由层）

    【为什么 admin 不需要过滤】
    admin 视角等价于"看全量"。这不是"额外给权限"，而是"不加限制" ——
    因此传 None 比传一个包含所有标签的列表更快也更准确
    （后者还得随新标签同步维护）。
    """
    return None if is_admin(user) else compute_user_permission_tags(user)

# =============================================================================
# 阶段 1:文档基础操作 API（增、查、删、重试）
# =============================================================================

# =============================================================================
# 1. 文档上传接口
# =============================================================================
# 语法（路由装饰器配置）：
#   - status_code=201: 显式声明符合 RFC 规范的“新建资源成功”状态码
#   - response_model=DocumentRead: 自动过滤底层敏感信息，仅输出安全 DTO
#   - operation_id="uploadDocument": 显式指定操作唯一 ID，供前端自动化生成客户端调用代码
@router.post(
    "",
    response_model=DocumentRead,
    status_code=201,
    operation_id="uploadDocument",
)
async def upload_document(
    admin: CurrentAdmin,
    session: DbSession,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(
        ...,
        description="待上传文档 (PDF / DOCX / Markdown / HTML)",
    ),
    permission_tags: str | None = Form(
        default=None,
        description='JSON 数组字符串，例如 ["public","hr"]；空 / 不传视为公开',
    ),
) -> DocumentRead:
    """上传文档：写入 COS、落库后立即返回，解析与向量化通过 BackgroundTasks 异步进行。

    【依赖注入说明】：
    - admin (CurrentAdmin): 【第 11 期】上传是写操作，要求管理员；
    - session (DbSession): 当前请求生命周期专属的异步数据库会话，请求结束自动释放；
    - background_tasks (BackgroundTasks): 异步任务池，待当前响应发送给前端后再执行重型计算；
    - file (UploadFile): 上传文件的二进制数据流及文件名、MIME 等元信息。

    【⚠️ permission_tags 为什么是字符串而不是 list[str]】
    这是 multipart/form-data 的固有限制：**表单里所有字段都以字符串传输**，
    没有办法直接声明成 `list[str]` 让 FastAPI 自动解析出数组。
    因此前端要把标签序列化成 JSON 字符串（`'["public","hr"]'`）随表单一起提交，
    后端再手工 `json.loads` 还原。

    【为什么还要兼容"非 JSON 的裸字符串"】
    前端若直接把多个标签拼成 `"public,hr"` 或用普通文本框输入，`json.loads` 会抛错。
    直接报 422 对用户体验不好，而且这种情况并没有歧义 —— 它就是一个单标签。
    因此兜底策略是：**JSON 解析失败就当成单个标签字符串**。
    这样"传对了"和"传得不太对"都能工作，只有真正的脏数据才会被 _normalize_tags 清掉。
    """
    # 【第 11 期】解析权限标签：表单字段是字符串，需要手工还原成列表
    tags: list[str] = []
    if permission_tags:
        try:
            parsed = json.loads(permission_tags)
        except json.JSONDecodeError:
            # 兼容"不是 JSON"的输入：直接当单个标签字符串处理，提升使用宽容度
            parsed = [permission_tags]
        # 必须是列表：若前端传了 '{"a":1}' 这种 JSON 对象，忽略它而不是崩溃
        if isinstance(parsed, list):
            tags = [str(t) for t in parsed]

    # 语法（服务层装配与调用）：service = DocumentService(session)
    #   特性：把数据库连接交给文档服务大总管，完成安检、云存储上传、写库与后台流水线派发
    #   通俗来讲：招揽文档总管去办上传这堆脏活累活，办完后拿到落盘的记录。
    service = DocumentService(session)
    document = await service.upload(
        file,
        background_tasks,
        # 【第 11 期】上传者记为当前登录的管理员（审计用）；
        # 权限标签传下去，由 Service 统一做 _normalize_tags 清洗
        created_by=admin.id,
        permission_tags=tags,
    )

    # 语法（Pydantic 序列化）：DocumentRead.model_validate(document)
    #   特性：基于 from_attributes=True 将 SQLAlchemy ORM 实体转换为安全的出参 DTO
    #   通俗来讲：把存好的数据装进脱敏包装盒里交给前端。
    return DocumentRead.model_validate(document)


# =============================================================================
# 2. 文档分页列表查询接口
# =============================================================================
@router.get(
    "",
    response_model=DocumentListResponse,
    operation_id="listDocuments",
)
async def list_documents(
    user: CurrentUser,
    session: DbSession,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: DocumentStatusValue | None = Query(None, description="按文档状态筛选"),
) -> DocumentListResponse:
    """按分页参数和可选状态多条件筛选文档列表。

    【参数校验与限流边界】：
    - page: 页码约束大于等于 1；
    - page_size: 限制 1~100 条，杜绝恶意传入过大尺寸撑爆服务内存；
    - status: 匹配 5 态字面量枚举过滤条件。

    【第 11 期 · 读接口要登录 + 按权限过滤】
    普通用户只能看到"公开文档 + 与自己标签重叠的文档"；
    admin 传 None 表示不做过滤（看全量）。
    """
    service = DocumentService(session)

    # 语法（枚举类型映射）：DocumentStatus(status) if status else None
    #   特性：将 Pydantic 层的字面量字符串安全转换为 ORM 支持的枚举对象
    #   通俗来讲：如果前端指定了状态，就转成内部枚举牌子传给仓储层检索。
    items, total = await service.list_documents(
        page,
        page_size,
        status=DocumentStatus(status) if status else None,
        # 【第 11 期】权限过滤：admin → None（不过滤），普通用户 → 其有效标签
        permission_tags=_viewer_tags(user),
    )

    # 语法（列表推导式 DTO 批量映射）：[DocumentRead.model_validate(d) for d in items]
    #   特性：逐条执行 DTO 转换，并聚合总条数与分页信息统一返回
    #   通俗来讲：把查出来的每一篇文档都包装成标准模型，连同总数装入分页箱子发给前端。
    return DocumentListResponse(
        items=[DocumentRead.model_validate(d) for d in items],
        total=total,
        page=page,
        page_size=page_size,
    )


# =============================================================================
# 3. 单文档详情查询接口
# =============================================================================
@router.get(
    "/{document_id}",
    response_model=DocumentRead,
    operation_id="getDocument",
)
async def get_document(
    document_id: UUID,
    user: CurrentUser,
    session: DbSession,
) -> DocumentRead:
    """根据主键 UUID 查询单篇文档的详情信息。

    【异常传播】：
    - 若文档在数据库中不存在 **或当前用户无权查看**，底层 service 均抛出 NotFoundError，
      由全局异常处理器拦截并返回 404。
    - 【为什么"无权"也是 404 而不是 403】若两者用不同状态码，调用方就能靠差异
      探测出某个 document_id 是否真实存在（越权信息的侧信道）。统一 404 更安全。
    """
    service = DocumentService(session)

    # 语法（精确单条查询）：await service.get(document_id, permission_tags=...)
    #   特性：通过 URL 路径中的 UUID 提取文档记录，同时校验可见性
    #   通俗来讲：拿着文档 ID 去库里找详情，找不到或你没权限看，service 层都报 404。
    document = await service.get(document_id, permission_tags=_viewer_tags(user))

    return DocumentRead.model_validate(document)


# =============================================================================
# 4. 单文档删除接口
# =============================================================================
# 语法（删除语义规范配置）：
#   - status_code=204: 声明 HTTP 204 No Content（操作执行成功，但响应体无需包含实体内容）
#   - response_class / -> Response: 显式返回原生 Response 对象以适配空内容载荷
@router.delete(
    "/{document_id}",
    status_code=204,
    operation_id="deleteDocument",
)
async def delete_document(
    _: CurrentAdmin,
    document_id: UUID,
    session: DbSession,
) -> Response:
    """根据文档 UUID 执行安全删除（DB 优先 + COS 容错策略）。

    【状态机约束与容错设计】：
    - 仅允许删除处于终态（READY/FAILED）或初始态（UPLOADING）的文档；
    - 若文档正在被后台流水线处理（PARSING/INDEXING），service 层将抛出 ValidationError (400) 拒绝删除；
    - 数据库物理记录删除成功后返回 204，COS 物理文件随后异步完成清理。

    【第 11 期 · 为什么删除要求管理员】
    删除是不可逆操作（连 COS 上的原件都会被清掉），且会影响所有能看到该文档的人 ——
    属于管理动作，不适合普通用户自助执行。参数命名为 `_` 表示函数体用不到这个值。
    """
    service = DocumentService(session)

    # 语法（服务层删除调用）：await service.delete(document_id)
    #   特性：执行先删数据库记录并提交事务，再通知云存储清理对象文件的双阶段容错删除
    #   通俗来讲：让总管把这篇文档彻底从数据库和腾讯云上注销抹平。
    await service.delete(document_id)

    # 语法（空响应体构建）：Response(status_code=204)
    #   特性：直接构建并返回 HTTP 204 标准空载荷响应
    #   通俗来讲：告诉前端“事情办成了，没有其他内容需要看”，干净利落。
    return Response(status_code=204)


# =============================================================================
# 5. 文档失败重试接口
# =============================================================================
@router.post(
    "/{document_id}/retry",
    response_model=DocumentRead,
    operation_id="retryDocument",
)
async def retry_document(
    _: CurrentAdmin,
    document_id: UUID,
    session: DbSession,
    background_tasks: BackgroundTasks,
) -> DocumentRead:
    """对处理失败（FAILED）的文档清理历史脏分块并重新调度入库流水线。

    【核心防线】：
    - 仅 FAILED 状态允许重试，防止并发竞争或对正常文档重复计算；
    - 清理上次失败残留的中间半成品切块，重置为 UPLOADING 态并向 BackgroundTasks 重新挂载任务。

    【第 11 期】同删除：重试会把文档重新送回解析/向量化流水线（消耗算力、可能改写切片），
    属于管理动作，因此要求管理员。
    """
    service = DocumentService(session)

    # 语法（服务层重试调用）：await service.retry(document_id, background_tasks)
    #   特性：防御性清理脏切片数据，重置错误状态并重启后台异步流水线
    #   通俗来讲：让总管打扫干净上次失败留下的烂摊子，然后把文档重新塞回后台处理流水线。
    document = await service.retry(document_id, background_tasks)

    return DocumentRead.model_validate(document)


# =============================================================================
# 阶段 2：文件下载与预览 API
# =============================================================================

# 语法（模块级标准常量）：_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
#   特性：标准 Microsoft Word (.docx) 的 MIME 格式定义
#   核心收益：用于类型精准断言——由于绝大多数现代浏览器无法直接内联渲染二进制 DOCX 文档，
#            即便前端请求传 `?download=0`（尝试预览），服务端也要强制降级为 attachment（下载），
#            防止浏览器误当成乱码文本强行在窗口打开导致崩溃。
#   通俗来讲：这是 Word 文件的“专属防乱码标签”。浏览器看不了 Word，所以只要碰上它，无论用户怎么选都必须强制下载。
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


# =============================================================================
# 6. 文件下载与内联预览接口
# =============================================================================
# 语法（原始字节流响应路由）：
#   - response_class=Response: 不走常规 Pydantic JSON 序列化，直接返回纯二进制网络响应
#   - operation_id="downloadDocument": 供前端 OpenAPI 客户端生成对应的 blob 下载请求代码
@router.get(
    "/{document_id}/file",
    operation_id="downloadDocument",
)
async def download_document(
    document_id: UUID,
    session: DbSession,
    download: int = Query(0, ge=0, le=1, description="1=强制下载, 0=尝试内联预览"),
    token: str | None = Query(
        None,
        description="Bearer token（iframe/新窗口无法带 header 时使用）",
    ),
    authorization: str | None = Header(None, alias="Authorization"),
) -> Response:
    """返回文档原始字节。

    - PDF / HTML / Markdown: 可在浏览器内联预览 (inline)
    - DOCX: 浏览器无法渲染，强制 attachment 下载

    【⭐ 第 11 期 · 本端点为什么要支持两种认证方式（全项目唯一一处）】
    普通接口用 `CurrentUser` 依赖就够了 —— 前端 axios 会给每个请求自动加
    `Authorization` 头。但**下载接口的使用场景有两种是带不上请求头的**：

        ① `<iframe src="/api/documents/{id}/file">` 内联预览PDF
        ② `window.open(...)` / `<a download>` 直接在新窗口打开

    这两种方式由**浏览器自己发起请求**，我们没有任何机会插入自定义请求头。
    所以必须额外支持把令牌放在 URL 查询参数里：`?token=xxx`。

    【为什么要手工调用 get_current_user，而不是用 Depends】
    依赖注入只能从**固定的一个位置**取值。若要同时支持 header 与 query 两种来源，
    就得在函数体内自己做"二选一"，然后把选中的那个交给认证函数。
    因此这里直接 `await get_current_user(session, effective_auth)` ——
    **这是全项目唯一一处绕过依赖注入、手工调用认证函数的地方。**

    【优先级：header 优先于 query】
    两者都传时以 header 为准。原因：请求头不会出现在 URL 里，
    **不会写进浏览器历史、访问日志、Referer 头**，泄露面更小；
    query 参数只是"没法用 header 时"的降级方案。

    【⚠️ 安全提醒（本方案的固有代价）】
    令牌放在 URL 里会被记入服务器访问日志与浏览器历史。
    这是 iframe 下载这个需求带来的固有代价，无法完全消除；
    缓解手段是令牌本身有 1440 分钟的过期时间，且泄露后可通过改密/停用账号失效。

    【第 11 期 · 权限过滤器照常生效】
    手工调用认证拿到 user 之后，仍然要 `_viewer_tags(user)` 传下去 ——
    否则"能下载"就绕过了"能查看"，权限体系在这一处会破一个洞。
    """
    # 【第 11 期】双来源认证：header 优先，其次 query 里的 token
    effective_auth = authorization or (f"Bearer {token}" if token else None)
    user = await get_current_user(session, effective_auth)

    # 语法（业务调度与跨服务协同）：
    #   - 先经由 DocumentService 验证文档主键有效性并获取元数据实体（同时校验可见性）；
    #   - 再委托底层的 file_service (对象存储适配器) 传入内部物理 key 抓取全量二进制文件流。
    #   通俗来讲：先找文档管家核对有没有这个文件、你有没有权看并拿到密钥路径，
    #             再让文件存储仓库把二进制原始内容捞出来。
    service = DocumentService(session)
    document = await service.get(document_id, permission_tags=_viewer_tags(user))
    content = await service.file_service.download(document.cos_object_key)

    # 语法（响应头处置策略决策）：
    #   - force_attachment: 当客户端显式传 download=1 或文件为 DOCX 格式时激活
    #   - disposition: "attachment" 触发浏览器弹出“另存为”保存窗口，"inline" 指引浏览器在标签页直接展示渲染
    #   通俗来讲：判断到底是在网页里直接看（比如 PDF/网页/Markdown），还是直接让浏览器弹弹窗把文件下到电脑里。
    force_attachment = download == 1 or document.mime_type == _DOCX_MIME
    disposition = "attachment" if force_attachment else "inline"

    # 语法（RFC 5987 标准化文件名编码）：quote(document.name, safe="")
    #   特性：将中文字符转义为 URL 编码（百分号编码，如 %E6%96%87%E6%A1%A3.pdf），safe="" 确保所有保留字均被编码
    #   核心作用：彻底杜绝不同操作系统及浏览器因为中文字符集（UTF-8 vs GBK）导致的下载文件名乱码或报错截断。
    #   通俗来讲：把中文文件名翻译成国际标准的百分号安全编码，防止下载下来的文件名字变成一堆乱码或者下不下来。
    filename_quoted = quote(document.name, safe="")

    # 语法（构造原生 Response 对象并挂载流控头）：
    #   - content: 文件的原始二进制数据 (bytes)
    #   - media_type: 真实 MIME 类型（告诉浏览器这是 pdf 还是 docx）
    #   - headers["Content-Disposition"]: 遵循 RFC 5987 国际规范注入标准头 `filename*=UTF-8''...`
    return Response(
        content=content,
        media_type=document.mime_type,
        headers={
            "Content-Disposition": (
                f"{disposition}; filename*=UTF-8''{filename_quoted}"
            ),
        },
    )


# =============================================================================
# 阶段 3：chunks 浏览 API
# =============================================================================


# =============================================================================
# 7. 切片分页列表与质量体检统计查询接口
# =============================================================================
# 语法（路由装饰器配置）：
#   - response_model=DocumentChunkListResponse: 强制出参遵从包含 items 与 stats 的组合 DTO 规范
#   - operation_id="listDocumentChunks": 显式定义操作唯一标识，便于前端生成语义清晰的 API SDK 函数
@router.get(
    "/{document_id}/chunks",
    response_model=DocumentChunkListResponse,
    operation_id="listDocumentChunks",
)
async def list_document_chunks(
    document_id: UUID,
    user: CurrentUser,
    session: DbSession,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> DocumentChunkListResponse:
    """分页查询指定文档的切片（Chunk）列表，并附带整体切块质量统计指标。

    【核心设计考量】：
    - 列表项轻量化：通过 DocumentChunkRead.from_orm_chunk 裁剪正文，仅返回 100 字符摘要，
      避免长切片在列表页全量序列化造成网络与前端 DOM 卡死；
    - 调优反馈：返回 DocumentChunkStats 统计对象，直观透传分块长度极值与均值，
      协助开发者评估切分配置（chunk_size / overlap）是否合理；
    - 状态宽容：若文档未切片或尚处于解析中，stats 优雅降级为 None，不阻塞列表响应。

    【第 11 期 · 权限校验落在父文档上】
    先校验"你能不能看这份文档"，再看它的切片。若父文档不可见，直接 404，
    根本不会走到切片查询 —— 这样切片明细不会成为绕过文档权限的后门。
    """
    service = DocumentService(session)

    # 语法（服务层分页与统计联动查询）：
    #   - 先校验父文档有效性（含可见性），然后执行底层分页 SQL 及聚合函数计算（AVG/MIN/MAX/COUNT）
    #   通俗来讲：让文档总管先确认你有权看这份文档，再把第几页的小纸条和整体字数统计报告一起拿出来。
    items, total, stats = await service.list_chunks(
        document_id,
        page,
        page_size,
        permission_tags=_viewer_tags(user),
    )

    # 语法（响应包装与条件注入）：
    #   - 列表项使用推导式批量执行工厂转换：[DocumentChunkRead.from_orm_chunk(c) for c in items]
    #   - stats 采用三元表达式：仅当统计指标存在时才实例化 DocumentChunkStats，否则回填 None
    return DocumentChunkListResponse(
        items=[DocumentChunkRead.from_orm_chunk(c) for c in items],
        total=total,
        page=page,
        page_size=page_size,
        stats=(
            DocumentChunkStats(
                total=stats.total,
                avg_length=stats.avg_length,
                min_length=stats.min_length,
                max_length=stats.max_length,
            )
            if stats is not None
            else None
        ),
    )


# =============================================================================
# 8. 单切片（Chunk）完整详情查询接口
# =============================================================================
@router.get(
    "/{document_id}/chunks/{chunk_id}",
    response_model=DocumentChunkDetail,
    operation_id="getDocumentChunk",
)
async def get_document_chunk(
    document_id: UUID,
    chunk_id: UUID,
    user: CurrentUser,
    session: DbSession,
) -> DocumentChunkDetail:
    """查询单条切块的完整正文内容及元数据详情。

    【双重鉴权与安全防线（第 11 期升级为三重）】：
    - **父文档可见性**：先确认调用方有权查看该文档（无权则 404）；
    - 路径双校验：显式校验 document_id 与 chunk_id 的级联归属关系，防止越权拉取其他文档的切块；
    - 全文交付：与列表接口的 100 字符截断形成互补，此接口交付未经裁剪的完整 content；
    - 向量隔离：即便读取完整切片实体，也严格隐藏底层高维 Embedding 向量，避免带宽过载。

    【为什么本端点最需要权限校验】
    它是**唯一会返回切块全文**的接口。若只靠"路径双校验"（chunk 属于 document），
    攻击者只要知道任一文档的 id，就能把它的全部原文逐条拉走 ——
    文档列表/详情上的权限过滤会被这个后门完全绕过。
    """
    service = DocumentService(session)

    # 语法（级联主键 + 父文档权限查询）：await service.get_chunk(..., permission_tags=...)
    #   特性：底层先校验父文档可见性，再比对 chunk.document_id == document_id，
    #         若切块不存在、不属于该文档、或调用方无权查看该文档，一律抛 NotFoundError (404)
    #   通俗来讲：先确认你有权看这份文档，再拿主文档 ID 和切片 ID 双重核对，确认无误才把整张小纸条递出来。
    chunk = await service.get_chunk(
        document_id, chunk_id, permission_tags=_viewer_tags(user)
    )

    # 语法（专属工厂转换）：DocumentChunkDetail.from_orm_chunk(chunk)
    #   特性：安全计算字符数并序列化包含全量 content 的专属详情 DTO
    #   通俗来讲：打包成完整的切片详情包装盒，交付前端。
    return DocumentChunkDetail.from_orm_chunk(chunk)


# =============================================================================
# 9. 修改文档可见性标签（第 11 期新增，仅管理员）
# =============================================================================
@router.patch(
    "/{document_id}/permission-tags",
    response_model=DocumentRead,
    operation_id="updateDocumentPermissionTags",
)
async def update_document_permission_tags(
    _: CurrentAdmin,
    document_id: UUID,
    session: DbSession,
    payload: DocumentPermissionTagsUpdate,
) -> DocumentRead:
    """修改文档的可见性标签（谁能看这份文档）。

    【为什么单独开一个 PATCH 端点，而不是混进"编辑文档"】
    本项目没有通用的"编辑文档"接口 —— 文档的其它字段（名称、内容、哈希）
    都来自上传时的物理文件，改它们没有意义（内容变了就是另一份文档）。
    **唯一需要人工调整的就是可见性标签**，因此为它单独开一个语义明确的端点。

    【为什么用 PATCH 而不是 PUT】
    虽然请求体只带一个字段，看起来像"全量替换标签列表"（那更像 PUT），
    但本端点的语义是"**只动权限标签这一个属性**，其它字段一概不碰" ——
    这正是 PATCH（部分更新）的定义。用 PUT 会暗示"提交的是文档的完整表示"，
    那会让人误以为没传的字段会被清空。

    【为什么要管理员】
    改可见性直接影响"谁能看到这份文档"，是安全管理动作：
    - 普通用户若能自我提权（把自己的文档标签清空变成公开），会绕开管理员管控；
    - 反过来，普通用户若能改别人的文档标签，可以做"藏起来让管理员也看不到"这类破坏。
    因此这一处必须由管理员执行。

    【传空列表是什么语义】
    **把文档改回公开**（撤掉所有门禁标签），任何人可见。
    这是合法且有意义的操作，例如"这份制度可以对外公开了"。
    """
    service = DocumentService(session)
    document = await service.update_permission_tags(
        document_id, payload.permission_tags
    )
    return DocumentRead.model_validate(document)

