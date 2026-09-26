"""用户服务（第 11 期）。

【模块职责说明】
用户的增删改查 + 角色分配，属于"后台管理"性质的常规业务层。

【本模块的三处重点】
1. 密码只进不出：入参是明文，落库前必须经过 hash_password；
   任何返回给外层的对象都只带 password_hash，绝不可能回显明文。
2. 创建时必须在构造阶段就把 roles 传进去（见 create_user 的说明）——
   这是本项目最容易踩的一个异步坑。
3. 唯一性与长度校验放在 Service 层，而不是只依赖数据库约束：
   数据库的 UNIQUE 报错是一句难以理解的 IntegrityError，
   在这里先查一次能给出"用户名 x 已存在"这样能直接展示给用户的提示。
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.security import hash_password
from app.db.models import User, UserStatus
from app.db.repositories.role_repo import RoleRepository
from app.db.repositories.user_repo import UserRepository


class UserService:
    """用户业务动作服务。"""

    def __init__(self, session: AsyncSession) -> None:
        """注入请求级异步会话，并初始化用户 / 角色两个仓储。

        【为什么同时持有 RoleRepository】
        创建与分配角色时，前端传进来的是 role_id 列表，需要翻译成 Role 实体。
        让本服务直接持有角色仓储，比跨服务调用（UserService → RoleService）更简单，
        也避免服务之间的循环依赖。
        """
        self.session = session
        self.user_repo = UserRepository(session)
        self.role_repo = RoleRepository(session)

    async def list_users(self, page: int, page_size: int) -> tuple[list[User], int]:
        """分页列出用户。

        直接透传仓储：分页钳制（page >= 1、page_size <= 100）已在仓储层做过，
        理由是仓储也可能被脚本 / 离线任务直接调用，防线必须设在最靠近 SQL 的地方。
        """
        return await self.user_repo.list_paginated(page, page_size)

    async def get_user(self, user_id: UUID) -> User:
        """按主键取用户，不存在则抛 404。"""
        user = await self.user_repo.get_by_id(user_id)
        if user is None:
            raise NotFoundError("用户不存在")
        return user

    async def create_user(
        self,
        *,
        username: str,
        password: str,
        display_name: str,
        role_ids: Sequence[UUID] | None = None,
    ) -> User:
        """创建用户，并可选地一次性分配角色。

        :raises ValidationError: 用户名为空 / 密码短于 4 位（400）
        :raises ConflictError: 用户名已存在（409）

        【⚠️ 本项目最容易踩的坑：roles 必须在【构造阶段】就传进去】
        下面这行是错的：
            user = User(username=..., ...)      # roles 集合处于"未加载"态
            await self.user_repo.add(user)      # flush
            user.roles = roles                  # ← 赋值前要读旧集合 → 异步惰性加载 → 崩
        报错是：
            MissingGreenlet: greenlet_spawn has not been called ...

        原因：关系集合若没被初始化，SQLAlchemy 在赋值时会先去读旧值算差异，
        这个读取动作在异步驱动下必须发生在可 await 的上下文里，而属性访问做不到。

        【本项目的解法】在 `User(...)` 构造时就把列表给上（哪怕是空列表），
        集合即为"已加载的空集合"，后续赋值与 flush 都不再触发惰性加载。
        这也正是下面 `roles=[]` 与 `roles=roles` 两种写法并存的原因：
        不传角色时给 `[]` 而不是省略，就是为了显式初始化这个集合。

        【为什么角色数量要校验】
        仓储的 `get_many` 对不存在的 id 是"少返回"而不是报错。
        如果不比对数量，管理员传了一个已删除的角色 id，接口会静默成功、
        用户却少了一个角色 —— 这种"看起来成功实际没生效"最难排查，因此这里拦掉。
        """
        username = username.strip()
        display_name = display_name.strip() or username

        if not username:
            raise ValidationError("用户名不能为空")
        if len(password) < 4:
            raise ValidationError("密码长度至少 4 位")

        if await self.user_repo.get_by_username(username) is not None:
            raise ConflictError(f"用户名 {username} 已存在")

        # 先把角色实体查出来（此时还没有 User 对象，不涉及集合加载问题）
        roles: list = []
        if role_ids:
            roles = await self.role_repo.get_many(list(role_ids))
            if len(roles) != len(set(role_ids)):
                raise ValidationError("部分角色不存在，请刷新页面后重试")

        user = User(
            username=username,
            # 明文绝不落库，统一由 security 模块做 bcrypt 哈希
            password_hash=hash_password(password),
            display_name=display_name,
            status=UserStatus.ACTIVE,
            # ★ 关键：构造时就初始化 roles 集合（空列表也必须有）
            roles=roles,
        )
        await self.user_repo.add(user)
        await self.session.commit()

        # 提交后把 roles 重新拉一次：
        #   commit 会结束事务，之前 set 进去的关系在"已提交"状态下重新加载更稳妥，
        #   也让返回对象与数据库的最终状态一致（例如将来在角色上加了过滤条件）。
        await self.session.refresh(user, attribute_names=["roles"])
        return user

    async def update_user(
        self,
        user_id: UUID,
        *,
        display_name: str | None = None,
        status: UserStatus | None = None,
        password: str | None = None,
    ) -> User:
        """更新用户资料：昵称 / 状态 / 密码，三个字段都可选（PATCH 语义）。

        :raises NotFoundError: 用户不存在（404）
        :raises ValidationError: 昵称为空 / 密码短于 4 位（400）

        【为什么这里不校验密码的 72 字节上限】
        bcrypt 5.x 对超过 72 字节的明文会抛 ValueError。
        本方法把"长度校验"交给上层（第 6 步的路由层用 Pydantic 的 max_length 拦），
        因为那是"请求参数合法性"的范畴，在参数校验层返回 422 比在这里抛 500 更合适。
        本层只做业务级下限（至少 4 位）这一条。

        【为什么不提供修改 username 的能力】
        用户名是登录凭据与审计线索（文档的 created_by、会话的归属都间接指向它）。
        改名的需求在真实系统里通常用"新建 + 停用旧的"来满足，避免历史记录歧义。
        """
        user = await self.get_user(user_id)

        if display_name is not None:
            display_name = display_name.strip()
            if not display_name:
                raise ValidationError("昵称不能为空")
            user.display_name = display_name

        if status is not None:
            user.status = status

        if password is not None:
            if len(password) < 4:
                raise ValidationError("密码长度至少 4 位")
            user.password_hash = hash_password(password)

        await self.session.commit()
        # 返回前确保 roles 是"已加载"状态，避免路由层序列化时触发隐式 IO
        await self.session.refresh(user, attribute_names=["roles"])
        return user

    async def set_user_roles(
        self, user_id: UUID, role_ids: Sequence[UUID]
    ) -> User:
        """整体替换用户的角色集合。

        :raises NotFoundError: 用户不存在（404）
        :raises ValidationError: 含不存在的角色 id（400）

        【语义是"替换"而不是"追加"】
        前端提交的是编辑完的完整角色清单，服务端让它与提交值一致即可。
        传空列表 = 收回该用户所有角色（合法操作，用于"暂时停权"）。
        """
        user = await self.get_user(user_id)

        roles = await self.role_repo.get_many(list(role_ids))
        if len(roles) != len(set(role_ids)):
            raise ValidationError("部分角色不存在，请刷新页面后重试")

        await self.user_repo.set_roles(user, roles)
        await self.session.commit()
        await self.session.refresh(user, attribute_names=["roles"])
        return user

    async def delete_user(self, user_id: UUID) -> None:
        """删除用户。

        :raises NotFoundError: 用户不存在（404）

        【危险操作的提醒（为什么不做"禁止删除管理员"的拦截）】
        本方法没有像 RoleService 那样保护内置管理员，因为：
        - 角色保护解决的是"删了 admin 角色就没人能管用户"这个不可逆问题；
        - 而用户本身可重建：`UserRepository.count_all()` 提供了"库内是否还有用户"的判据，
          启动时据此播种默认管理员（该播种逻辑属第 5～6 步的启动流程，本节还没接）。
        真实系统通常还会加"不能删自己"，那属于业务策略，留给第 6 步按需补充。

        【数据库侧的连带影响（第 2 步的设计取舍）】
        - user_roles：ON DELETE CASCADE，关联行一并消失；
        - documents.created_by / conversations.user_id：ON DELETE SET NULL，
          即**文档与会话都保留**，只是失去归属人，供管理员继续审计。
        """
        user = await self.get_user(user_id)
        await self.user_repo.delete(user)
        await self.session.commit()
