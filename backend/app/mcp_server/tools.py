"""【模块职责说明】MCP 工具注册层（第 5 章）——5 个知识库工具的实现。

本模块是第 13 期唯一"把协议层接到业务层"的地方，每个工具都遵循同一条流水线：

================================================================================
【步骤 1】统一入口：register_tools(mcp) 把全部工具挂到一个确定的 FastMCP 实例上
================================================================================

不在模块里 `mcp = FastMCP(...)` 就地建实例，而是把实例**从外面传进来**。
好处是：挂载它的地方（`app/mcp_server/server.py` 或 `app/main.py`）决定叫什么名字、
挂到哪个路径，本模块只负责"有哪些工具"。要用的时候一行调用即可：

    from app.mcp_server.tools import register_tools
    register_tools(mcp)

================================================================================
【步骤 2】每个工具都是同一套五步处理
================================================================================

    resolve_current_user(ctx)          步骤 2a：拿用户（第 2 章）
        → require_admin(user)          步骤 2b：需要管理员的工具再加一道闸门
        → 参数校验                      步骤 2c：MCP 参数是外部传进来的，必须自己挡
        → 调对应的 service 方法         步骤 2d：业务逻辑一律在 app/services 里，这里只转译
        → 把 dataclass / ORM 翻成 MCP schema  步骤 2e：出参显式构造（不是 model_validate）

⚠️ 本模块**不写任何业务逻辑**：不判权限、不拼 SQL、不算向量。一旦这里出现"第二套检索实现"
   或"第二套权限判断"，网页侧修好的 bug 就不会自动修到 MCP 侧（第 12 期限流漏掉直传链路的教训）。

================================================================================
【步骤 3】错误出口统一走 _to_tool_error
================================================================================

业务层抛 `AppException`（自带中文 message），本模块统一翻译成**协议层**的
`mcp.server.fastmcp.exceptions.ToolError`，让 Agent 读到一句能理解的人话。

⚠️ 注意区分两个同名异常：
    app.core.exceptions.*                        服务层的异常（本项目体系，带 http_status）
    mcp.server.fastmcp.exceptions.ToolError      协议层的异常（FastMCP → CallToolResult(isError=True)）
服务层不感知 MCP 协议，翻译只发生在这一层（第 4 章的结论）。
"""

from __future__ import annotations

import base64
import binascii

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from app.api.schemas.documents import DocumentStatusValue
from app.core.exceptions import AppException
from app.core.logging import get_logger
from app.db.session import AsyncSessionLocal
from app.mcp_server.auth import require_admin, resolve_current_user
from app.mcp_server.schemas import (
    MCPAnswer,
    MCPCitation,
    # 下面 4 个模型是下一节那三个工具的出参，本节尚未用到 —— 刻意保留，
    # 因为 register_tools 的契约就是"5 个工具一次注册完"，import 块与本函数一一对应。
    MCPDocumentItem,
    MCPDocumentList,
    MCPDocumentStatus,
    MCPStats,
    MCPUploadResult,
)
from app.services.chat_service import ChatService
from app.services.document_service import DocumentService

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
        # ⚠️ 这里刻意【不用】model_validate：两个模型的字段名并不一一对应
        # （服务层 dataclass 的字段是 answer/refused/citations/trace_id，
        #  而 citation dict 里带着 chunk_id / score / retrieval_meta 等内部字段），
        # 显式取字段等于**白名单脱敏** —— 内部调试信息不会因为"名字恰好相同"就漏出去。
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
    # 其余 3 个工具（list_documents / get_document_status / get_knowledge_base_stats）
    # 见下一节，共用上面同一套五步处理与 _to_tool_error / _status_value
    # =========================================================================


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
