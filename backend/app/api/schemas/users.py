"""用户管理 API 契约（第 11 期，第 7 章）。

【模块职责说明】
用 Pydantic 模型描述"用户管理"这一组接口的请求体与响应体：

- 请求侧：`UserCreate`（POST）/ `UserUpdate`（PATCH）/ `AssignRolesRequest`（PUT 角色）
- 响应侧：`UserPage`（列表分页），单个用户复用 `schemas/auth.py` 的 `UserRead`

【为什么复用 auth.py 的 UserRead / UserStatusValue 而不是在这里重定义】
用户这个实体的**读模型只有一份**。登录（`/api/auth/*`）与管理（`/api/users`）
返回的是同一个 `UserRead`，若各写一份就会出现"两处用户字段慢慢不一致"的经典问题。
同理，`status` 的合法值域也只有一份（`UserStatusValue`），从 auth.py 导入。

【校验规则的三个注意点】
1. `username` 上限 64 / 密码上限 128 / 昵称上限 128 —— 都对应对齐数据库列宽
   （`String(64)` / `String(255)` / `String(128)`）。
   在 schema 层拦住超长输入，才能返回**框架级 422**，而不是到数据库才报错。
2. `min_length` 与 Service 层校验**有一层重叠**（密码至少 4 位、用户名非空）。
   这是刻意的双保险：schema 管"请求格式"，Service 管"业务规则"——
   Service 也会被脚本、种子初始化等非 HTTP 路径调用，不能只依赖 schema。
3. ⚠️ **一个已知的边界（见下方 UserCreate 的说明）**：
   bcrypt 的 72 字节上限是**字节**，而 `max_length` 限制的是**字符**。
   两者在纯 ASCII 下巧合相等，但中文/emoji 会不一致。
"""

from uuid import UUID

from pydantic import BaseModel, Field

from app.api.schemas.auth import UserRead, UserStatusValue


class UserCreate(BaseModel):
    """创建用户的请求体。

    【为什么 role_ids 单独给、而不是塞进 User 一起建】
    角色分配是独立的一步（`PUT /users/{id}/roles` 也能改），
    但创建时"顺手带上角色"能省掉前端一次往返 —— 新建管理员这类场景几乎总要立刻挂角色。
    默认空列表 = 建一个没有任何角色的用户（合法，只是他登录后什么也看不到）。
    """

    username: str = Field(min_length=2, max_length=64)
    # ⚠️ 【已知边界，不是 bug 但是个坑】max_length 限制的是【字符数】，
    #   而 bcrypt 拒绝的是超过 72 【字节】的明文。
    #   纯 ASCII 下 72 字符 = 72 字节，看起来"上限 128 很宽松"；
    #   但中文（UTF-8 每字 3 字节）只要 25 个字就超过 72 字节，
    #   emoji（4 字节）18 个就超 —— 此时 hash_password 会抛 ValueError → 500。
    #   真正做到"任何输入都返回 422 而不是 500"，需要按字节校验（自定义 validator）。
    #   本期先保持与教程一致（max_length=128），已记入归档遗留项。
    password: str = Field(min_length=4, max_length=128)
    display_name: str = Field(min_length=1, max_length=128)
    # 与 Pydantic v2 的可变默认值约定一致：用 default_factory 而不是 = []
    role_ids: list[UUID] = Field(default_factory=list)


class UserUpdate(BaseModel):
    """PATCH 请求体；字段均可选，**传 None = 本次不改这一项**。

    【password 的三态语义（最容易误解的一处）】
    - 不传 / 传 None → **密码不动**（不是清空、也不是设成空串）
    - 传非空字符串   → 重置为新密码
    - 传空字符串 ""  → 被 `min_length=4` 挡下，返回 422
    即"改密码"只能靠显式给出一个新密码，无法用 None 表达"清空密码"——
    这是刻意的：密码永远不该被清空。
    """

    display_name: str | None = Field(default=None, max_length=128)
    status: UserStatusValue | None = None
    password: str | None = Field(default=None, min_length=4, max_length=128)


class AssignRolesRequest(BaseModel):
    """分配角色的请求体：整体替换该用户的角色集合。

    【为什么是 PUT 而不是 PATCH】
    语义是"把用户的角色设成这份清单"，是**幂等的整体替换**：
    同样的请求执行两次，结果相同。PUT 正好表达这个语义。
    而"追加一个角色 / 移除一个角色"那样的增量操作才该用 PATCH。

    传空列表 = 收回该用户所有角色（合法的"暂时停权"操作）。
    """

    role_ids: list[UUID] = Field(default_factory=list)


class UserPage(BaseModel):
    """用户分页响应体。

    【与 Run/Item 分页的写法差异】
    评测模块的分页模型用 `Field(default_factory=list)`（字段可缺省）；
    这里照教程写成**必填**（`items: list[UserRead]` / `total: int`）——
    因为这是服务端自己构造的响应，永远会给全这四项，没有"可能缺省"的场景。
    两种写法都能跑，区别只在对缺省值的宽容度。
    """

    items: list[UserRead]
    total: int
    # ge/le 在此处的意义与请求侧略有不同：响应里的 page/page_size 是"回显请求参数"，
    # 加约束等于声明"服务端保证不会回显越界的页码"。
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
