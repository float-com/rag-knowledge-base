"""认证路由层（第 11 期）。

【模块职责说明】
1. 协议入口与能力暴露（RESTful Endpoint）：
   把认证能力暴露成两个端点，统一挂在 `/api/auth` 之下：
   - `POST /login`：用账号密码换令牌（**唯一不需要认证的端点**）
   - `GET  /me`   ：取当前登录用户的完整身份视图

2. 依赖注入带来的"声明即生效"：
   `/me` 只需要在签名里写 `user: CurrentUser`，认证就自动完成 ——
   取请求头、验令牌、查库、判状态全部由 deps.py 的依赖完成，本文件一行认证代码都没有。

3. 契约校验下沉到框架（Schema-driven Validation）：
   请求体与响应体都由 Pydantic 模型约束，本文件只负责"调服务、拼响应"。

【依赖说明】
   - `DbSession`：`app/api/deps.py` 里的请求级会话类型别名；
   - `CurrentUser`：认证依赖，声明即"必须登录"；
   - `AuthService`：登录校验与令牌签发；
   - `compute_user_permission_tags` / `is_admin`：权限计算（纯函数）。
"""

from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession
from app.api.schemas.auth import LoginRequest, LoginResponse, MeResponse, UserRead
from app.core.exceptions import UnauthorizedError
from app.services.auth_service import AuthService
from app.services.permission_service import compute_user_permission_tags, is_admin

# 语法（APIRouter 路由分组）：APIRouter(prefix="/auth", tags=["auth"])
#   路径拼接公式 = 全局前缀 [/api] + 模块前缀 [/auth] + 端点子路径；
#   Swagger UI 会把这一组单独折叠成 "auth" 分组。
router = APIRouter(prefix="/auth", tags=["auth"])


# =============================================================================
# 1. 登录：用账号密码换令牌
# =============================================================================
# operation_id 必须与前端 types.gen.ts / sdk.gen.ts 里已写死的契约逐字一致，
# 否则前端生成的 SDK 函数名会对不上。
@router.post("/login", response_model=LoginResponse, operation_id="login")
async def login(payload: LoginRequest, session: DbSession) -> LoginResponse:
    """校验账号密码并签发访问令牌。

    【401 而不是 400】
    账号密码不对属于"身份没证明成功"，用 401（未认证）而不是 400（参数不合法）——
    因为请求体格式完全合法，问题出在凭据本身。

    【为什么文案统一成"用户名或密码错误"】
    `authenticate` 已经把三种失败（用户名不存在 / 密码不对 / 账号停用）合并成同一个 None，
    这里必须沿用同一个笼统文案，否则前面那层合并就白做了 ——
    分开提示会让攻击者能拿用户名字典探测出"哪些账号真实存在"。

    【为什么这个端点不需要 CurrentUser 依赖】
    登录本身就是为了换取身份，此时还没有令牌可验。它是全项目唯一公开的业务端点。
    """
    service = AuthService(session)
    user = await service.authenticate(payload.username, payload.password)

    if user is None:
        # 统一文案，避免侧信道枚举"用户名是否存在"（详见 auth_service 的说明）
        raise UnauthorizedError("用户名或密码错误")

    token = AuthService.issue_token(user)

    # 响应里一次性带上前端登录后立刻要用的全部信息，省掉一次 /auth/me 往返。
    # permission_tags 在前端只用于"提前隐藏无权限入口"，真正的拦截仍在后端。
    return LoginResponse(
        access_token=token,
        user=UserRead.model_validate(user),
        permission_tags=compute_user_permission_tags(user),
        is_admin=is_admin(user),
    )


# =============================================================================
# 2. 当前登录用户视图
# =============================================================================
@router.get("/me", response_model=MeResponse, operation_id="getCurrentUser")
async def me(user: CurrentUser) -> MeResponse:
    """返回当前登录用户的身份、有效权限标签与是否管理员。

    【前端什么时候调它】
    页面启动时与路由切换时各拉一次。因为认证依赖每次请求都会重查数据库，
    所以本接口天然返回**最新**的角色与状态 —— 管理员刚改完某人的权限，
    对方下次切换页面就能看到变化，不需要重新登录。

    【为什么不像 login 那样返回 token】
    调用方本来就持有令牌（否则过不了本接口的认证），回显它只是让令牌多走一趟网络，
    没有任何收益。
    """
    return MeResponse(
        user=UserRead.model_validate(user),
        permission_tags=compute_user_permission_tags(user),
        is_admin=is_admin(user),
    )
