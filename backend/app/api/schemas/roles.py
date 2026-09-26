"""角色管理 API 契约（第 11 期，第 7 章）。

【模块职责说明】
描述"角色管理"这一组接口的请求体：`RoleCreate`（POST）与 `RoleUpdate`（PATCH）。
响应侧复用 `schemas/auth.py` 的 `RoleRead` —— 角色读模型只有一份。

【本模块最重要的设计：`RoleUpdate` 里【没有】name 字段】
角色名是权限判断的锚点（`permission_service.is_admin` 比对 `"admin"`），
所以第 4 步的 `RoleService.update_role` 签名里就没有 `name` 参数。
这里的 PATCH schema 同样不暴露它 —— 形成**双层防呆**：

```
契约层（本文件）不提供 name 字段  →  前端根本传不进来
业务层（RoleService）没有 name 参数  →  即使绕过 HTTP 直接调 Service 也改不了
```

只用其中一层也能挡住，但两层都做才能覆盖"前端调用"与"脚本直连 Service"两条路径。
"""

from pydantic import BaseModel, Field


class RoleCreate(BaseModel):
    """创建角色的请求体。

    - `name` 必填且上限 64，对应数据库 `String(64)` 列宽，与 `Role.name` 的 UNIQUE 约束配合；
    - `description` 可缺省（默认空串），对应 `String(256)`；
    - `permission_tags` 缺省为空列表 —— 注意空标签的角色【不是"看不到任何东西"】：
      第 7 步的检索 SQL 用的是"标签重叠 OR 文档标签为空（公开）"，
      所以空标签角色的用户仍能看到所有公开文档。
    """

    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=256)
    permission_tags: list[str] = Field(default_factory=list)


class RoleUpdate(BaseModel):
    """更新角色的请求体；字段均可选，**传 None = 本次不改这一项**。

    ⚠️ `name` 字段**刻意不暴露**：策略代码以角色名为锚（如 `"admin"`），不允许改名。
    详见模块 docstring 的双层防呆说明。

    另一个细节：`permission_tags` 传 `None` 表示"不改标签"，
    而传**空列表** `[]` 表示"把所有标签清空"——这是两个不同的语义，不要混。
    （`description` 同理：`None` 不改，`""` 清空。）
    """

    description: str | None = Field(default=None, max_length=256)
    permission_tags: list[str] | None = None
