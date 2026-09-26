"""权限标签标准化工具（第 11 期）。

【为什么要有这个模块】
`permission_tags` 写入路径上都必须做同一套清洗：

```
角色         RoleService.create_role / update_role        admin 配置"角色能看什么"
文档（普通）  DocumentService.upload / update_permission_tags
文档（直传）  DocumentUploadService.finalize_upload       ★ 第 11 期补漏：此前漏了清洗与搬运
```

本函数原先在 `role_service` 与 `document_service` 里**各留一份完全相同的副本**
（当时认为"8 行的纯函数不值得为它新建模块"）。
但 `document_upload_service` 的 finalize 也要用它时，这个判断就站不住了：
要么 import 别人模块里的私有函数（破坏封装），要么再抄第三份。
于是上收到 `core` —— 与 `core/permissions.py` 的常量同理：
**共享工具放最底层，谁都能用且不产生反向依赖**（services 依赖 core，方向正确）。

> 📌 顺带更正一处记录：我曾在第 8 章归档里写"三处各留一份副本"，
> 实际查证只有 `role_service` 与 `document_service` **两处**
> （`user_service` 从未有过这个函数）。归档已同步更正。

【为什么必须做这一步清洗】
标签最终会进 PostgreSQL 的数组重叠运算（`&&`）。
若存进去的是 `" hr"`（前导空格）或 `"hr "`（尾随空格），
它就是一个"看起来像 hr 但实际不相等"的字符串 —— 检索时 `&&` 永远不命中，
表现为"权限明明配了却不生效"，**极难排查**（既不报错，也没有任何日志异常）。
"""

from collections.abc import Sequence


def normalize_tags(tags: Sequence[str]) -> list[str]:
    """标准化标签：去空白、丢弃空串、去重，并保持输入顺序。

    【为什么用 seen 集合 + 结果列表，而不是 `sorted(set(...))`】
    要同时满足"去重"与"保持用户输入顺序"：
    - 纯 `set` 会丢顺序；
    - `sorted` 会改变顺序（用户按 `[sales, hr]` 输入，回显却变成 `[hr, sales]`，
      会让人以为"没保存成功"）。
    因此用一个 seen 集合判重、一个 list 保序。

    【为什么是"保序去重"而不是排序】
    与 `compute_user_permission_tags` 的排序策略**故意不同**：
      - 本函数处理的是"人手工输入并会在表单里回显"的标签 → 顺序要跟人走；
      - 那个函数处理的是"多个角色标签求并集"的中间结果 → 顺序无意义，
        排序反而让结果稳定、便于断言与打日志。
    两者都不影响检索正确性（SQL 用数组运算，与顺序无关）。

    :param tags: 原始标签序列（可能含空串、前后空白、重复项）
    :return: 清洗后的标签列表
    """
    seen: set[str] = set()
    result: list[str] = []
    for tag in tags:
        t = tag.strip()
        # 跳过空串（前端"回车新增标签"的操作很容易留下空项）
        if not t or t in seen:
            continue
        seen.add(t)
        result.append(t)
    return result
