"""【模块职责说明】MCP Server 的鉴权工具函数层。

本模块是《第 2 章 鉴权工具函数》的产物，只解决一件事：

    **MCP 工具被调用时，怎么知道现在是谁在调？**

【为什么不能直接 import 网页侧的 get_current_user】

网页侧（第 11 期）的 `app/api/deps.py::get_current_user` 是 **FastAPI 的依赖**，
它依赖的是 `Depends` + `Header(alias="Authorization")` 这套**框架注入机制**。
FastMCP 这一层没有 FastAPI 依赖注入：工具函数拿到的是 `Context`，
真正的 HTTP 请求要从 `ctx.request_context.request` 里自己取。

两边的「入参来源」与「错误出口」都不一样，所以不能复用同一个函数：

| | 网页侧（RESTful） | 本模块（MCP） |
| --- | --- | --- |
| 请求头来源 | FastAPI 注入的 `Authorization` 参数 | `ctx.request_context.request.headers` |
| 失败出口 | `UnauthorizedError` → 全局异常处理器 → HTTP 401 | `ToolError` → 协议层以 isError 内容返回给 Agent<br>（本模块内部的 `_parse_bearer_token` 仍抛 `UnauthorizedError`，由 `resolve_current_user` 统一转译） |
| 取用户 | `UserRepository(session).get_by_id()` | 同一份（**故意保持一致**） |

> ⚠️ 上表最后一列写的是**本模块对外的入口**（`resolve_current_user`）。
> 内部那个 `_parse_bearer_token` 并不抛 `ToolError`，它抛的是 `UnauthorizedError`
> —— 与网页侧同名函数行为一致，转译发生在 `resolve_current_user` 的 `except` 处。

【但"链路"必须完全一致 —— 这是本模块最重要的设计约束】

```
    解析 Authorization → 取出 Bearer 令牌 → 验签拿到 subject
        → UUID 转型 → 查库 → 校验 status == ACTIVE → 返回 User
```

上面六步与 `app/api/deps.py::get_current_user` **一步一步对齐**，只有"失败时抛哪个异常"不同。
理由很实在：**鉴权口径一旦有第二份实现，就会出现"网页登不进、Agent 却能进"这类漏洞**，
而且将来加"令牌黑名单"之类的机制时，只改一处是修不掉两边的。

【为什么这里要把 AppException 统一翻译成 ToolError】

`AppException` 是给 **HTTP 响应**用的（自带 code / message / http_status），
但 MCP 协议里没有 HTTP 状态码这一层 —— Agent 看到的是"工具调用失败 + 一段文本"。
所以本模块的 `_user_facing_message` 把业务异常降维成一句话文案，
把 HTTP 细节收敛在这一层里，**不让它泄漏到协议层**。

【⚠️ 本模块只做"认证"，不做"鉴权"】

- 认证（你是谁）→ 本模块的 `resolve_current_user`；
- 鉴权（你能不能）→ `require_admin`（工具级 admin 闸门）
  + 业务层按 `permission_tags` 过滤（**不是在这里判**）。

`permission_tags` 的计算留给各工具去调 `permission_service.compute_user_permission_tags(user)`，
本模块**不缓存、不猜测**用户可见范围。
"""

from uuid import UUID

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError

from app.core.exceptions import AppException, UnauthorizedError
from app.core.security import decode_access_token
from app.db.models import User, UserStatus
from app.db.repositories.user_repo import UserRepository
from app.db.session import AsyncSessionLocal
from app.services.permission_service import is_admin

# =============================================================================
# 第一步：纯字符串的令牌提取（与 app/api/deps.py::_parse_bearer_token 同构）
# =============================================================================
#
# 【为什么这一步要单独拆出来】
# 它只做「从请求头抠出令牌」这一件与数据库、与业务都无关的纯字符串工作，
# 拆出来既能被 resolve_current_user 复用，也便于单独测试（不用起 MCP 服务）。
#
# 【为什么用 partition(" ") 而不是 split(" ")】
# partition 恒定返回 3 个元素（左、分隔符本身、右），找不到分隔符时返回 ("Bearer", "", "")；
# 而 split(" ") 的元素个数随空格数量变化：
#   - "Bearer"      → 1 个元素，解包抛 ValueError（not enough values）
#   - "Bearer a b"  → 3 个元素，多出来的部分会被当成 token 的一部分
# 两者都可能把未处理的 ValueError 抛成"工具内部错误"，所以这里选用 partition。


def _parse_bearer_token(authorization: str | None) -> str:
    """从 Authorization 请求头里取出 Bearer 令牌。

    :param authorization: 原始请求头值，形如 `Bearer eyJhbGciOi...`；未携带时为 None
    :return: 去掉 `Bearer ` 前缀后的纯令牌串
    :raises UnauthorizedError: 未携带请求头（未登录），或格式不符合 Bearer 规范

    【为什么未携带和格式错误要给不同的文案】
    两者最终都是"拒绝"，但语义不同：
    - 没带请求头 → 调用方根本没做认证，提示"请先登录"，客户端应先走登录拿令牌；
    - 带了但不是 Bearer → 令牌本身可能没问题，是**请求头拼错了**（例如漏了 "Bearer " 前缀、
      或写成了 "Token xxx"），提示"无效的访问凭证"能让开发者直接定位到**请求构造**而非"我的令牌坏了"。

    【为什么不区分大小写】
    HTTP 规范里认证方案名（Bearer）是大小写不敏感的，`bearer` / `BEARER` 都合法，
    因此 scheme 部分统一 lower() 之后再比较。
    """
    if not authorization:
        raise UnauthorizedError("请先登录")

    # partition(" ") 按首个空格将字符串切分成 (左侧部分, 分隔符本身, 右侧部分)
    # 依次解包赋值给 3 个变量：
    # 1. scheme: 认证类型（如 "Bearer"）
    # 2. _     : 哑变量，接收切分出来的空格本身 " "（占位且后续不使用）
    # 3. token : 凭证主体（即剩余的 Token 字符串）
    scheme, _, token = authorization.partition(" ")

    # 空格后必须有内容：`"Bearer "` 这种只有前缀没有令牌的情况同样要拒绝
    if scheme.lower() != "bearer" or not token:
        raise UnauthorizedError("无效的访问凭证")

    return token


# =============================================================================
# 第二步：从 MCP Context 反查出当前用户
# =============================================================================


async def resolve_current_user(ctx: Context) -> User:
    """从 MCP Context 取出 Bearer 令牌并查到活跃用户。

    :param ctx: FastMCP 注入的工具调用上下文，内含底层传输层的 Request
    :return: 已认证且状态正常的 User ORM 实体
    :raises ToolError: 鉴权链路中任一环节失败均统一抛出该异常，供 Agent 感知

    【设计背景：MCP 协议与传统 REST 的鉴权差异】
    - REST 端失败时抛出 `UnauthorizedError`（HTTP 401 状态码）。
    - MCP 协议无 HTTP 状态码概念，其核心交互单元是"工具调用结果"。
    - 抛出 `ToolError` 会被 FastMCP 捕获并封装为工具执行错误文本（isError=True）返回给 LLM，
      使模型能理解"请先登录/令牌已失效"并引导终端用户，而非直接崩溃或返回空数据。
    """

    # -------------------------------------------------------------------------
    # 步骤 1：从传输上下文安全提取底层 HTTP Request 对象
    # -------------------------------------------------------------------------
    # getattr(object, name, default): 防御式属性获取
    # 1. ctx.request_context: FastMCP 提供的当前调用上下文
    # 2. "request"          : 期望获取的底层 Starlette/FastAPI Request 实例
    # 3. None               : 默认缺省值；若运行于非 HTTP 传输（如 stdio）则返回 None，避免抛 AttributeError
    request = getattr(ctx.request_context, "request", None)

    if request is None:
        # 显式阻断不支持 HTTP Header 传递的传输协议（如 stdio/CLI）
        raise ToolError("当前传输模式不支持基于 HTTP Header 的身份鉴权")

    # -------------------------------------------------------------------------
    # 步骤 2：提取、解密 Token 并转换用户唯一标识（UUID）
    # -------------------------------------------------------------------------
    try:
        # request.headers.get(): 大小写不敏感获取 HTTP Authorization 请求头
        token = _parse_bearer_token(request.headers.get("authorization"))
        # 验签、校验过期时间并反解 JWT payload 中的 sub 声明
        subject = decode_access_token(token)
        # 将 sub 转换为 UUID 实体（若格式非合法 UUID 字符串会抛出 ValueError）
        user_id = UUID(subject)
    except (AppException, ValueError) as exc:
        # 【语法重点】多异常联合捕获：
        # 1. AppException (包含 UnauthorizedError): 涵盖凭证缺失、过期、验签失败、算法不匹配等业务鉴权异常
        # 2. ValueError: 捕获 UUID(subject) 转换失败（脏数据或恶意伪造的非标准 sub）
        # 因 ValueError 属 Python 内置标准库异常，与 AppException 无继承关系，必须并列捕获，防止穿透导致服务报 500。
        #
        # 【安全考虑】对外统一模糊为"无效的访问凭证"，防止旁路攻击者推断令牌是过期还是伪造。
        # 【异常追溯】使用 `from exc` 显式保留异常链，确保服务端控制台保留底层原始报错堆栈以供排查。
        raise ToolError(_user_facing_message(exc, default="无效的访问凭证")) from exc

    # -------------------------------------------------------------------------
    # 步骤 3：在独立会话中查询用户数据（短生命周期）
    # -------------------------------------------------------------------------
    # 为什么手动管理 `async with AsyncSessionLocal()` 而不是注入 Depends？
    # MCP 工具函数脱离了 FastAPI 的路由依赖注入体系，需自行创建并关闭会话。
    # ⚠️ 规则：采用"即查即关"策略，查完立即释放/归还数据库连接池，不在函数外保持打开状态。
    async with AsyncSessionLocal() as session:
        user = await UserRepository(session).get_by_id(user_id)

    # -------------------------------------------------------------------------
    # 步骤 4：校验用户存在性
    # -------------------------------------------------------------------------
    if user is None:
        # 令牌签名与时效虽合法，但对应主体在数据库中已物理删除或软删除
        raise ToolError("用户不存在或已被删除")

    # -------------------------------------------------------------------------
    # 步骤 5：校验用户账号状态
    # -------------------------------------------------------------------------
    if user.status != UserStatus.ACTIVE:
        # 区分"身份认证失败"与"无权限"：此处为账号已被冻结/封禁，拒绝提供工具服务
        raise ToolError("账号已被停用")

    # ⚠️ 注意事项：返回的 user 已脱离 ORM 会话（Detached 状态），本函数的 session 已关闭。
    # 但 **不同属性的安全性不一样，别一概而论**：
    # - `user.roles` 是**安全的**：User.roles 声明了 lazy="selectin"，
    #   而 UserRepository.get_by_id 走的是 session.get(User, id)，会顺带把 roles 一起加载。
    #   （实测依据：app/db/models.py 中 User.roles 的 lazy="selectin" 注释。）
    # - **不安全的是"没被预加载的关联属性"**：一旦访问，SQLAlchemy 会尝试为这个已关闭的
    #   会话触发一次 IO，在异步引擎下抛的是 **MissingGreenlet**（同步 lazy="select" 会撞的那个），
    #   而不是 DetachedInstanceError。
    # 结论：后续工具里若要读关联数据，**自己新开 session 用仓储查**，不要指望这个实例。
    return user


# =============================================================================
# 第三步：工具级的管理员闸门
# =============================================================================


def require_admin(user: User) -> None:
    """admin-only 工具入口处调用；非 admin 直接抛 ToolError。

    :param user: 已由 resolve_current_user 认证过的用户
    :raises ToolError: 已认证但不是管理员

    【为什么判管理员复用 permission_service.is_admin，而不是自己判角色名】

    与网页侧的 `get_current_admin` 用的是**同一个函数**。
    角色体系将来若新增一种管理员（例如 audit_admin），只需改 `is_admin` 一处，
    两边同时生效 —— 这正是"不要新造鉴定口径"的实践。

    ⚠️ 与网页侧的差别同样只在出口：网页侧抛 `PermissionDeniedError`（HTTP 403），
    这里抛 `ToolError`（协议层错误文本）—— 因为 MCP 没有 403。
    """
    if not is_admin(user):
        raise ToolError("仅管理员可调用此工具")


# =============================================================================
# 第四步：异常文案的统一转换（把 AppException 降维成一句话）
# =============================================================================


def _user_facing_message(exc: Exception, *, default: str) -> str:
    """统一把 AppException.message 透出给 Agent；其它异常用兜底文案。

    :param exc: 被捕获的异常
    :param default: 非 AppException（以及无 message 的异常）的兜底文案
    :return: 可以安全展示给外部 Agent 的一句话

    【为什么需要这一层"翻译"】

    `AppException` 的子类都自带一段**写给人看的中文 message**
    （"请先登录" / "账号已被停用" / "无效的访问凭证"），
    这正是 Agent 最能利用的信息 —— 模型可以据此告诉用户该做什么。

    而 `ValueError`、`AttributeError` 这类原生异常没有这个字段，
    它们的 `str()` 往往是内部实现细节（甚至可能带栈信息），
    **不适合直接透给外部调用方**，所以统一换成兜底文案。

    【`default` 的真实用途：兜住"没带 message 的那一半"】

    本函数唯一的调用点在 `resolve_current_user`，那里的 `except` 同时捕获了
    `AppException` **与 `ValueError`**。前者有 message，后者没有 ——
    `default` 就是给后者准备的（调用处传的是 `"无效的访问凭证"`）。
    少了它，脏 sub 导致的 `ValueError` 就会把英文原文（如
    `badly formed hexadecimal UUID string`）直接暴露给 Agent。

    【为什么用 isinstance 判断，而不是 getattr(exc, "message", default)】

    两种写法都能跑通，这里选 isinstance 有两个理由：
    1. **类型收窄**：isinstance 之后静态检查器知道 exc 就是 AppException，`exc.message` 取得到；
       getattr 则会把类型信息丢掉，将来 `AppException.message` 改名也不会有任何提示；
    2. **语义诚实**：本函数要表达的是"只把**业务异常**的文案透出去"，
       而不是"凡是碰巧带 message 属性的异常都放行"—— 后者等于给任意异常开了后门。
    """
    if isinstance(exc, AppException):
        return exc.message
    return default
