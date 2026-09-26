"""权限计算服务（第 11 期）。

【模块职责说明】
本模块是整个权限体系的【纯计算层】，回答两个问题：
1. `compute_user_permission_tags(user)`：这个用户的有效权限标签是什么？
2. `is_admin(user)`：这个用户是不是管理员？

它不碰数据库、不碰 HTTP，只对已经加载好的 User 实体做计算 ——
因此极易测试，也能被检索链路、依赖注入、评测入口等任何地方复用。

【为什么"有效权限标签"要单独抽成一个函数】
因为后面所有「该用户能看哪些文档」的判断，最终都要落到同一条 SQL 上：
    WHERE documents.permission_tags && :user_tags  OR  documents.permission_tags = '{}'
这条 SQL 的参数 `:user_tags` 就来自本函数。全项目只有这一个来源，
避免各处各算一遍导致口径不一致（例如某处算成了"角色并集"、另一处只取了第一个角色）。
"""

from app.db.models import User

# =============================================================================
# 常量定义
# =============================================================================

# 通配权限标签：含义是"无视权限过滤"，admin 角色默认持有。
# 【为什么要引入它】admin 视角等价于"不加任何权限条件"——
# 与其给 admin 逐个列出所有标签（还得随新标签同步维护），
# 不如用一个特殊值表示"全部"，检索时识别到它就【直接跳过权限 WHERE 拼接】。
WILDCARD_PERMISSION_TAG = "*"

# 内置管理员角色名。
# 【为什么用名字而不是 id 判断】：id 是运行时生成的 uuid，代码里写不死；
# 而 admin 是内置角色的业务标识，数据库上有 UNIQUE 约束保证唯一。
ADMIN_ROLE_NAME = "admin"


def compute_user_permission_tags(user: User) -> list[str]:
    """合并用户所有角色的 permission_tags 并去重。

    【合并规则】
    - 任一角色持有 `"*"` → **立即返回 `["*"]`**，不再往下合并。
      这是短路优化，也是语义表达：通配即"全部"，再叠加别的标签没有意义。
    - 多角色叠加时用**集合去重**，最后排序返回。

    【为什么排序而不是保持原顺序】
    权限标签的先后顺序对检索逻辑没有任何意义（SQL 用的是数组重叠运算，与顺序无关）。
    但排序能让结果**稳定**：同样的角色组合永远得到同样的列表，
    便于写单元测试断言，也便于打日志时肉眼比对（debug 友好）。

    【前置条件：user.roles 必须已加载】
    本函数会直接遍历 `user.roles`。模型上已声明 `lazy="selectin"`，
    因此从数据库取出的 User（无论 select 还是 session.get）都带好了 roles。
    ⚠️ 唯一例外是**刚 new 出来、尚未 flush 的新对象** —— 它的集合可能是未加载态，
    直接访问会抛 MissingGreenlet。所以调用本函数前，user 应当来自一次数据库查询。

    :param user: 已加载 roles 的 User 实体
    :return: 去重并排序后的有效权限标签列表；持通配时返回 `["*"]`
    """
    merged: set[str] = set()
    for role in user.roles:
        for tag in role.permission_tags:
            if tag == WILDCARD_PERMISSION_TAG:
                # 短路：只要有一个角色带通配，整个用户就是"全部可见"
                return [WILDCARD_PERMISSION_TAG]
            merged.add(tag)
    return sorted(merged)


def is_admin(user: User) -> bool:
    """判断用户是否拥有 admin 角色（按角色名识别）。

    【为什么按角色名而不是按通配标签】
    两者在当前数据下等价（只有 admin 角色带 `"*"`），但含义不同：
    - 按角色名 → 回答"这个人是不是管理员"，用于【管理类接口】的准入判断
      （谁能访问 /api/users、谁能建角色）；
    - 按通配标签 → 回答"这个人能不能看全部文档"，用于【数据检索】的过滤判断。

    本函数选角色名，是为了让初学者一眼看懂"管理员就是这么判的"。
    副作用是：如果哪天给某个业务角色也加了 `"*"`，他数据上全可见、
    但**不会**因此获得管理接口的权限 —— 这个行为是合理的（数据可见 ≠ 管理权）。

    :param user: 已加载 roles 的 User 实体
    :return: 是否持有 admin 角色
    """
    return any(role.name == ADMIN_ROLE_NAME for role in user.roles)
