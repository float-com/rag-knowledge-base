"""认证与权限 API 契约（第 11 期）。

【模块职责说明】
1. 协议契约声明（API Contract Declaration）：
   用 Pydantic 模型统一描述认证模块对外的请求体与响应体，避免把 SQLAlchemy 实体
   （User / Role）直接暴露到 HTTP 边界。路由层与 Service 层之间只通过本模块的模型交换数据。

2. 枚举契约的"单一事实源"问题：
   `UserStatusValue` 必须与 `app/db/models.py` 的 `UserStatus` **逐字对齐**（值 + 顺序）。
   本项目此刻它存在 2 份拷贝（models.py / 本文件）+ 1 份前端 types.gen.ts 里的联合类型。
   改动任何一处都要同步其它几处。

3. 字段集合与前端契约严格对齐：
   本模块的 `UserRead` / `RoleRead` 必须与 `frontend/src/client/types.gen.ts` 里的同名类型
   逐字段一致。前端类型是**手工维护在前**的契约（它比后端更早写好），
   后端只是把它兑现出来 —— 多一个字段或少一个字段都算契约漂移。
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# =============================================================================
# 1. 字面量契约（Literal 枚举）
# =============================================================================
# 与 app/db/models.py 的 UserStatus 对齐：
#   物理列是 String(16)，这里用 Literal 让 OpenAPI 直接输出候选值字符串数组，
#   前端生成 TS 时得到 `'active' | 'disabled'` 的联合类型，无需额外映射表。
UserStatusValue = Literal["active", "disabled"]


# =============================================================================
# 2. 角色读模型
# =============================================================================
class RoleRead(BaseModel):
    """角色响应体：角色本身的元信息 + 它持有的权限标签。

    【为什么这个模型只暴露 5 个字段、没有 Role.users】
    角色与用户是多对多：一个角色可能被成百上千个用户持有。
    若在这里带上 users，查一个用户就会连带拉出整棵"用户-角色-用户"的树，
    既浪费带宽也容易递归膨胀。需要"这个角色有哪些用户"时，
    应当去 `/api/users` 按角色筛选，而不是从角色对象反查。
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str
    # 【为什么用 Field(default_factory=list) 而不是 = []】：
    # 直接写 [] 会让所有实例共享同一个列表对象，是 Pydantic 的经典可变默认值陷阱。
    permission_tags: list[str] = Field(default_factory=list)
    created_at: datetime


# =============================================================================
# 3. 用户读模型
# =============================================================================
class UserRead(BaseModel):
    """用户响应体。

    ⚠️ **本模型绝不包含 `password_hash`** —— 密码哈希是服务端内部字段，
    任何情况下都不该出现在 HTTP 响应里。它不在本模型的字段列表中，
    因此即使 ORM 实体上有这个属性，序列化时也不会被带出去。

    【roles 为什么用 Field(default_factory=list)】
    ORM 的 `User.roles` 是 relationship。若某个取数路径没有加载它，Pydantic 会取不到值。
    用 default_factory 兜底成空列表，比返回 `null` 对前端更友好（前端不必写 `?? []`）。
    本项目模型上已配 `lazy="selectin"`，正常情况下 roles 总是已加载。

    【时间戳的用途（不是抄教程的样板字段）】
    `updated_at` 是 users 表【唯一】能回答"这个账号什么时候被改过"的字段：
    出现安全事件后要查"攻击者拿到 admin 后有没有改过密码 / 加过账号"，
    只能靠它。前端 UsersPage 目前不展示这两列，但契约里保留它们是正确的 ——
    审计信息属于"现在不用、将来必须有"的那一类。
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    username: str
    display_name: str
    status: UserStatusValue
    # 角色列表里只暴露角色本身的必要字段（RoleRead），不回传 Role.users，
    # 避免"查一个用户却带出一整棵角色-用户树"的递归膨胀。
    roles: list[RoleRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


# =============================================================================
# 4. 登录请求 / 响应
# =============================================================================
class LoginRequest(BaseModel):
    """登录请求体。

    【为什么这里不做长度校验（min_length / max_length）】
    登录接口的输入校验策略与"创建用户"不同：
    - 创建用户时校验密码长度是**业务规则**（我们规定密码至少 4 位），要给出明确的 400；
    - 登录时若对格式做限制，会向外泄露"哪些输入格式才是合法账号"的信息。
      登录失败一律回 401「用户名或密码错误」，不区分是"格式不对"还是"密码不对"。
    """

    username: str
    password: str


class LoginResponse(BaseModel):
    """登录成功响应体。

    【为什么要同时返回 token 和用户信息 + 权限标签】
    登录是前端**唯一**必须立刻拿到完整身份的一次交互：
    - `access_token`：后续所有请求的凭据；
    - `user`：界面右上角显示昵称、判断是否展示管理菜单；
    - `permission_tags`：前端可据此提前隐藏无权限的入口（**仅是体验优化，
      真正的权限拦截仍在后端** —— 前端隐藏按钮永远不能当成安全措施）；
    - `is_admin`：最常用的一个布尔，单独给出省去前端从 tags 里找 `"*"`。

    若只返回 token，前端登录后还得再发一次 `/auth/me`，多一次往返。
    """

    access_token: str
    # 固定为 "bearer"：这是 OAuth2 的约定，前端拼请求头时用它作前缀。
    # 用 Literal 而不是 str，让 OpenAPI 文档里直接显示可选值。
    token_type: Literal["bearer"] = "bearer"
    user: UserRead
    permission_tags: list[str] = Field(default_factory=list)
    is_admin: bool


# =============================================================================
# 5. 当前登录用户视图
# =============================================================================
class MeResponse(BaseModel):
    """当前登录用户视图：用户基础信息 + 合并后的有效权限标签 + 是否管理员。

    【与 LoginResponse 的区别】：少了 `access_token`。
    `/auth/me` 的调用方本来就持有令牌（不然过不了认证），没必要再把令牌回显一遍 ——
    回显反而多一次令牌在网络上来回的机会。

    【前端什么时候调它】
    页面启动时与路由切换时各拉一次，用来保证"管理员刚才改了我的角色"能立即反映到界面上。
    这正好呼应第 5 步的设计：认证时每次请求都重查数据库，因此本接口天然返回最新权限。
    """

    user: UserRead
    permission_tags: list[str] = Field(default_factory=list)
    is_admin: bool
