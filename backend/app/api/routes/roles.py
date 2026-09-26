"""角色管理路由层（第 11 期，第 7 章）。

【模块职责说明】
把角色管理能力暴露成 4 个端点，统一挂在 `/api/roles` 之下：

| 方法 | 路径 | operationId | 说明 |
| --- | --- | --- | --- |
| GET | `` | `listRoles` | 全量列表（**不分页**） |
| POST | `` | `createRole` | 新建（201） |
| PATCH | `/{role_id}` | `updateRole` | 改描述与权限标签 |
| DELETE | `/{role_id}` | `deleteRole` | 删除（204） |

【与 users 路由同构的部分】
- 同样全部要求 `CurrentAdmin`（管理面）；
- 同样只是"参数转发 + 实体转 DTO"，业务规则都在 `RoleService` 里。

【三处刻意的差异】
1. **列表不分页**：角色总量天然极小（内置 2 个 + 业务角色通常个位数），
   强行分页只会让前端多写一套翻页逻辑，收益为零。返回的是**裸数组**而不是 Page 对象
   —— 这一点必须与前端契约一致（`ListRolesResponses = { 200: Array<RoleRead> }`）。
2. **`updateRole` 的请求体里没有 `name`**：策略代码以角色名为锚，不允许改名。
   契约层不提供字段 + 业务层没有参数，构成双层防呆（详见 `schemas/roles.py`）。
3. **删除内置角色由 Service 拦**：本层不判断角色名，直接交给
   `RoleService.delete_role` —— 那里有 `PROTECTED_ROLE_NAMES` 白名单。
"""

from uuid import UUID

from fastapi import APIRouter, Response

from app.api.deps import CurrentAdmin, DbSession
from app.api.schemas.auth import RoleRead
from app.api.schemas.roles import RoleCreate, RoleUpdate
from app.services.role_service import RoleService

# 语法（APIRouter 路由分组）：APIRouter(prefix="/roles", tags=["roles"])
router = APIRouter(prefix="/roles", tags=["roles"])


# =============================================================================
# 1. 角色列表
# =============================================================================
@router.get("", response_model=list[RoleRead], operation_id="listRoles")
async def list_roles(_: CurrentAdmin, session: DbSession) -> list[RoleRead]:
    """列出全部角色。

    【返回裸数组，而不是 {items, total, page, page_size}】
    这是与 users 列表**刻意的不对称**，原因是量级：
    角色是"配置型数据"（个位数量级），前端一次性全取回来做下拉框即可，
    不需要翻页。`response_model=list[RoleRead]` 让 OpenAPI 直接输出数组类型，
    与前端 SDK 声明的 `Array<RoleRead>` 一致。
    """
    service = RoleService(session)
    roles = await service.list_roles()
    return [RoleRead.model_validate(r) for r in roles]


# =============================================================================
# 2. 创建角色
# =============================================================================
@router.post("", response_model=RoleRead, status_code=201, operation_id="createRole")
async def create_role(
    _: CurrentAdmin, session: DbSession, payload: RoleCreate
) -> RoleRead:
    """创建角色。

    【异常契约】
    - 角色名为空 → `ValidationError`（400，由 Service 在 strip 之后判断）
    - 角色名已存在 → `ConflictError`（409）
    - 名称超长/缺失 → 框架 422

    【权限标签会被 Service 标准化】
    去空白、去空串、去重且**保序**。这不是"顺手清理"，而是必要的：
    若存进一个带尾随空格的 `"hr "`，第 7 步的数组重叠运算永远不会命中，
    表现为"权限明明配了却不生效"，极难排查。
    """
    service = RoleService(session)
    role = await service.create_role(
        name=payload.name,
        description=payload.description,
        permission_tags=payload.permission_tags,
    )
    return RoleRead.model_validate(role)


# =============================================================================
# 3. 更新角色
# =============================================================================
@router.patch("/{role_id}", response_model=RoleRead, operation_id="updateRole")
async def update_role(
    _: CurrentAdmin, session: DbSession, role_id: UUID, payload: RoleUpdate
) -> RoleRead:
    """修改角色的描述与权限标签（两个字段都可选）。

    【为什么没有"name 不能改"的校验代码】
    因为 `RoleUpdate` schema 里**根本没有 name 字段** —— 传了也会被 Pydantic 忽略。
    运行期校验能省则省：字段不存在比"存在但拒绝"更彻底。

    【内置角色可以改描述与标签，但不能改名、不能删】
    这条边界由 Service 侧区分实现：
    - `update_role` 用私有的 `_get_role_for_write`（只查存在性，不做保护）；
    - `delete_role` 才检查 `PROTECTED_ROLE_NAMES`。
    所以给 admin 补一段说明文案是允许的，删掉它则会被拒。
    """
    service = RoleService(session)
    role = await service.update_role(
        role_id,
        description=payload.description,
        permission_tags=payload.permission_tags,
    )
    return RoleRead.model_validate(role)


# =============================================================================
# 4. 删除角色
# =============================================================================
@router.delete("/{role_id}", status_code=204, operation_id="deleteRole")
async def delete_role(
    _: CurrentAdmin, session: DbSession, role_id: UUID
) -> Response:
    """删除角色。

    【内置角色不可删】
    `RoleService.delete_role` 里有白名单 `PROTECTED_ROLE_NAMES = {"admin", "user"}`，
    命中则抛 `ValidationError`（400）。本层不做判断 ——
    业务规则只应有一处实现，否则将来加第三个内置角色时会漏改。

    【数据库侧的连带影响】
    `user_roles` 的外键是 ON DELETE CASCADE，所以持有该角色的用户会被自动摘掉关联行；
    用户本身不受影响，只是少了一个权限来源。

    【为什么不做成幂等静默成功】
    删一个不存在的角色返回 404，是为了向前端**暴露状态不一致**
    （例如用户在两个标签页里重复删除）。
    """
    service = RoleService(session)
    await service.delete_role(role_id)
    return Response(status_code=204)
