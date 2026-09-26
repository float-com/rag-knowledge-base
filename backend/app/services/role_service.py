"""角色服务（第 11 期）。

【模块职责说明】
RBAC 角色的业务层：列表 / 详情 / 创建 / 更新 / 删除。

【本模块的重点不是功能，而是"防呆"】
角色管理是唯一能让管理员**把自己锁死在系统外**的功能面。所以这里有三道防线：

1. 内置角色不可删（PROTECTED_ROLE_NAMES）：
   admin 被删掉 → 再也没有人能管理用户；
   user 被删掉 → 新用户的默认角色没了。
2. 角色名不可改（update_role 不接受 name 参数）：
   权限判断代码里写的是角色名（`is_admin` 比对 "admin"），
   如果允许改名，就会出现"代码以为你在找 admin，但库里已经改叫别的了"。
   用"参数不存在"这种最直白的防呆，比写一堆运行期校验更可靠。
3. 标签标准化（_normalize_tags）：
   去空白、去空串、去重 —— 避免管理员输入 "hr, " 这类带空格的值
   导致 SQL 数组重叠匹配永远不命中（那会变成一个很难排查的"权限明明配了却不生效"）。
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.tags import normalize_tags
from app.db.models import Role
from app.db.repositories.role_repo import RoleRepository

# 内置角色名：不允许删除。
# 【为什么用 frozenset】它是不可变集合，既表达了"这是一份常量白名单"，
# 也提供 O(1) 的 `in` 判断 —— 比 `in ("admin", "user")` 元组更贴合语义。
PROTECTED_ROLE_NAMES = frozenset({"admin", "user"})


class RoleService:
    """角色业务动作服务。"""

    def __init__(self, session: AsyncSession) -> None:
        """注入请求级异步会话，并初始化角色仓储。"""
        self.session = session
        self.repo = RoleRepository(session)

    async def list_roles(self) -> list[Role]:
        """列出全部角色（不分页，角色总量天然极小，理由见 RoleRepository）。"""
        return await self.repo.list_all()

    async def get_role(self, role_id: UUID) -> Role:
        """按主键取角色，不存在则抛 404。

        【为什么这里要把 None 转成异常，而不是让上层判空】
        角色是"写操作的落点"：更新 / 删除都要先拿到它。
        若返回 None，每个调用点都得写一遍 if role is None: raise ...
        收敛到本方法里，调用方就能假定"拿到的一定存在"。
        """
        role = await self.repo.get_by_id(role_id)
        if role is None:
            raise NotFoundError("角色不存在")
        return role

    async def create_role(
        self, *, name: str, description: str, permission_tags: list[str]
    ) -> Role:
        """创建角色。

        【强制关键字传参（* 语法）】
        name / description / permission_tags 都是字符串或字符串列表，
        位置传参时极易把 description 和 permission_tags 搞混（类型检查也救不了，
        因为 list[str] 和 str 不会被静态检查器当成同一个类型，但人眼会看错）。
        用 `*` 强制调用方写出键名，把这类错误挡在编译期。

        :raises ValidationError: 角色名为空（400）
        :raises ConflictError: 角色名已存在（409）
        """
        name = name.strip()
        if not name:
            raise ValidationError("角色名不能为空")

        if await self.repo.get_by_name(name) is not None:
            # 409 而不是 400：请求本身格式没问题，是"与现有资源的唯一性冲突"
            raise ConflictError(f"角色 {name} 已存在")

        role = Role(
            name=name,
            description=description.strip(),
            permission_tags=_normalize_tags(permission_tags),
        )
        await self.repo.add(role)
        # 事务边界由 Service 掌握：仓储只 flush，这里统一 commit
        await self.session.commit()
        return role

    async def update_role(
        self,
        role_id: UUID,
        *,
        description: str | None = None,
        permission_tags: list[str] | None = None,
    ) -> Role:
        """更新角色的描述与权限标签。

        【入参全是可选，"传 None = 本次不改这一项"（PATCH 语义）】
        与第 10 期评测模块的 `update_item_bad_case` 保持同一套约定，前端不需要
        为了改一个描述而回传完整对象。

        【⚠️ 刻意不接受 name 参数】
        角色名是权限判断的依据（`is_admin` 比对 "admin"）。允许改名会导致：
        代码里认的是 "admin"，而库里已被改成别的名字 → 管理员突然失去权限。
        与其在运行期检测"你是不是在改内置角色名"，
        不如**让这个参数根本不存在** —— 最直白的防呆。

        :raises NotFoundError: 角色不存在（404）
        """
        # 这里用私有的 _get_role_for_write 而不是公开的 get_role：
        # 内置角色【允许】改描述和标签（比如给 admin 补一段说明），只是不允许改名和删除。
        role = await self._get_role_for_write(role_id)

        if description is not None:
            role.description = description.strip()
        if permission_tags is not None:
            role.permission_tags = _normalize_tags(permission_tags)

        await self.session.commit()
        return role

    async def delete_role(self, role_id: UUID) -> None:
        """删除角色。

        【内置角色不可删】见模块 docstring 的第 1 条防线。

        【数据库侧会发生什么】
        user_roles 的外键是 ON DELETE CASCADE，所以持有该角色的用户
        会自动被摘掉这条关联 —— 用户本身不受影响，只是少了一个权限来源。

        :raises NotFoundError: 角色不存在（404）
        :raises ValidationError: 试图删除内置角色（400）
        """
        role = await self._get_role_for_write(role_id)
        if role.name in PROTECTED_ROLE_NAMES:
            raise ValidationError(f"内置角色 {role.name} 不允许删除")

        await self.repo.delete(role)
        await self.session.commit()

    async def _get_role_for_write(self, role_id: UUID) -> Role:
        """写操作专用取角色：只做存在性校验，不做内置角色保护。

        【为什么需要这个私有方法】
        公开的 `get_role` 若也带上"内置角色"保护，`update_role` 就没法更新内置角色了
        （它内部正是通过 get_role 取对象）。把"取值"与"是否允许该操作"分开，
        保护规则各自写在真正需要它的方法里。
        """
        role = await self.repo.get_by_id(role_id)
        if role is None:
            raise NotFoundError("角色不存在")
        return role


def _normalize_tags(tags: list[str]) -> list[str]:
    """标准化权限标签：去空白、丢弃空串、去重，并保持稳定顺序。

    【第 11 期 · 实现已上收到 `app/core/tags.py`，本函数保留为兼容垫片】
    原先本模块与 `document_service` / `user_service` 各留一份完全相同的副本；
    第 11 期 `document_upload_service` 也需要清洗时，为免抄第四份而上收到 core。
    详见 `app/core/tags.py`。
    """
    return normalize_tags(tags)
