"""用户管理路由层（第 11 期，第 7 章）。

【模块职责说明】
把用户管理能力暴露成 5 个端点，统一挂在 `/api/users` 之下：

| 方法 | 路径 | operationId | 说明 |
| --- | --- | --- | --- |
| GET | `` | `listUsers` | 分页列表 |
| POST | `` | `createUser` | 新建（201） |
| PATCH | `/{user_id}` | `updateUser` | 改昵称 / 状态 / 密码 |
| PUT | `/{user_id}/roles` | `assignUserRoles` | 整体替换角色 |
| DELETE | `/{user_id}` | `deleteUser` | 删除（204） |

【⚠️ 本文件与 auth 路由最大的区别：全部端点都要求 CurrentAdmin】
`app/api/routes/auth.py` 里 `/login` 是公开的、`/me` 只要登录；
而这里是**管理面**：普通用户连自己的用户记录都不该通过本组接口读改
（`/me` 已经提供了"看自己"的能力）。

因此每个路由的第一个参数都是 `_: CurrentAdmin` —— 用下划线命名表示"只为触发依赖，
函数体里用不到这个值"。FastAPI 仍然会执行整个认证 + 鉴权链，不通过就 401/403。

【依赖说明】
   - `CurrentAdmin`：认证 + 鉴权依赖，声明即"必须管理员"；
   - `DbSession`：请求级会话；
   - `UserService`：第 4 步写好的业务层，本文件只做"参数转发 + 实体转 DTO"。
"""

from uuid import UUID

from fastapi import APIRouter, Query, Response

from app.api.deps import CurrentAdmin, DbSession
from app.api.schemas.auth import UserRead
from app.api.schemas.users import (
    AssignRolesRequest,
    UserCreate,
    UserPage,
    UserUpdate,
)
from app.core.exceptions import ValidationError
from app.db.models import UserStatus
from app.services.user_service import UserService

# 语法（APIRouter 路由分组）：APIRouter(prefix="/users", tags=["users"])
#   路径拼接公式 = 全局前缀 [/api] + 模块前缀 [/users] + 端点子路径；
#   Swagger UI 会把这一组单独折叠成 "users" 分组。
router = APIRouter(prefix="/users", tags=["users"])


# =============================================================================
# 1. 用户列表（分页）
# =============================================================================
@router.get("", response_model=UserPage, operation_id="listUsers")
async def list_users(
    _: CurrentAdmin,
    session: DbSession,
    # 分页参数在签名上做"框架级"校验（越界 → 422）；
    # 仓储层还会再 max(min(page_size, 100), 1) 兜一次 —— 因为它可能被脚本直接调用，
    # 不能假设调用方一定走 HTTP。两道防线各管一段。
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> UserPage:
    """分页列出用户，按创建时间正序。

    【为什么要手动拼 Page 而不是直接返回元组】
    服务层返回的是 `(ORM 实体列表, total)`，本层需要做两件事：
    ① 把每个 ORM 实体转成 `UserRead`（脱敏、裁字段）；
    ② 补齐 page / page_size 这两个"请求侧"信息（服务层只关心数据，不关心页码）。
    """
    service = UserService(session)
    items, total = await service.list_users(page, page_size)
    return UserPage(
        # 列表推导里逐个 model_validate：UserRead 会自动带上 roles（模型配了 lazy="selectin"）
        items=[UserRead.model_validate(u) for u in items],
        total=total,
        page=page,
        page_size=page_size,
    )


# =============================================================================
# 2. 创建用户
# =============================================================================
@router.post("", response_model=UserRead, status_code=201, operation_id="createUser")
async def create_user(
    _: CurrentAdmin, session: DbSession, payload: UserCreate
) -> UserRead:
    """创建用户，可选地一次性分配角色。

    【为什么是 201 而不是 200】
    新建资源成功的标准状态码。前端 SDK 也是按 201 声明响应的
    （`CreateUserResponses = { 201: UserRead }`），对不上的话生成的类型会失效。

    【异常契约】
    - 用户名重复 → `ConflictError`（409）
    - 用户名/密码/角色 id 不合法 → Service 抛 `ValidationError`（400）
    - 请求体字段缺失或超长 → 框架自动 422
    """
    service = UserService(session)
    user = await service.create_user(
        username=payload.username,
        password=payload.password,
        display_name=payload.display_name,
        role_ids=payload.role_ids,
    )
    return UserRead.model_validate(user)


# =============================================================================
# 3. 更新用户（PATCH 部分更新）
# =============================================================================
@router.patch("/{user_id}", response_model=UserRead, operation_id="updateUser")
async def update_user(
    _: CurrentAdmin, session: DbSession, user_id: UUID, payload: UserUpdate
) -> UserRead:
    """修改用户的昵称 / 状态 / 密码（三个字段都可选）。

    【⚠️ 本文件唯一需要"手工转类型"的地方：status】
    `payload.status` 的静态类型是 `Literal["active","disabled"]`（字符串），
    而 Service 期望的是 `UserStatus` 枚举，数据库列也存枚举的 value。

    Pydantic 不会替我们做这一步，所以这里显式转换：

        status = UserStatus(payload.status)   # "active" -> UserStatus.ACTIVE

    【为什么要 try/except ValueError】
    `UserStatus(payload.status)` 对非法值会抛 `ValueError`。
    虽然 `UserStatusValue` 这个 Literal 已经把合法值限死了、正常路径不可能走到这里，
    但**多一层防御不亏**：万一将来有人放宽了 Literal 却忘了同步补枚举成员，
    这里会把 `ValueError` 转成 400（而不是让它冒成 500）。
    `from exc` 保留原始异常链，方便排障时看到真实原因。

    【为什么 None 可以原样透传】
    `UserStatus | None = None` 且"传 None = 本次不改这一项"，
    所以 `if payload.status is not None` 只在真有新状态时才转换。
    """
    status: UserStatus | None = None
    if payload.status is not None:
        try:
            status = UserStatus(payload.status)
        except ValueError as exc:
            raise ValidationError("非法的用户状态") from exc

    service = UserService(session)
    user = await service.update_user(
        user_id,
        display_name=payload.display_name,
        status=status,
        password=payload.password,
    )
    return UserRead.model_validate(user)


# =============================================================================
# 4. 分配角色（整体替换）
# =============================================================================
@router.put("/{user_id}/roles", response_model=UserRead, operation_id="assignUserRoles")
async def assign_user_roles(
    _: CurrentAdmin, session: DbSession, user_id: UUID, payload: AssignRolesRequest
) -> UserRead:
    """整体替换用户的角色集合。

    【为什么用 PUT】：语义是"把角色设成这份清单"，幂等 —— 同一请求执行两次结果相同。

    【注意方法名】：Service 侧的方法叫 `set_user_roles`（不是 `set_roles`）。
    之所以带 `user_` 前缀，是因为 `RoleService` 里也有"设置角色"语义的方法，
    加上前缀能让调用点一眼看出在操作哪一侧。

    传空列表 = 收回该用户所有角色（合法的"暂时停权"操作）。
    """
    service = UserService(session)
    user = await service.set_user_roles(user_id, payload.role_ids)
    return UserRead.model_validate(user)


# =============================================================================
# 5. 删除用户
# =============================================================================
@router.delete("/{user_id}", status_code=204, operation_id="deleteUser")
async def delete_user(
    admin: CurrentAdmin, session: DbSession, user_id: UUID
) -> Response:
    """物理删除用户。

    【为什么本函数的 CurrentAdmin 参数不叫 `_` 而叫 `admin`】
    唯一一个**函数体里要用到当前登录者身份**的端点：下面的防呆需要比对
    "被删的人"与"正在操作的人"是不是同一个。

    【防呆：不允许删除自己】
    这是本项目在用户删除上唯一的业务护栏，防的是最尴尬的一种事故：
    **管理员把自己删掉之后，系统里可能一个管理员都不剩**，
    而"再建一个管理员"这件事本身就需要管理员权限 —— 直接把自己锁在系统外。

    注意它与角色侧的护栏是**不同层次**的保护：
    - `RoleService.delete_role` 保护的是**内置角色**（admin/user 角色本身不可删）；
    - 这里保护的是**当前操作者**（不能删自己）。
    两者可以同时成立：管理员 A 可以删管理员 B（若确需如此），但删不掉自己。

    【为什么返回 `Response(status_code=204)` 而不是普通返回值】
    204 No Content 按 HTTP 规范**不允许携带响应体**，
    所以必须显式返回一个空的 `Response`；装饰器上的 `status_code=204` 是给 OpenAPI 文档用的声明。
    """
    if admin.id == user_id:
        # 防呆：admin 不能把自己删了（否则可能把系统里最后一个管理员抹掉）
        raise ValidationError("不能删除当前登录的管理员账号")

    service = UserService(session)
    await service.delete_user(user_id)
    return Response(status_code=204)
