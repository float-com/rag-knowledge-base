"""FastAPI 依赖项汇总。"""

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import PermissionDeniedError, UnauthorizedError
from app.core.security import decode_access_token
from app.db.models import User, UserStatus
from app.db.repositories.user_repo import UserRepository
from app.db.session import get_session
from app.services.permission_service import is_admin

# ===== 依赖项类型别名（FastAPI 推荐的 Annotated 语法糖） =====
#
# 【设计目的】：
# 将传统的参数依赖声明（如 session: AsyncSession = Depends(get_session)）
# 打包为一个全局可复用的类型别名，实现“类型提示”与“框架注入”的解耦与复用。
#
# 【语法糖拆解 - Annotated[T, Metadata]】：
# 1. 第一个参数 (AsyncSession)：
#    提供真实的静态类型信息，IDE（如 PyCharm）和 mypy 据此提供代码自动补全与类型检查。
# 2. 第二个参数 (Depends(get_session))：
#    作为元数据挂载。FastAPI 运行时会解析该元数据，自动触发 get_session 获取连接、
#    执行请求级注入并在请求结束后关闭连接。
#
# 【使用收益】：
# - 统一依赖源：若底层 Session 获取方式变更，仅需在此处修改一次即可全局生效；
# - 代码极其简洁：后续路由只需书写 `async def api_name(db: DbSession):`，避免重复声明长串依赖。
DbSession = Annotated[AsyncSession, Depends(get_session)]


# =============================================================================
# 认证依赖（第 11 期）：从 Bearer 令牌 → 用户实体 → 管理员判定
# =============================================================================
#
# 【这一层解决什么问题】
# 前面几步已经备好了零件：security.py 能验令牌、permission_service 能判管理员、
# user_repo 能查用户。但它们是三个各自独立的能力，路由层要用它们，就得每个接口
# 都手写一遍「取请求头 → 验签 → 查库 → 判状态」——既啰嗦又容易漏。
#
# 依赖注入把这条链打包成两个可复用的「类型别名」：
#     async def some_route(user: CurrentUser, session: DbSession): ...
#     async def admin_only(user: CurrentAdmin, session: DbSession): ...
# 路由函数只需声明参数类型，认证与鉴权就自动完成。
#
# 【为什么分成三个函数而不是一个】
# _parse_bearer_token 只做「从请求头抠出令牌」这一件纯字符串的事，
# 与数据库、与业务都无关。拆出来既能被 get_current_user 复用，
# 也便于单独测试（本节验收里就单独测了它）。


def _parse_bearer_token(authorization: str | None) -> str:
    """从 Authorization 请求头里取出 Bearer 令牌。

    :param authorization: 原始请求头值，形如 `Bearer eyJhbGciOi...`；未携带时为 None
    :return: 去掉 `Bearer ` 前缀后的纯令牌串
    :raises UnauthorizedError: 未携带请求头（未登录），或格式不符合 Bearer 规范

    【为什么未携带和格式错误要给不同的文案】
    两者都返回 401，但语义不同：
    - 没带请求头 → 用户就是没登录，提示"请先登录"，前端跳登录页是正确反应；
    - 带了但不是 Bearer → 多半是前端拼错了请求头（例如漏了 "Bearer " 前缀），
      提示"无效的访问凭证"能让开发者更快定位到是客户端构造请求的问题。

    【为什么不区分大小写】
    HTTP 规范里认证方案名（Bearer）是大小写不敏感的，`bearer` / `BEARER` 都合法。
    因此 scheme 部分统一 lower() 之后再比较。
    """
    if not authorization:
        raise UnauthorizedError("请先登录")

    # -------------------------------------------------------------------------
    # 语法拆解：str.partition(" ") 与防御性解包（Defensive Unpacking）
    # -------------------------------------------------------------------------
    # 【语法知识点】：
    # 1. str.partition(sep)：
    #    - 永远且严格返回一个长度固定为 3 的元组：(分隔符左侧文本, 分隔符本身, 分隔符右侧剩余文本)；
    #    - 若找不到分隔符（如前端只传了 "Bearer"），会返回 ("Bearer", "", "")，绝不抛异常。
    # 2. 下划线 `_`（哑变量 / Throwaway Variable）：
    #    - 用于接收中间那个被切出来的空格字符串 " "；表示占位但代码后续不使用。
    #
    # 【为什么弃用 split(" ") 而必须选 partition(" ")】：
    # - 传统 split(" ") 的返回列表长度取决于空格数量：
    #   * 若客户端传 "Bearer"（无空格）：split 仅产出 1 个元素，解包时抛 ValueError (not enough values)；
    #   * 若客户端传 "Bearer a b c"（多空格）：split 产出 4 个元素，解包时抛 ValueError (too many values)；
    #   两种情况都会把未处理异常向外抛出，导致 API 接口崩成 HTTP 500 Internal Server Error。
    # - partition(" ") 保证恒定产出 3 个元素，解包永远安全成立，将异常防御收敛到后续的业务校验中。
    scheme, _, token = authorization.partition(" ")

    # 空格后必须有内容：`"Bearer "` 这种只有前缀没有令牌的情况同样要拒绝
    if scheme.lower() != "bearer" or not token:
        raise UnauthorizedError("无效的访问凭证")

    return token


async def get_current_user(
    session: Annotated[AsyncSession, Depends(get_session)],
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> User:
    """解析 Bearer 令牌并查出当前登录用户。

    :param session: 请求级数据库会话（与路由里的 DbSession 是同一个实例，见下方说明）
    :param authorization: FastAPI 从 `Authorization` 请求头自动注入
    :return: 已认证且状态正常的 User 实体（roles 已加载）
    :raises UnauthorizedError: 令牌缺失 / 格式错误 / 过期 / 签名无效 / 用户不存在 / 账号被停用

    【为什么每次请求都要重新查一次库，而不是把用户信息塞进令牌里】
    JWT 是自包含的，完全可以签发时就带上角色名与状态，省掉这一查。但本项目刻意不这么做：

    - **角色变更立即生效**：管理员把某人的 admin 角色收回，如果他手上的令牌里
      还写着"我是 admin"，那在令牌过期前他依然能进管理接口 —— 这是不可接受的。
    - **状态变更立即生效**：停用一个账号应当立刻生效，而不是"最多等 24 小时"。
    - **令牌里信息越少越安全**：令牌一旦泄露就是明文可读（Base64 不是加密），
      里面只放一个 user_id，泄露面最小。放的角色越多，泄露的信息越多。

    代价是每个受保护请求多一次主键点查 —— 这是可以接受的：
    它是走索引的单行查询，且令牌过期时间长达 1440 分钟，
    用一次廉价查询换取"权限变更立即生效"，这笔交易是划算的。

    【关于 session 复用】
    本函数与路由函数都依赖 `get_session`，FastAPI 的依赖缓存会把**同一个** session
    实例注入进来。因此这里查出的 User 与路由里后续操作的 User 处于同一事务上下文，
    既能共享身份映射（Identity Map，同一行不会查两次），也避免了跨会话的对象游离问题。
    """
    token = _parse_bearer_token(authorization)

    # 验签 + 校验 exp + 校验算法白名单，失败已被 security 层统一转成 401
    subject = decode_access_token(token)

    # sub 是签发时写进去的 user_id 字符串。它虽经签名保护，但类型仍不可信 ——
    # 若库里存进了非 UUID 的脏数据、或将来换了签发逻辑，这里必须挡住而不是抛 500。
    try:
        user_id = UUID(subject)
    except (ValueError, TypeError) as exc:
        raise UnauthorizedError("无效的访问凭证") from exc

    user = await UserRepository(session).get_by_id(user_id)
    if user is None:
        # 令牌有效但用户已被删除：属于"凭证仍然合法、主体已不存在"
        raise UnauthorizedError("用户不存在或已被删除")

    if user.status != UserStatus.ACTIVE:
        # 账号被停用：401 而不是 403 —— 停用意味着"你的身份不再被承认"，
        # 与 403（身份有效但权限不够）是两回事。
        raise UnauthorizedError("账号已被停用")

    return user


async def get_current_admin(
    user: Annotated[User, Depends(get_current_user)],
) -> User:
    """在当前登录用户的基础上，额外要求其为管理员。

    :param user: 由 get_current_user 注入的已认证用户
    :return: 同一个 User 实体（供路由直接使用）
    :raises PermissionDeniedError: 已登录但不是管理员（403）

    【为什么是 403 而不是 401】
    401 表示"你是谁没证明成功"，403 表示"身份已证明，但权限不够"。
    这里用户已经通过令牌认证，只是不持有 admin 角色 —— 属于后者。
    两者对前端的含义完全不同：401 要清登录态并跳登录页，403 只提示"无权访问"
    （重登也不会变得有权限）。

    【为什么复用 get_current_user 而不是重写一遍】
    写成一个依赖套另一个依赖，FastAPI 会先跑内层。
    这样认证逻辑只有一份，将来加"令牌黑名单"之类的机制只需改一处。
    """
    if not is_admin(user):
        raise PermissionDeniedError("仅管理员可访问")
    return user


# =============================================================================
# 认证类型别名：通过 PEP 593 Annotated 将「类型提示」与「FastAPI 依赖注入」打包
# - 第一个参数 User：给 IDE 看的，提供 user.id / user.username 等代码自动补全。
# - 第二个参数 Depends(...)：给 FastAPI 看的运行时元数据，负责执行校验并将用户实体注入参数。
# =============================================================================

# 【普通登录用户】：必须登录（未登录/Token 过期直接报 401）
# 用法：@router.get("/me")
#      async def get_me(user: CurrentUser): return user.id
CurrentUser = Annotated[User, Depends(get_current_user)]

# 【系统管理员】：必须登录且必须持有 admin 角色（非超管直接报 403）
# 用法：@router.delete("/users/{id}")
#      async def delete_user(admin: CurrentAdmin): return "ok"
CurrentAdmin = Annotated[User, Depends(get_current_admin)]