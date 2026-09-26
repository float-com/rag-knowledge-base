"""用户数据仓储层（第 11 期）。

【模块架构定位与设计哲学】
1. 仓储模式（Repository Pattern）的职责边界：
   - 屏蔽底层持久化细节：把 SQLAlchemy 的 select 拼装、分页偏移量（Limit/Offset）计算、
     排序稳定性兜底等 SQL 细节全部封锁在仓储内部；
   - 对外只提供面向领域实体的接口，让 Service 层专注"登录校验、权限汇总"等业务逻辑，
     实现持久化与业务逻辑解耦。

2. 工作单元与事务边界的严格契约（Unit of Work）：
   - 【核心法则】：仓储层内部**只执行 `flush()`，严禁调用 `commit()` 或 `rollback()`**；
   - `flush()` 会把 Session 内存中的变化转成 SQL 发给数据库，使 `default=uuid4`、
     `server_default=now()` 这类数据库侧默认值被生成并回填到 Python 实体上，
     同时触发非空 / 唯一约束检查，但**不结束当前事务**；
   - `commit()` 由上层 Service 统一调用，以保证多表联动时的原子性 ——
     例如"创建用户 + 给他挂角色"必须同生共死。

3. 【关于 roles 的加载：模型层已解决，本层不需要任何兜底】
   `User.roles` 在模型里声明为 `lazy="selectin"`，因此不论实体通过哪条路径取出
   （`select(User)` / `session.get()` / `expire_all()` 后重取 / 新建对象），
   集合都已经是加载好的。已逐条实测确认。

   【为什么这件事值得反复强调】异步 SQLAlchemy 下最常见的运行时崩溃就是
   `MissingGreenlet: greenlet_spawn has not been called ...` ——
   它来自"在属性访问时临时发 SQL"，而属性访问不在可 await 的上下文里。
   本项目的对策是把需要的关系在模型上统一声明成 `selectin`，
   而不是要求每个调用点都记得手写 `.options(selectinload(...))`。
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import Role, User


class UserRepository:
    """用户主表（users）仓储。

    【核心职责】
    管理用户维度的持久化生命周期：按身份标识点查（主键 / 登录名）、统计用户总数、
    控制台分页列表，以及"整体替换用户角色集合"这一多对多关系的写入。
    """

    def __init__(self, session: AsyncSession) -> None:
        """注入异步数据库会话（AsyncSession）。

        Session 的生命周期由外部维护（FastAPI Depends 注入的请求级会话，
        或后台任务自建的短会话），仓储自身不创建也不关闭它。
        """
        self.session = session

    async def get_by_id(self, user_id: UUID) -> User | None:
        """按主键 UUID 点查用户。

        【为什么这里【没有】预加载 roles，而 get_by_username 有】
        `session.get()` 是主键点查，它支持一次只查一张表；
        而 `.options(selectinload(...))` 是 select() 语句构造器上的能力，
        `session.get()` 不接受该参数。因此本方法只能先拿到"角色未加载"的 User。

        调用方若需要读 roles，必须自己再显式触发一次加载（见 get_by_username 的写法），
        绝不能直接 `user.roles` —— 那会抛 MissingGreenlet。

        :param user_id: 用户主键
        :return: User 实体；不存在时返回 None（是否转 404 由 Service 决策）
        """
        return await self.session.get(User, user_id)

    async def get_by_username(self, username: str) -> User | None:
        """按登录名精确查询用户，并【显式预加载角色】。

        【为什么写了 .options(selectinload(User.roles))，虽然模型已配 lazy="selectin"】
        模型上的 `lazy="selectin"` 已保证任何取数路径都会自动加载 roles，
        所以这一行在行为上是【冗余的】，不会多产生任何查询。
        保留它是把"登录紧接着就要读 roles 汇总权限标签"这个意图写在查询处：
        将来若有人调整模型上的 lazy 配置，这里仍然安全。

        登录拿到 User 之后的下一步就是汇总权限标签：
            tags = {t for r in user.roles for t in r.permission_tags}
        若 roles 未加载，这行会触发异步惰性加载并抛 MissingGreenlet。

        :param username: 登录用户名
        :return: User 实体（roles 已加载）；不存在时返回 None
        """
        stmt = (
            select(User)
            .where(User.username == username)
            .options(selectinload(User.roles))
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def count_all(self) -> int:
        """统计用户总数。

        【业务用途】启动时的种子管理员初始化：库内【无任何用户】时才创建默认 admin。
        这也是"没有 admin 就无法创建用户、没有用户就无法创建 admin"这个死锁的破局点。

        【为什么套一层 int(...)】
        `func.count()` 在 SQLAlchemy 里的返回类型是 `int | None` 的宽泛标注
        （聚合函数理论上可能返回 NULL）；实际 COUNT(*) 永远返回非空整数，
        这里显式转成 int 让类型检查器满意，也避免调用方处理 None。
        """
        return int((await self.session.execute(select(func.count(User.id)))).scalar_one())

    async def list_paginated(
        self, page: int, page_size: int
    ) -> tuple[list[User], int]:
        """分页查询用户列表，并返回总记录数。

        【防御性设计 - 参数强制钳制（Parameter Clamping）】
        - `page = max(page, 1)`：屏蔽 0 或负数页码，避免负 offset 让 SQL 报错；
        - `page_size = max(min(page_size, 100), 1)`：单页上限封顶 100，
          即使调用方传 10000 也拖不垮连接池。
        即使上层 Pydantic Schema 已做校验，仓储层仍要自主设防 ——
        因为本方法也可能被数据修复脚本、离线任务直接调用（不过 HTTP）。

        【排序确定性】
        `order_by(created_at, id)` 复合升序：created_at 在高并发批量创建时可能重复，
        必须用唯一主键兜底，否则分页会出现"漏看 / 重复看到"的幽灵数据。

        :return: (当前页用户列表, users 表总记录数)
        """
        page = max(page, 1)
        page_size = max(min(page_size, 100), 1)
        offset = (page - 1) * page_size

        items_stmt = (
            select(User)
            .order_by(User.created_at.asc(), User.id.asc())
            .offset(offset)
            .limit(page_size)
            # 列表页要展示每个用户的角色标签，因此同样需要预加载
            .options(selectinload(User.roles))
        )
        count_stmt = select(func.count(User.id))

        items = list((await self.session.execute(items_stmt)).scalars().all())
        total = int((await self.session.execute(count_stmt)).scalar_one())
        return items, total

    async def add(self, user: User) -> User:
        """新增用户（写入 users 表）。

        【为什么返回实体而不返回 None】
        调用方通常在 add 之后立刻需要 `user.id`（例如把它挂到角色上、
        或作为 created_by 写进其它表）。虽然 flush() 之后主键已回填到对象上、
        通过入参也能拿到，但显式返回能让"add 完就能直接用"这条意图更清楚。

        【事务安全提醒】只 flush 不 commit，该行仍在当前事务中，
        须由上层 Service 显式 commit 才对其它连接可见。
        """
        self.session.add(user)
        await self.session.flush()
        return user

    async def delete(self, user: User) -> None:
        """物理删除用户。

        【级联行为由数据库负责】：
        - users 与 user_roles：外键是 ON DELETE CASCADE，关联行会一并消失；
        - users 与 documents.created_by / conversations.user_id：
          外键是 ON DELETE SET NULL，即**用户删了、文档与会话仍在**，
          只是失去归属人，供管理员继续审计（第 2 步的设计取舍）。
        """
        await self.session.delete(user)
        await self.session.flush()

    async def set_roles(self, user: User, roles: Sequence[Role]) -> None:
        """整体替换用户的角色集合（多对多关系写入）。

        【为什么是"整体替换"而不是逐个 add/remove】
        对齐前端"编辑用户角色"的交互语义：用户提交的是一份完整的目标角色清单，
        服务端只需要让它与提交值一致。整体赋值由 SQLAlchemy 自动 diff，
        算出需要 INSERT / DELETE 哪些 user_roles 行，比手工比对更不容易出错。

        【为什么可以放心直接赋值（不需要任何兜底）】
        `user.roles = [...]` 这行赋值，SQLAlchemy 在写入前会先读取旧集合来算差异。
        若集合处于"未加载"态，读取就会触发异步惰性加载并抛 MissingGreenlet。

        而本项目把 `User.roles` 声明成了 `lazy="selectin"`，**无论实体怎么被取出来**，
        集合都已经是加载好的，因此这里可以放心直接赋值。已逐条实测：

        | 取数方式 | 赋值前 roles 状态 |
        | --- | --- |
        | `select(User)`（get_by_username / list_paginated） | 已加载 |
        | `session.get()`（get_by_id） | 已加载 |
        | `expire_all()` 之后再 `session.get()` | 已加载 |
        | 新建对象（构造时传 `roles=[]`） | 已加载 |

        （正是这份实测让本方法从"需要写兜底 reload"简化回了现在的两行。）

        【为什么显式 list(...)】
        `roles` 参数是只读的 Sequence，而 SQLAlchemy 的关系集合要求是可变列表；
        转换一次可避免"传进来的是 tuple，赋值后关系集合行为异常"的隐患。
        """
        user.roles = list(roles)
        await self.session.flush()
