"""种子数据初始化（第 11 期）。

【模块职责说明】
在应用启动时，保证数据库里存在"能让人登进来"的最小数据集：

1. 两个内置角色（admin / user）—— RBAC 的地基；
2. 一个默认管理员账号 —— 打破先有鸡还是先有蛋的死锁。

【为什么必须有这个模块】
没有它，新库第一次启动后是这样的：库里没有任何用户 → 没有任何人能登录 →
进不去系统 → 也就无法创建第一个用户。这个死锁在教学与部署场景下都会直接卡住人。

【为什么它是幂等的（可以反复重启而不出问题）】
- 用户侧：`count_all() > 0` 就【整个跳过】。只要库里有人，本模块什么都不做 ——
  因为"已经有人在用了"意味着初始化早就完成了，绝不能去动任何已有数据。
- 角色侧：逐个 `get_by_name` 判断，存在就复用、不存在才建。
- 兜底：外层 try/except 把任何异常都吞掉并记日志，**不阻断应用启动** ——
  初始化失败只影响"能不能登录"，不应该让整个服务起不来（那样连 /docs 都看不了，
  反而更难排查）。
"""

from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import hash_password
from app.db.models import Role, User, UserStatus
from app.db.repositories.role_repo import RoleRepository
from app.db.repositories.user_repo import UserRepository
from app.db.session import AsyncSessionLocal
from app.core.permissions import WILDCARD_PERMISSION_TAG

logger = get_logger(__name__)

# =============================================================================
# 内置角色定义
# =============================================================================
# 【为什么用 list[dict] 而不是直接写两个 Role 对象】
# 常量只描述"需要哪些角色"，不携带 ORM 状态。到真正建表时才用这些字段去 new Role，
# 避免模块级就创建 ORM 实例（那会让 Import 阶段就依赖 Base 的元数据配置）。
#
# 【两个角色的权限标签为什么这么定】
# - admin：持通配标签 `*`。第 7 步的检索 SQL 识别到它会**直接跳过权限过滤** ——
#   admin 视角等价于"不加任何权限条件"，既跑得最快，也符合"管理员看全量"的预期。
# - user：持 `public` 标签。注意它**不是空数组**：
#   空数组的文档对所有人可见（公开语义），而 `user` 角色的标签用于匹配
#   那些明确标了 `public` 的文档。两者语义不同，不要混。
BUILTIN_ROLES: list[dict] = [
    {
        "name": "admin",
        "description": "系统管理员",
        # 通配标签：检索 SQL 不加权限过滤
        "permission_tags": [WILDCARD_PERMISSION_TAG],
    },
    {
        "name": "user",
        "description": "普通用户",
        # 默认仅能访问带 "public" 标签的文档；
        # 空 permission_tags 的存量文档也会被视为公开（第 7 步的 OR 分支保证）
        "permission_tags": ["public"],
    },
]


async def seed_default_admin() -> None:
    """库内无用户时，创建 admin 角色 + user 角色 + 默认管理员账号。

    【幂等性靠 count_all() > 0 做兜底】：
    只要库里存在任意一个用户，整个方法直接 return ——
    既不重复建管理员，也不去动内置角色（避免把管理员改过的标签重置回去）。

    【为什么要自己开 session 而不是复用请求级会话】
    本方法在**应用启动期（lifespan）**执行，那时还不存在任何 HTTP 请求，
    因此没有请求级 session 可用，必须自己开一个短会话并显式提交。

    【异常处理策略】
    内部不吞异常 —— 由调用方（lifespan）统一兜住并记日志。
    这样"初始化失败"这件事只在启动流程里处理一次，本函数保持职责单一。
    """
    async with AsyncSessionLocal() as session:
        user_repo = UserRepository(session)

        # ---------------------------------------------------------------------
        # 幂等守卫：库里只要有任何用户，就认为初始化早就完成了
        # ---------------------------------------------------------------------
        if await user_repo.count_all() > 0:
            return

        logger.info("seeding default admin user and builtin roles")

        # ---------------------------------------------------------------------
        # 第一步：确保两个内置角色存在
        # ---------------------------------------------------------------------
        # 【为什么角色也要"存在即复用"而不是直接建】
        # 因为可能遇到"库里没人、但角色已经被手工建过"的情况（例如 DBA 手工塞过数据）。
        # 直接 add 会撞上角色名的 UNIQUE 约束。
        role_repo = RoleRepository(session)
        roles_by_name: dict[str, Role] = {}
        for spec in BUILTIN_ROLES:
            existing = await role_repo.get_by_name(spec["name"])
            if existing is not None:
                roles_by_name[spec["name"]] = existing
                continue
            role = Role(
                name=spec["name"],
                description=spec["description"],
                permission_tags=list(spec["permission_tags"]),
            )
            await role_repo.add(role)
            roles_by_name[spec["name"]] = role

        # ---------------------------------------------------------------------
        # 第二步：创建默认管理员账号
        # ---------------------------------------------------------------------
        # ★ 关键：roles 必须【在构造时】就传进去。
        #   若 new 出来之后再赋值（admin_user.roles = [...]），SQLAlchemy 会先读旧集合
        #   算差异，而这个读取动作在异步驱动下会抛 MissingGreenlet。
        #   （第 4 步已实测确认，user_service.create_user 也是同样的写法。）
        admin_user = User(
            username=settings.default_admin_username,
            # 明文密码只在这里出现一次，落库前一律 bcrypt 哈希
            password_hash=hash_password(settings.default_admin_password),
            display_name=settings.default_admin_display_name,
            status=UserStatus.ACTIVE,
            roles=[roles_by_name["admin"]],
        )
        await user_repo.add(admin_user)

        # 事务边界由本函数掌握：仓储只 flush，这里统一提交
        await session.commit()

        logger.info(
            "seeded default admin: username=%s (请尽快修改默认密码)",
            settings.default_admin_username,
        )
