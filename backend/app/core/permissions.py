"""权限体系共享常量（第 11 期）。

【为什么这些常量要单独放在 core 层，而不是留在 permission_service.py】
它们**同时被两个不同层次使用**：

```
db 层    document_repo / chunk_repo  ── 拼检索 SQL 时需要判断"是否持通配标签"
services permission_service          ── 计算有效标签时需要识别通配并短路
db 层    seed                        ── 初始化 admin 角色时写入通配标签
```

若把常量定义在 `services/permission_service.py`，那么 `db` 层的仓储就得
`import app.services.permission_service` —— **依赖方向反了**（db 是被 services 依赖的下层）。

因此把它提到 `core`（最底层、谁都依赖它）：
每一层都可以 import，且不产生任何反向依赖。
"""

# 通配权限标签：含义是"无视权限过滤"，admin 角色默认持有。
#
# 【为什么用一个特殊值而不是"给 admin 列出所有标签"】
# 列举法有两个致命问题：
#   ① 每加一个新标签都要同步维护 admin 角色的标签列表，漏一次就出现"管理员看不到新文档"；
#   ② 标签多起来之后，`permission_tags && user_tags` 的比较开销也随之增长。
# 用一个特殊值表示"全部"，检索时识别到它就**直接跳过权限 WHERE 拼接** ——
# 既免维护，也跑得最快（少一个条件就少一次数组运算）。
WILDCARD_PERMISSION_TAG = "*"

# 内置管理员角色名。
#
# 【为什么用名字而不是 id 判断】id 是运行时生成的 uuid，代码里写不死；
# 而 admin 是内置角色的业务标识，数据库上有 UNIQUE 约束保证唯一。
#
# 【⚠️ 它是权限判断的锚点】`is_admin` 比对的就是这个字符串，
# `RoleService.PROTECTED_ROLE_NAMES` 也用它。这也解释了为什么角色**不允许改名** ——
# 一旦改名，就会出现"代码认 admin、而库里已改名"的错位，管理员会突然失去权限。
ADMIN_ROLE_NAME = "admin"
