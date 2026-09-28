"""MCP 工具级限流（第 13 期 · 上线前安全补齐）。

================================================================================
【这个模块解决什么】
================================================================================

第 12 期给 REST 侧挂上了滑动窗口限流，但**只挂在走 FastAPI 依赖链的路由上**：

    app/api/deps.py::enforce_rate_limit     ← 只有 include_router 的路由才吃得到

而 MCP 是 `app.mount("/mcp", ...)` 挂进来的**独立 ASGI 子应用**，
它不走 FastAPI 的 `Depends` 那一套 —— 于是：

    ❌ MCP 的 5 个工具，一个都没被限流
    ⚠️ 而其中 ask_knowledge_base 烧 LLM token、upload_document 烧 embedding 与 COS
       → 一个持有合法 JWT 的调用方可以无限刷，绕过第 12 期的全部限流设计

这正是第 12 期那条教训的第三次复现：**新加一条通路，要重新按"能力清单"打钩**，
而不是按文件排查。当时漏的是直传链路，这次漏的是 MCP 通道。

================================================================================
【为什么限流写在工具层，而不是塞进 resolve_current_user】
================================================================================

`resolve_current_user` 的职责是"认人"（Context → 令牌 → 用户），它被**每一个**工具调用。
若把限流放进去，就等于"所有工具共享一个额度" —— 但三个只读工具
（list_documents / get_document_status / get_knowledge_base_stats）是 Agent 的
"了解环境"动作，前端那三个对应只读接口本来也刻意没挂限流
（第 12 期结论：给读接口挂限流会让正常轮询把自己限死）。

**限流要打在钱上**：只有 ask（LLM）与 upload（embedding + COS）挂。
"""

from app.core.config import settings
from app.core.rate_limiter import get_rate_limiter
from app.db.models import User


async def enforce_tool_limit(user: User, *, scope: str) -> None:
    """按用户 + 工具分组做滑动窗口限流；越限抛 RateLimitError。

    :param user: 已由 resolve_current_user 认证过的用户
    :param scope: 分组名（如 "ask" / "upload"）。同一个人在不同分组里各有一份独立额度。
    :raises RateLimitError: 窗口内该分组的调用次数已达 RATE_LIMIT_PER_MINUTE

    【为什么按 scope 分组，而不是所有工具共用一个额度】
    若共用一个 bucket，会出现"上传一个大文件把问答额度吃光"这种诡异现象 ——
    用户只是想问个问题，却因为刚传过文件而被 429。
    分开计数后，每类重操作的额度互相独立，语义也更清楚。

    【为什么限流 key 要加 "mcp-" 前缀】
    第 12 期 REST 侧的 key 是 `rag:rate_limit:user:<uuid>`；
    这里用 `rag:rate_limit:mcp-<scope>:user:<uuid>`。
    加前缀是为了【与 REST 隔离】—— 否则 MCP 里问一次问题，
    网页侧就少一次问答额度，用户会莫名其妙被 429，且完全看不出原因。

    【为什么依赖 settings.rate_limit_enabled 总开关】
    与 REST 侧共用同一个开关：本地压测 / 调试时一键关闭，
    不必分别记得关两处（第 12 期那条"漏挂"的教训同样适用于"漏关"）。

    【错误出口】
    抛的是 `RateLimitError`（AppException 子类，自带中文 message）。
    在 MCP 工具层它会被 `_to_tool_error` 捕获并透出文案
    （"请求过于频繁，每分钟最多 N 次，请稍后再试"）——
    对 Agent 来说是可读的，模型能据此决定"稍后重试"而不是"换个工具瞎试"。
    """
    if not settings.rate_limit_enabled:
        # 总开关关闭时直接放行（与 app/api/deps.py::enforce_rate_limit 的行为保持一致）
        return
    await get_rate_limiter().check(f"mcp-{scope}:user:{user.id}")
