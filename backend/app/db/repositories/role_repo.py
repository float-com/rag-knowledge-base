"""角色数据仓储层（第 11 期）。

【模块架构定位与设计哲学】
1. 比 UserRepository 更简单：角色表没有外键依赖、没有多对多预加载问题，
   查询维度也少（只有 id 与 name 两个身份标识），因此本模块全文只有 6 个方法。

2. 【为什么角色列表不做分页】
   角色总量天然极小 —— 教学项目内置 admin / user 两个，加上管理员自建的
   业务角色（hr / sales 之类）通常也就个位数到十几个。
   强行分页只会让前端多写一套翻页逻辑，收益为零。
   （对比：用户与文档必须分页，因为它们的量级会随使用持续增长。）

3. 同样遵守 Unit of Work 契约：本模块**只 flush，不 commit / rollback**，
   事务边界统一由上层 Service 掌握。
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Role


class RoleRepository:
    """RBAC 角色表（roles）仓储。

    【核心职责】
    角色维度的持久化生命周期管理：按主键 / 角色名点查、全量列表、
    批量按主键取回（供"整体替换用户角色"时把前端传来的 id 列表转成实体）。
    """

    def __init__(self, session: AsyncSession) -> None:
        """注入异步数据库会话（AsyncSession），生命周期由外部维护。"""
        self.session = session

    async def get_by_id(self, role_id: UUID) -> Role | None:
        """按主键 UUID 点查角色。

        `session.get()` 会优先命中 Session 的一级缓存（Identity Map），
        已加载过的角色不会重复发 SQL。

        :param role_id: 角色主键
        :return: Role 实体；不存在时返回 None
        """
        return await self.session.get(Role, role_id)

    async def get_by_name(self, name: str) -> Role | None:
        """按角色名精确查询。

        【业务用途】种子角色初始化：判断 admin / user 这两个内置角色是否已存在。
        角色名在数据库上建了 UNIQUE 约束，因此这里用 scalar_one_or_none 是安全的
        （不可能查出两行）。

        :param name: 角色名（如 admin / user / hr）
        :return: Role 实体；不存在时返回 None
        """
        stmt = select(Role).where(Role.name == name)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_all(self) -> list[Role]:
        """全量拉取角色列表（不分页，理由见模块 docstring）。

        【排序】按 (created_at, id) 升序：
        - 用 created_at 让内置角色（最先创建）排在最前面，符合管理员的直觉；
        - 用 id 兜底保证顺序稳定 —— created_at 精度有限，批量创建时可能相同，
          没有唯一列兜底时列表顺序会在两次查询之间漂移，前端会看到"闪动"。
        """
        stmt = select(Role).order_by(Role.created_at.asc(), Role.id.asc())
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_many(self, role_ids: Sequence[UUID]) -> list[Role]:
        """按一组主键批量取回角色实体。

        【业务用途】用户角色编辑接口收到的是前端传来的 role_id 列表，
        需要先把它翻译成 Role 实体才能赋给 user.roles。
        一次 IN 查询完成，避免在循环里逐个 get_by_id 造成 N 次查询。

        【防御：空列表短路】
        `WHERE id IN ()` 在 SQL 里是非法语法（PostgreSQL 会直接报错），
        SQLAlchemy 虽然会把空 IN 优化成恒假表达式、不至于崩，
        但显式返回空列表语义更清晰，也省掉一次无意义的数据库往返。

        :param role_ids: 角色主键序列（可以是 tuple / list）
        :return: 匹配到的 Role 列表；**注意可能少于入参个数**
                 （传了不存在的 id 时不会报错，只会少返回）——
                 调用方若要求"全部存在"，需自行比对数量并决定是否报 400。
        """
        if not role_ids:
            return []
        stmt = select(Role).where(Role.id.in_(list(role_ids)))
        return list((await self.session.execute(stmt)).scalars().all())

    async def add(self, role: Role) -> Role:
        """新增角色（写入 roles 表）。

        只 flush 不 commit，由上层 Service 决定事务何时落盘。
        """
        self.session.add(role)
        await self.session.flush()
        return role

    async def delete(self, role: Role) -> None:
        """物理删除角色。

        【级联行为】users 与 roles 之间的 user_roles 外键是 ON DELETE CASCADE，
        因此删除角色会**自动摘掉所有持有该角色的用户的关联行**，
        这些用户本身不会被删除，只是少了一个角色来源。

        ⚠️ 业务上应当禁止删除内置的 admin / user 两个角色（否则可能把自己锁在系统外），
        该限制属于业务规则，由上层 Service 负责拦截，仓储层不做判断。
        """
        await self.session.delete(role)
        await self.session.flush()
