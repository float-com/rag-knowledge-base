"""【模块职责说明】MCP 工具注册层（第 5～6 章）——5 个知识库工具的实现。

本模块是第 13 期唯一"把协议层接到业务层"的地方，每个工具都遵循同一条流水线：

================================================================================
【步骤 1】统一入口：register_tools(mcp) 把全部工具挂到一个确定的 FastMCP 实例上
================================================================================

不在模块里 `mcp = FastMCP(...)` 就地建实例，而是把实例**从外面传进来**。
好处是：挂载它的地方（服务装配层）决定叫什么名字、挂到哪个路径，本模块只负责"有哪些工具"。
要用的时候一行调用即可：

    from app.mcp_server.tools import register_tools
    register_tools(mcp)

5 个工具（本节已全部实现）：

    ask_knowledge_base        问答（出参 MCPAnswer）
    upload_document           上传（仅管理员，出参 MCPUploadResult）
    list_documents            列文档（出参 MCPDocumentList）
    get_document_status       查状态与入库进度（出参 MCPDocumentStatus）
    get_knowledge_base_stats  知识库概览（出参 MCPStats）

================================================================================
【步骤 2】每个工具都是同一套五步处理
================================================================================

    resolve_current_user(ctx)          步骤 2a：拿用户（第 2 章）
        → require_admin(user)          步骤 2b：需要管理员的工具再加一道闸门
        → 参数校验                      步骤 2c：MCP 参数是外部传进来的，必须自己挡
        → 调对应的 service 方法         步骤 2d：业务逻辑一律在 app/services 里，这里只转译
        → 把 dataclass / ORM 翻成 MCP schema  步骤 2e：出参见下方"两种翻译方式"

【两种翻译方式，各有适用场景 —— 不是随手选的】

    model_validate(orm_obj)     仅当该 schema 开了 from_attributes=True 且字段与 ORM 对齐时用
                                （本项目目前只有 MCPDocumentItem 一个，见第 3 章）
    显式逐字段构造               其余模型一律这么做：等于白名单脱敏，
                                内部字段（retrieval_meta / score / chunk_id）不会因"名字恰好撞上"而外泄

================================================================================
【步骤 3】错误出口统一走 _to_tool_error
================================================================================

业务层抛 `AppException`（自带中文 message），本模块统一翻译成**协议层**的
`mcp.server.fastmcp.exceptions.ToolError`，让 Agent 读到一句能理解的人话。

⚠️ 注意区分两个同名异常：
    app.core.exceptions.*                        服务层的异常（本项目体系，带 http_status）
    mcp.server.fastmcp.exceptions.ToolError      协议层的异常（FastMCP → CallToolResult(isError=True)）
服务层不感知 MCP 协议，翻译只发生在这一层（第 4 章的结论）。

================================================================================
【步骤 4】权限口径：_viewer_tags 是全模块唯一的标签来源
================================================================================

    admin → None（服务层约定：None = 不做过滤）
    普通用户 → compute_user_permission_tags(user)

★ 这条必须与 REST 侧完全一致（第 1 章 §3.5 的头号风险）：
  MCP 只是换个入口，绝不能自己判一套可见范围，否则外部 Agent 会成为绕过权限体系的捷径。
"""

from __future__ import annotations

import base64
import binascii
from typing import Literal
from uuid import UUID

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from app.api.schemas.documents import DocumentStatusValue, IngestionTaskStatusValue
from app.core.exceptions import AppException
from app.core.logging import get_logger
from app.db.models import DocumentStatus, IngestionTask, User
from app.db.session import AsyncSessionLocal
from app.mcp_server.auth import require_admin, resolve_current_user
from app.mcp_server.schemas import (
    MCPAnswer,
    MCPCitation,
    MCPDocumentItem,
    MCPDocumentList,
    MCPDocumentStatus,
    MCPStats,
    MCPUploadResult,
)
from app.services.chat_service import ChatService
from app.services.document_service import DocumentService
from app.services.permission_service import compute_user_permission_tags, is_admin

logger = get_logger(__name__)

# 【语法点：from __future__ import annotations】
# 它把本模块所有类型注解变成**字符串**、延后求值，带来两个实际好处：
#   ① 注解里可以自由引用尚未定义的名字（前向引用不必加引号）；
#   ② 注解的求值成本从"导入时"推迟到"真的需要时"。
# ⚠️ 一个常见误解要澄清：**它不负责类型转换**。
#    即使加了它，`status=document.status` 这种"字符串 → Literal"的收敛也**不会**自动发生，
#    仍要靠下面的 _status_value 显式处理（见该函数的说明）。


def register_tools(mcp: FastMCP) -> None:
    """把 5 个知识库工具挂到给定 FastMCP 实例。

    :param mcp: 由调用方创建好的 FastMCP 实例（本模块不自己建、不自己挂载）

    【为什么工具函数要定义在这个函数体内部（闭包）】
    因为它们必须捕获外层的 `mcp` 才能写 `@mcp.tool(...)`。
    定义在模块级、再 `mcp.tool()(fn)` 手动注册也可以，但那样：
      · 工具函数会变成模块级公开 API，容易被别处误调用；
      · 装饰器的 name / description 离函数更远，改契约时更容易漏。
    写在里面则"契约声明"与"函数实现"始终贴在一起。
    """

    # =========================================================================
    # 工具 1：ask_knowledge_base —— 向知识库提问
    # =========================================================================
    @mcp.tool(
        name="ask_knowledge_base",
        title="知识库问答",
        description=(
            "向知识库提问并得到带引用的答案。检索按调用者的权限标签过滤；"
            "命中阈值不足或答案校验未通过时返回 refused=true 与统一拒答文案。"
        ),
    )
    async def ask_knowledge_base(question: str, ctx: Context) -> MCPAnswer:
        """向知识库提问，返回带引用的非流式答案。

        【步骤 1：认人】MCP 侧没有 FastAPI 的依赖注入，必须显式解析当前用户
        （第 2 章 resolve_current_user：Context → Bearer 令牌 → 查库 → 活跃用户）。
        """
        user = await resolve_current_user(ctx)

        # 【步骤 2：挡参数】MCP 的参数来自外部 Agent，不能假设它一定传了有效内容。
        # 空字符串也能通过协议层的类型校验（str 类型本身没问题），所以必须自己挡。
        question = question.strip()
        if not question:
            raise ToolError("问题不能为空")

        # 【步骤 3：短会话调服务】业务逻辑全在 ChatService 里：
        # 权限过滤、RAG 图、答案校验都在那一侧，这里只负责"传参 + 收结果"。
        async with AsyncSessionLocal() as session:
            service = ChatService(session)
            try:
                result = await service.answer_for_mcp(question, current_user=user)
            except Exception as exc:
                # 【步骤 4：翻译错误】第 4 章结论：service 层"异常一路冒出"，
                # 由本层统一转成协议层 ToolError，Agent 才看得到失败原因。
                raise _to_tool_error(exc, default="问答处理失败") from exc

        # 【步骤 5：出参显式构造】把服务层的 dataclass（MCPChatAnswer）翻成
        # MCP 契约模型（MCPAnswer / MCPCitation）。
        # ⚠️ 这里刻意【不用】model_validate：服务层 citation 是 dict，里面带着
        # chunk_id / score / retrieval_meta 等内部调试字段（第 4 章 _serialize_citation 产出的），
        # 显式取字段等于**白名单脱敏** —— 内部信息不会因为"名字恰好相同"就漏出去。
        citations = [
            MCPCitation(
                ordinal=int(c["ordinal"]),
                document_id=c["document_id"],
                document_name=c["document_name"],
                page_no=c.get("page_no"),
                section_path=c.get("section_path"),
                quote=c.get("quote", ""),
            )
            for c in result.citations
        ]
        return MCPAnswer(
            answer=result.answer,
            refused=result.refused,
            citations=citations,
            trace_id=result.trace_id,
        )

    # =========================================================================
    # 工具 2：upload_document —— 上传文档（管理员）
    # =========================================================================
    @mcp.tool(
        name="upload_document",
        title="上传文档",
        description=(
            "上传文件到知识库（仅管理员）。content_base64 是文件原字节的 "
            "base64 编码；服务端按 sha256 做幂等，相同内容会复用现有文档。"
            "解析与向量化由 Celery 异步执行，调用方可通过 get_document_status 轮询进度。"
        ),
    )
    async def upload_document(
        filename: str,
        content_base64: str,
        ctx: Context,
        mime_type: str | None = None,
        permission_tags: list[str] | None = None,
    ) -> MCPUploadResult:
        """上传文档（MCP 版）。内容以 base64 字符串塞进 JSON 参数。

        【步骤 1：认人 + 鉴权】上传是**写操作**，按第 1 章 §3.7 定的边界要求管理员：
        `require_admin` 复用 permission_service.is_admin，与网页侧的 CurrentAdmin 同一口径。
        ⚠️ 顺序不能反：先 resolve_current_user（拿到人）再 require_admin（判资格）。
        """
        admin = await resolve_current_user(ctx)
        require_admin(admin)

        # 【步骤 2：挡参数】文件名不能是空白 —— 服务层要靠后缀与 MIME 双向比对来拦截非法类型。
        if not filename.strip():
            raise ToolError("filename 不能为空")

        # 【步骤 3：base64 解码】MCP 协议没有 multipart，文件内容只能作为字符串传进来。
        # validate=True：遇到 base64 字母表之外的字符（含换行、空格、URL-safe 的 -_）直接报错，
        # 而不是"尽力猜测、静默丢字节" —— 后者会写出一个内容被悄悄截断的文档，比报错难查得多。
        try:
            content = base64.b64decode(content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ToolError("content_base64 不是合法的 base64 编码") from exc

        # 【步骤 4：把 bytes 包装成"兼容 UploadFile 的轻量对象"】
        # DocumentService.upload 是为 FastAPI 路由设计的，签名要求 UploadFile；
        # 而 MCP 侧只有 bytes。与其改动 service（会波及网页侧上传链路），
        # 不如在本层适配 —— 见下方 _Base64UploadFile，**服务层完全不知情**。
        upload_file = _Base64UploadFile(
            filename=filename,
            content=content,
            content_type=mime_type,
        )

        # 【步骤 5：调服务 + 翻译错误】幂等（sha256 秒传）、大小限制、MIME 检测
        # 全部走 service 层既有逻辑，工具层只做协议转换。
        async with AsyncSessionLocal() as session:
            service = DocumentService(session)
            try:
                document = await service.upload(
                    upload_file,  # type: ignore[arg-type]
                    created_by=admin.id,
                    permission_tags=permission_tags,
                )
            except Exception as exc:
                raise _to_tool_error(exc, default="文档上传失败") from exc

        # 【步骤 6：出参显式构造 + 状态归一】
        # _status_value 把 ORM 上的字符串状态收敛成 DocumentStatusValue 这个 Literal 的取值，
        # 避免"数据库里存了契约没声明的状态"时直接构造失败（见函数内的详细说明）。
        return MCPUploadResult(
            document_id=document.id,
            name=document.name,
            status=_status_value(document.status),
            version=document.version,
            file_hash=document.file_hash,
        )

    # =========================================================================
    # 工具 3：list_documents —— 列出当前用户可见的文档
    # =========================================================================
    @mcp.tool(
        name="list_documents",
        title="列出文档",
        description="按更新时间倒序分页列出当前用户可见的文档。",
    )
    async def list_documents(
        ctx: Context,
        page: int = 1,
        page_size: int = 20,
        status: DocumentStatusValue | None = None,
    ) -> MCPDocumentList:
        """列出当前用户可见的文档（分页）。

        【为什么这个工具不涉及新 service 逻辑】
        它直接调既有的 `DocumentService.list_documents`：分页、状态过滤、权限过滤
        全都在那一层做过且测过（第 11 期给它加过 permission_tags 参数）。
        本工具只做三件事：认人 → 挡参数 → 把结果翻译成 MCP 契约。
        """
        # 【步骤 1：认人】与工具 1 一致；注意这里还【没有】算权限标签，
        # 标签在调用服务时才通过 _viewer_tags(user) 现算（见该函数说明）。
        user = await resolve_current_user(ctx)

        # 【步骤 2：挡参数】为什么服务层有校验、这里还要再挡一次：
        # MCP 的参数由**外部 Agent**填，模型很容易给出 page=0 或 page_size=1000；
        # 若只依赖服务层兜底，Agent 拿到的会是一句模糊的失败原因。
        # 在最外层给出**明确的边界提示**（"page 必须 >= 1"），模型才知道该怎么改参数重试。
        if page < 1:
            raise ToolError("page 必须 >= 1")
        if page_size < 1 or page_size > 100:
            raise ToolError("page_size 必须在 1-100 之间")

        # 【步骤 3：调服务】注意 status 的翻译：
        #   MCP 参数是 Literal 的五个字符串取值 → 服务层要的是 DocumentStatus 枚举成员，
        #   所以必须 `DocumentStatus(status)` 显式转一次。
        #   （这是"契约层与服务层类型不同"的一处真实例子：不是多余，而是两边各自合理。）
        async with AsyncSessionLocal() as session:
            service = DocumentService(session)
            try:
                items, total = await service.list_documents(
                    page,
                    page_size,
                    status=DocumentStatus(status) if status else None,
                    permission_tags=_viewer_tags(user),
                )
            except Exception as exc:
                raise _to_tool_error(exc, default="文档列表查询失败") from exc

        # 【步骤 4：出参翻译】★ 这里用 model_validate，与工具 1/2 的显式构造不同 —— 为什么？
        # MCPDocumentItem 声明了 `model_config = ConfigDict(from_attributes=True)`（第 3 章），
        # 而它的字段集合与 ORM Document 是**刻意对齐**的（id/name/status/mime_type/size/
        # version/permission_tags/created_at/updated_at），没有内部字段需要拦。
        # ⚠️ 所以两条路径的差别不是"随意"，而是取决于模型是否开了 from_attributes：
        #    开了 → 可以直接吃 ORM 对象（本工具）；
        #    没开 → 必须显式构造（工具 1 的 MCPCitation、工具 2 的 MCPUploadResult）。
        return MCPDocumentList(
            items=[MCPDocumentItem.model_validate(d) for d in items],
            total=total,
            page=page,
            page_size=page_size,
        )

    # =========================================================================
    # 工具 4：get_document_status —— 查文档状态与入库进度
    # =========================================================================
    @mcp.tool(
        name="get_document_status",
        title="查询文档状态",
        description=(
            "返回文档当前状态与最近一次入库任务（ingest / reindex）的进度。"
            "适合在 upload_document 后轮询直到 status='ready'。"
        ),
    )
    async def get_document_status(
        document_id: str,
        ctx: Context,
    ) -> MCPDocumentStatus:
        """查文档状态与最近一次入库任务进度。

        【步骤 1：认人】同上。

        【步骤 2：解析 document_id】★ MCP 协议层传进来的是**字符串**，
        而 service 层要的是 UUID —— 所以工具层必须显式 UUID() 解析一次。
        见下方 `_parse_uuid`：解析失败要给"字段名 + 不合法"的明确文案，
        而不是让 ValueError 冒成工具内部错误。
        """
        user = await resolve_current_user(ctx)
        document_uuid = _parse_uuid(document_id, field="document_id")

        async with AsyncSessionLocal() as session:
            service = DocumentService(session)
            try:
                # 【步骤 3：查文档】get 会做权限过滤（传了 _viewer_tags）：
                # 无权访问时抛 NotFoundError —— 与网页侧口径一致，
                # 不区分"不存在"与"无权"，避免向外泄露"这份文档存在"。
                document = await service.get(
                    document_uuid, permission_tags=_viewer_tags(user)
                )
                # 【步骤 4：查最近任务】返回值可以为 None（文档从未入过库 / 任务被清理），
                # 所以下面 latest_task_* 字段全部允许为空（第 3 章已按可空设计）。
                latest = await service.get_latest_task(document.id)
            except Exception as exc:
                raise _to_tool_error(exc, default="文档状态查询失败") from exc

        # 【步骤 5：出参翻译】这里的三个辅助函数各解决一类"类型不能直接相加"的问题：
        #   _status_value       str → DocumentStatusValue（Literal 五态）
        #   _task_type_value    str → Literal["ingest","reindex"]
        #   _task_status_value  str → IngestionTaskStatusValue
        # 全部是"ORM 上是普通字符串、契约上是 Literal"的收敛，
        # 且都遵循同一条原则：**只去空白，不兜底**（未知取值让校验报出来）。
        return MCPDocumentStatus(
            document_id=document.id,
            name=document.name,
            status=_status_value(document.status),
            version=document.version,
            error_message=document.error_message,
            latest_task_type=_task_type_value(latest),
            latest_task_status=_task_status_value(latest),
            latest_task_progress_total=latest.progress_total if latest else None,
            latest_task_progress_done=latest.progress_done if latest else None,
            latest_task_error_message=latest.error_message if latest else None,
        )

    # =========================================================================
    # 工具 5：get_knowledge_base_stats —— 知识库概览
    # =========================================================================
    @mcp.tool(
        name="get_knowledge_base_stats",
        title="知识库概览",
        description=(
            "返回当前用户视角的文档总数 / chunk 总数 / 最近入库时间。"
            "admin 看全量，普通用户严格按权限标签过滤后统计。"
        ),
    )
    async def get_knowledge_base_stats(ctx: Context) -> MCPStats:
        """知识库概览（严格按调用者权限范围统计）。

        【步骤 1：认人】同上。

        【为什么统计也要按权限过滤】
        "知识库有多大"本身也是信息：一份只对 HR 可见的文档，
        若被统计进总数，外部 Agent 就能推断出"存在我无权访问的资料"。
        所以三项指标全部走 `_viewer_tags(user)` 的可见范围 —— 与列表、检索同口径。
        """
        user = await resolve_current_user(ctx)

        async with AsyncSessionLocal() as session:
            service = DocumentService(session)
            try:
                # 【步骤 2：三项聚合统一走服务层】
                # DocumentService.get_stats 内部同时用到 self.repo 与 self.chunk_repo：
                # 文档数与最近入库时间来自 documents 表，chunk 数来自 chunks join documents。
                # 工具层只负责把这份快照翻译成契约对象。
                stats = await service.get_stats(permission_tags=_viewer_tags(user))
            except Exception as exc:
                raise _to_tool_error(exc, default="知识库统计查询失败") from exc

        # 【步骤 3：出参翻译】★ 这里用属性访问（stats.document_count）而不是下标 ——
        # 因为服务层返回的是 KnowledgeBaseStats 这个 dataclass，
        # 字段名拼错在**这一行**就会被静态检查抓到，而不是等到运行时 KeyError。
        return MCPStats(
            document_count=stats.document_count,
            chunk_count=stats.chunk_count,
            last_indexed_at=stats.last_indexed_at,
        )


def _to_tool_error(exc: Exception, *, default: str) -> ToolError:
    """统一把业务异常翻译为 MCP ToolError。

    :param exc: 被捕获的异常
    :param default: 非 AppException（以及无 message 的异常）的兜底文案
    :return: 可直接 raise 的协议层 ToolError

    【步骤 1：AppException 的 message 是写给人看的，直接透出】
    本项目所有业务异常都带中文 message（"文件为空"/"不支持的文件类型"…），
    这正是 Agent 最需要的信息 —— 它能把这句话转述给用户，或据此调整下一次调用。

    【步骤 2：其它异常只给兜底文案，但服务端要留完整堆栈】
    ValueError / KeyError 这类原生异常的 str() 可能是内部实现细节，
    直接透给外部调用方既不安全也没帮助；但排查时必须能看到它，所以：
      · 对外 → return ToolError(default)
      · 对内 → logger.exception(...) 打完整堆栈（含 traceback）
    这是"对外收敛、对内透明"的标准做法。
    """
    if isinstance(exc, AppException):
        # 与第 2 章 auth.py 的 _user_facing_message 同构：只认业务异常的 message
        return ToolError(exc.message)
    logger.exception("MCP tool unexpected error")
    return ToolError(default)


class _Base64UploadFile:
    """把已 decode 的 bytes 包装成兼容 FastAPI UploadFile 的轻量对象。

    DocumentService.upload 只用到 `filename` / `content_type` / `read()` 三个属性，
    这里满足这个最小接口即可，避免再引入 `python-multipart` 的 SpooledTemporaryFile。

    【为什么不用真的 UploadFile】
    FastAPI 的 UploadFile 需要一个 `starlette.datastructures.UploadFile` + 底层文件对象，
    在 MCP 侧凭空造一个完整的它，等于把 Web 框架的实现细节拖进协议层；
    而 service 真正依赖的只是"三个属性 + 一个 async read"。
    **面向接口而非面向实现**：这里给的是一个鸭子类型对象，不是它的孪生兄弟。

    【⚠️ 它与 UploadFile 的差异，改 service 时必须知道】
      · read() 每次返回**全量字节**（不推进游标），可重复调用；UploadFile 的 read() 有游标语义；
      · 没有 seek / size / file / headers 等属性 —— 一旦 service 开始用它们，
        这里就会 AttributeError。所以本类只适合"当前这个"service.upload。
    """

    def __init__(
        self,
        *,
        filename: str,
        content: bytes,
        content_type: str | None,
    ) -> None:
        self.filename = filename
        # content_type 归一：None 与 "" 在 service 里走同一条"按后缀推断"的分支，
        # 统一成 "" 可以让下游的 if 判断只写一次。
        self.content_type = content_type or ""
        self._content = content

    async def read(self) -> bytes:
        """返回完整文件字节（不移动游标，可重复读）。"""
        return self._content


def _viewer_tags(user: User) -> list[str] | None:
    """把用户翻译成"检索/查询时该传的权限标签"。

    :param user: 已认证的当前用户
    :return: admin → None（表示不做权限过滤）；普通用户 → 合并后的有效标签列表

    【为什么 admin 要传 None 而不是 ["*"]】
    服务层的约定是 **None = 不做权限过滤**（见 DocumentService.get / list_documents 的
    permission_tags 参数说明）。而 ["*"] 是"通配标签"，语义上虽然也等于全可见，
    但它要走一遍"数组重叠"的 SQL 运算 —— 既然 admin 本来就不需要过滤，
    直接传 None 让 SQL 少一个条件更直接。

    【为什么与 REST 侧同口径】
    网页侧的路由也是这么算的（admin → None）。★ 这条必须一致：
    一旦 MCP 侧自己判一套可见范围，就会出现"网页看不到、Agent 却能看到"的双口径漏洞
    —— 这正是第 1 章 §3.5 定的头号风险，也是本模块唯一不能写错的一行。
    """
    return None if is_admin(user) else compute_user_permission_tags(user)


def _task_type_value(task: IngestionTask | None) -> Literal["ingest", "reindex"] | None:
    """任务类型 ORM 字符串 → 契约 Literal；无任务时为 None。

    【为什么可以直接 .value】
    任务类型在 ORM 上是 Enum（不是裸字符串），`.value` 拿到的正是
    "ingest" / "reindex" 这两个字面量。ORM 与契约的取值集合本来就一致，
    这里不需要像 _status_value 那样额外收敛。
    """
    return task.task_type.value if task else None


def _task_status_value(
    task: IngestionTask | None,
) -> IngestionTaskStatusValue | None:
    """任务状态 ORM 字符串 → 契约 Literal；无任务时为 None。

    ⚠️ 与 _status_value 的区别要留意：这里**没有** strip() ——
    任务状态来自 Enum 的 .value，不会带尾随空格；
    而文档状态来自 ORM 的普通字符串列，才有去空白的必要。
    """
    return task.status.value if task else None


def _parse_uuid(raw: str, *, field: str) -> UUID:
    """把 MCP 传来的字符串解析成 UUID；失败时给出带字段名的明确文案。

    :param raw: 协议层传进来的字符串（Agent 写在 JSON 里的值）
    :param field: 字段名，只用于拼错误文案
    :raises ToolError: 不是合法的 UUID 字符串

    【为什么必须显式解析，而不是让 Pydantic 自动转】
    本工具的入参声明是 `document_id: str`，**不是 UUID** —— 因为 MCP 的 JSON 里它就是个字符串。
    若把它声明成 UUID，格式不合法会变成协议层的参数校验错误（模型看到的是一段 schema 报错），
    不如"document_id 不是合法的 UUID"这句话可操作：模型能立刻明白要重新取一个正确的 id。

    【为什么捕获 (TypeError, ValueError) 两个】
    `uuid.UUID()` 对**类型不对**（如 None、int）抛 TypeError，对**格式不对**抛 ValueError。
    只写 ValueError 的话，上游一旦传了非字符串就会冒成工具内部错误。
    这里两个都收，统一转成一句可读文案。

    【import 位置的说明】
    教程把 `from uuid import UUID` 写在函数体内；本项目统一把 import 放在模块顶部
    （见文件头 `from uuid import UUID`），因此这里只是就地使用、不再重复导入 ——
    函数内 import 会让"这个依赖从哪来"变难追踪，也会让静态检查的未使用检测失效。
    """
    try:
        return UUID(raw)
    except (TypeError, ValueError) as exc:
        raise ToolError(f"{field} 不是合法的 UUID") from exc


def _status_value(raw: str) -> DocumentStatusValue:
    """把 ORM 上的状态字符串收敛成 DocumentStatusValue 的取值。

    :param raw: `document.status`（数据库里就是普通字符串）
    :return: 去空白后的状态串（类型层面属于 DocumentStatusValue 那个 Literal）

    【为什么需要这一步，而不是直接把 document.status 塞进 MCPUploadResult】
    MCP 契约把 status 声明为 `DocumentStatusValue`（Literal 五态），而 ORM 上只是 str。
    Pydantic 会在构造时做校验，本函数负责把"数据库里的字符串"变成"可信的契约取值"。

    ⚠️ 这里**只做一件事：strip()**。不做映射、不做兜底 ——
    因为库里若出现契约未声明的状态（历史脏数据，或将来新增状态忘了同步契约），
    正确的行为是**让校验失败并报出来**，而不是静默改成某个合法值。
    把未知状态兜底成 "ready" 尤其危险：Agent 会以为文档已经可检索了。
    strip() 只是因为"尾随空格"这种最常见的存储瑕疵不值得让整条链路失败。

    【# type: ignore[return-value] 的来历】
    `str.strip()` 的静态返回类型是 `str`，而不是 `DocumentStatusValue` ——
    静态检查器无法证明去空白后的字符串一定落在字面量集合里。
    这个转换的合法性由 Pydantic 在构造 MCPUploadResult 时兜底校验，
    所以在这里显式标注忽略，而不是用 cast() 假装安全。
    """
    return raw.strip()  # type: ignore[return-value]
