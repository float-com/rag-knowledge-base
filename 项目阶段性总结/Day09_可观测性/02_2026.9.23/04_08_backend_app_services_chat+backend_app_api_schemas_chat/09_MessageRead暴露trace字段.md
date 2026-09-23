# 09_MessageRead暴露trace字段

> 期：**Day09 · 可观测性**
> 章：第 9 章 MessageRead 暴露 trace_id / trace_url
> 记录日期：2026.09.23

---

## 一、本章做什么

**只改后端 2 个文件**（前端仅核对，不改）：

| # | 文件 | 改什么 |
| --- | --- | --- |
| 1 | `backend/app/core/observability.py` | 无改动（本档说明：它只在第 9 章被**新增一个调用方**） |
| 2 | `backend/app/api/schemas/chat.py` | import + 新增解析函数 + `MessageRead` 加 2 字段 + `from_orm` 组装 |
| 3 | `backend/app/services/chat_service.py` | 🔧 **回退**一处：不再往 metadata 写 `trace_url` |

**做完的效果**：**历史回看**（刷新页面 / 翻旧会话）也能显示追踪标识与跳转链接 ——
这是第 8 章只补了实时通路之后剩下的另一半。

---

## 二、核心设计：`trace_url` **不入库**、每次响应现拼

教程原话：

> `trace_url` **不进数据库**，每次响应时按当前 `settings.langsmith_run_url_prefix` 现拼。
> 这样如果以后切换 LangSmith workspace、改 URL 格式，老消息也能跟着新规则跳转，**不用回填历史**。

### 为什么这个决定比"落库"更好

| 方案 | 换 workspace / 改 URL 格式后 | 数据表 |
| --- | --- | --- |
| ❌ 把 URL 落库 | 老消息里是**旧链接**（可能已失效），要写脚本回填 | 多一列冗余数据 |
| ✅ **只落库 ID、URL 现拼** | **所有历史消息自动跟着新规则** | 无冗余 |

**一句话**：**ID 是事实，URL 是表现。事实入库，表现现算。**

> 📌 **这与第 8 章形成了修正关系**：
> 第 8 章我按"前端不会自己拼 URL"推断出"那就得落库"，把 `trace_url` 也写进了 metadata。
> 第 9 章给出了更好的答案 —— **拼接的责任本来就不该在数据库，而在 API 层**。
> 所以本章把第 8 章那处落库**移除**（见 §五）。
> 第 8 章归档已同步加上"本节结论已被第 9 章推翻"的批注，**两份归档不会互相矛盾**。

---

## 三、改动 1：解析函数 `_parse_trace_id`（L359-380）

```python
# =============================================================================
# 3.3 可观测性追踪标识（第 9 期）
# =============================================================================
def _parse_trace_id(metadata: dict | None) -> str | None:
    """从 messages.extra_metadata 提取 trace_id：缺失 / 非法静默返回 None。"""
    if not metadata:
        return None
    raw = metadata.get("trace_id")
    # `not isinstance(raw, str)` 挡住所有非字符串；`not raw.strip()` 挡住空串与纯空白
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw
```

### 与既有两个解析函数的关系

| 函数 | 防御层数 | 为什么不同 |
| --- | --- | --- |
| `_parse_query_route` | 三层 | 值是**嵌套结构**，要交给 Pydantic 校验 |
| `_parse_verify_result` | 三层 | 同上 |
| **`_parse_trace_id`** | **两层** | 值是**纯字符串**，不需要 Pydantic 二次校验 |

但**有一条共同纪律**：

> **绝不因为一段可选元数据让整个历史接口报错** —— 任何异常都静默返回 `None`。

### 为什么必须**显式判类型**（本章最容易被忽略的一行）

元数据列是 **JSONB**，里面可以存任意 JSON 类型。假如某条数据是手工写的或历史遗留的：

```
{"trace_id": 12345}       ← 数字
{"trace_id": true}        ← 布尔
{"trace_id": {"a": 1}}    ← 对象
```

**若直接透出**，Pydantic 会因类型不符抛 `ValidationError` → **整个历史接口 500**。

**一行 `isinstance` 挡住所有非字符串，这就是"两层防御"的全部意义。**

---

## 四、改动 2：`MessageRead` 加两个字段（L411-412 / L441 / L467）

### 字段声明

```python
    verify_result: VerifyResultRead | None = None
    # 【第 9 期】LangSmith 追踪标识与跳转链接。
    #   实时对话时由 SSE 的 message_start 事件下发；历史回看时从这里取。
    #   【为什么 trace_url 也要声明成字段】前端 TraceIdPanel 只读这一个 URL 字段、
    #   不会自己拿 trace_id 去拼（拼接需要私有的 URL 前缀，前端无从得知）。
    trace_id: str | None = None
    trace_url: str | None = None
```

> **注意这里与 §二并不矛盾**：
> - **不入库** ≠ **不出现在响应里** —— `trace_url` 是**响应字段**，只是它的值**当场算**，不来自数据库。

### `from_orm` 里组装

```python
        is_assistant = message.role == "assistant"
        # 【第 9 期】追踪标识只从 assistant 消息的元数据里取（user 消息从不写该键）；
        #   trace_url 则**不入库**、按当前配置现拼 —— 这样换 LangSmith 工作区或改 URL 格式后，
        #   历史消息跟着新规则跳转，不必回填历史数据。
        trace_id = _parse_trace_id(message.extra_metadata) if is_assistant else None
        return cls(
            ...
            # 【第 9 期】追踪标识来自落库的元数据；跳转链接按当前配置现拼
            trace_id=trace_id,
            trace_url=build_trace_url(trace_id),
        )
```

**三点设计**：

| 点 | 说明 |
| --- | --- |
| **取号抽成局部变量** | `trace_id` 要在两处用（出参 + 拼 URL），抽出来避免**解析两次**、也避免两处判断不一致 |
| **`is_assistant` 守卫** | 与 `query_route` / `agent_steps` / `verify_result` **完全一致**：user 消息从不写这些元数据 |
| **`build_trace_url(trace_id)`** | 复用第 4 章那个函数 —— **同一份拼接规则，实时与历史两条路不会各自演化** |

---

## 五、改动 3：移除第 8 章写进 metadata 的 `trace_url`

第 8 章曾这样落库（当时的理由是"前端不会自己拼 URL"）：

```python
# ❌ 第 8 章（现已被本章移除）
"trace_id": state.get("trace_id"),
"trace_url": build_trace_url(state.get("trace_id")),
```

本章改为：

```python
# ✅ 第 9 章
#   【只存 trace_id，不存 trace_url】——
#   第 9 章的响应模型是"拿落库的 trace_id、按【当前】配置现拼跳转链接"。
#   若这里也存一份 URL，它就成了永不读取的死数据，而且换 LangSmith 工作区
#   或改 URL 格式后，历史里那份旧链接会与新规则不一致。
#   【为什么不在这里把 URL 一并算好】拼接规则属于表现层关切，
#   放在 API 模型层能在每次响应时反映最新配置。
"trace_id": state.get("trace_id"),
```

### 为什么必须删（不是洁癖，是防坑）

| 留着会怎样 |
| --- |
| 数据库里多一份**永不读取**的数据（API 层根本不会去读那个键） |
| 一旦有人看到它、以为"URL 是从这里来的"，就会**改错地方** |
| 两份 URL 会**各自演化**：落库那份用旧配置，响应那份用新配置 → 排查时极难发现 |

**实测确认**：删掉后 metadata 的键从 6 个变回 5 个，而历史接口照样能拿到完整 URL（见 §七）。

> ⭐ **这是"同一件事在两个地方各算一遍"的典型隐患** ——
> 删掉一处，比留着"反正也没用"更安全。

---

## 六、前端：**仅核对，未改动**（第 10/11/12 章的内容）

按你的要求，前端只检查、不写入。**核对结果：17 / 17 项全部已就绪** ✅

| 章 | 检查项 | 结果 |
| --- | --- | --- |
| **10** | `components/TraceIdPanel.tsx` 存在（50 行） | ✅ |
| 10 | L16 `if (!traceId) return null`（无标识不渲染） | ✅ |
| 10 | L42 `traceUrl ? (...)` （有链接才给跳转） | ✅ |
| **11** | `chatStream.ts` L17/L19 事件类型含 `traceId` / `traceUrl` | ✅ |
| 11 | L123 `traceId: data.trace_id ?? null` | ✅ |
| 11 | L124 `traceUrl: data.trace_url ?? null` | ✅ |
| **12** | `ChatPage.tsx` L34 导入 `TraceIdPanel` | ✅ |
| 12 | L55/L56 UI 消息含 `traceId` / `traceUrl` | ✅ |
| 12 | L74/L75 历史回填 `m.trace_id` / `m.trace_url` | ✅ |
| 12 | L227/L228 SSE `start` 事件写入 | ✅ |
| 12 | L470 渲染 `<TraceIdPanel>` | ✅ |
| 12 | **渲染顺序**：TraceIdPanel(L470) **在** QueryRoutePanel(L473) **之前** | ✅ |
| 契约 | `types.gen.ts` L1020/L1024 已含两个字段 | ✅ |

> 📌 **结论**：第 10/11/12 章对你**不需要写代码**，只需要**确认**。
> 真正的工作量就是本章这一个后端文件。

### ⚠️ 一处**不要做**的事

教程第 10 章开头写 `npm run gen:api`（重新生成前端类型）。

**本项目不要跑它**：

| 原因 | 说明 |
| --- | --- |
| 本项目的 `types.gen.ts` 是**手工维护的完整契约**（2588 行） | 它比当前后端多出 142 个类型（含**后续几期**才实现的字段） |
| 跑 `gen:api` 会让它缩到约 1115 行 | 直接导致 `CitationList.tsx` 等文件 `error TS2339` |

> 而**本章后端新增的两个字段，前端类型里早就有**（L1020/L1024）——
> 所以**根本不需要重新生成**。

---

## 七、验证

### ① 后端静态核对（7 / 7）

| 检查项 | 位置 |
| --- | --- |
| 新 import | `schemas/chat.py` L29 |
| 解析函数定义 | L362 |
| `MessageRead.trace_id` / `trace_url` | L411 / L412 |
| `from_orm` 取号 | L441 |
| `from_orm` 组装 URL | L467 |
| 落库处只留 `trace_id` | `chat_service.py` L624 |
| chat_service 已不再写 `trace_url` | ✅ |

### ② `_parse_trace_id` 单元验证（9 / 9，含 3 种脏数据类型）

| 输入 | 输出 | 说明 |
| --- | --- | --- |
| `None` | `None` | 元数据整体缺失 |
| `{}` | `None` | 键不存在 |
| `{"trace_id": None}` | `None` | 值是 None |
| `{"trace_id": ""}` | `None` | 空字符串 |
| `{"trace_id": "   "}` | `None` | 纯空白 |
| `{"trace_id": 12345}` | `None` | **数字 → 不抛异常** |
| `{"trace_id": True}` | `None` | **布尔 → 不抛异常** |
| `{"trace_id": {"a": 1}}` | `None` | **对象 → 不抛异常** |
| `{"trace_id": "01a0cd28-..."}` | 原值 | 正常值 |

### ③ 端到端（真实问答 → 走仓储取历史，与真实接口同路径）

```
[实时] trace_id  = 01a0cd44-7c9a-7c03-b56a-e357cf3a797b
[实时] trace_url = https://smith.langchain.com/o/probe-org/projects/p/rag-knowledge-base/r/01a0cd44-...

[历史] role=user      trace_id=None
[历史] role=assistant trace_id='01a0cd44-7c9a-7c03-b56a-e357cf3a797b'
[历史] role=assistant trace_url='https://smith.langchain.com/o/probe-org/projects/p/rag-knowledge-base/r/01a0cd44-...'

[一致] 实时与历史 trace_id 相同？ True
[一致] 实时与历史 trace_url 相同？ True
[user] 用户消息两个字段都为空？ True
[落库] metadata 键 = ['agent_steps', 'query_route', 'refused', 'trace_id', 'verify_result']
[落库] 含 trace_url 吗？ False（符合预期）
（临时会话已删除）
```

> 📌 上面输出里的 `trace_url` 已按本档 §十 的修正更新为 `/r/` 段（原文为 `/runs/`）。

**四条关键结论**：

1. ✅ 实时通路与历史通路拿到**完全相同**的两个值
2. ✅ 用户消息两个字段都为 `None`（不泄漏到 user 消息）
3. ✅ 数据库里**没有** `trace_url`（现拼设计生效）
4. ✅ 但历史接口**照样能给出完整 URL**（说明现拼这条路是通的）

> ⚠️ 本档验证时**踩了一个坑**：一开始手动 `select(Message)` 查库直接喂给 `from_orm`，
> 触发 `MissingGreenlet`。**原因不是本章代码有问题**，而是绕过了仓储 ——
> `MessageRead.from_orm` 的前置条件是**调用方必须已预加载 `citations` 关系**（docstring 里写了），
> 仓储的 `list_messages` 内部用 `selectinload` 做了这件事。改用仓储后一次通过。

---

## 八、两条通路现在都通了

```
                    ┌─ 实时：SSE message_start ─→ chatStream.ts:123-124 ─→ ChatPage:227-228
trace_id / trace_url ┤                                                        ↓
                    │                                                    TraceIdPanel 渲染
                    │
                    └─ 历史：assistant.metadata（只存 trace_id）
                                 ↓
                        MessageRead.from_orm（L441/L467）
                                 ↓  现拼 trace_url
                        ChatPage.tsx:74-75 ─→ TraceIdPanel 渲染
```

**第 8 章补了上半条，本章补了下半条 —— 至此闭环。**

---

## 十、🔧 后续修订：跳转链接有两个坑（本章接通前端链路时实测暴露）

接到前端之后**点那个「在 LangSmith 中查看」打不开**，追查官方 SDK 源码后确认是**两处拼写/认知错误**，都已修正。

### 坑 ① 路径段是 `/r/`，不是 `/runs/`

**官方 SDK 的权威依据**（依赖源码，不是猜的）：

```python
# langsmith/client.py  →  Client._construct_run_url（本地拼 URL，不调后端）
f"{self._host_url}/o/{self._get_tenant_id()}/projects/p/{session_id_}/r/{run.id}?poll=true"
#                                                                      ^^^ 是 /r/

# langsmith/schemas.py  →  Run.url
f"{self._host_url}/o/{self.tenant_id}/projects/p/{self.id}"
```

| 段 | 官方 | 本档初版 | 修正 |
| --- | --- | --- | --- |
| 连接段 | `/r/` | **`/runs/`** ❌ | ✅ 改为 `/r/` |

### 坑 ② 前缀里 project 那段是**项目 UUID**，不是项目名

`Run.url` 里拼的是 `self.id`（**项目主键**），所以前端配置的 `LANGSMITH_RUN_URL_PREFIX` 形如：

```
https://smith.langchain.com/o/{org_id}/projects/p/{project_id}
                                └ UUID ┘         └ UUID ┘
```

**两个都是 UUID。** 本档初版在 `.env` 注释里写的是 `{project_name}` ——
**会把人误导成填项目名**（我一开始就误判了用户的正确配置）。

### 修正清单

| 文件 | 修正 |
| --- | --- |
| `backend/app/core/observability.py` L104 | `/runs/` → **`/r/`**，并在 docstring 里写明两条依据（含 SDK 源码出处） |
| `.env` / `.env.example` | 注释里 `{project_name}` → **`{project_id}`**，并补"获取方法"四步说明 |
| 本档 §七 输出、第 8 章归档输出、04-05 归档代码块 | 同步更新为 `/r/`（并加"后续修订"批注） |

### 实测验证（修正后）

```
② build_trace_url 拼接结果
  https://smith.langchain.com/o/8d759a3a-.../projects/p/ed18bad7-.../r/01a0cd51-...
  含 /r/ 段   ? True
  含 /runs/ ? False   ✅
③ 边界：前缀带尾斜杠 -> 无双斜杠 ✅   前缀为空 -> None ✅   空 trace_id -> None ✅
④ 真实 API：message_start 载荷 = {user_message_id, trace_id, trace_url}
  trace_url 用 /r/ ? True    结尾是 trace_id ? True
```

### 💡 这条坑的通用教训

> **"格式自洽"不等于"能用"。**
> `/runs/` 拼出来是一条**语法完全合法**的 URL，测试里也能断言"拼接正确"，
> **但点开是 404** —— 而**只有真人点一下才会发现**。
>
> → **凡是"给用户点的链接"，验证标准必须是"点得开"，而不是"拼得对"。**
> → 这次的发现路径正是：**用户配好后点了一下** → 才发现。**这就是"必须真跑一遍"的价值。**

---

## 十一、本档遗留

| # | 遗留项 | 说明 |
| --- | --- | --- |
| 1 | 未配 `LANGSMITH_RUN_URL_PREFIX` 时无跳转链接 | 设计如此；两条通路都只显示"复制"按钮 |
| 2 | `cache_hit` 仍未实现 | 属第 12 期语义缓存；本章不补 |
| 3 | `load_context` 仍未加装饰器 | 第 6 章遗留 |


---

## 关联文件索引

| 文件 | 位置 | 说明 |
| --- | --- | --- |
| `backend/app/api/schemas/chat.py` | L29 | ⭐ 新增 `from app.core.observability import build_trace_url` |
| `backend/app/api/schemas/chat.py` | L359-361 | 新增小节标题「3.3 可观测性追踪标识」 |
| `backend/app/api/schemas/chat.py` | L362-380 | ⭐ `_parse_trace_id`（两层防御） |
| `backend/app/api/schemas/chat.py` | L377-378 | 类型守卫（挡住数字/布尔/对象） |
| `backend/app/api/schemas/chat.py` | L384-386 | `MessageRead` 定义起 |
| `backend/app/api/schemas/chat.py` | L411-412 | ⭐ `trace_id` / `trace_url` 两个字段 |
| `backend/app/api/schemas/chat.py` | L441 | ⭐ `from_orm` 里的取号（含 `is_assistant` 守卫） |
| `backend/app/api/schemas/chat.py` | L467 | ⭐ `from_orm` 里的现拼 URL |
| `backend/app/api/schemas/chat.py` | L489 | 文件总行数（第 8 期为 450） |
| `backend/app/services/chat_service.py` | L610 | `extra_metadata` 组装起 |
| `backend/app/services/chat_service.py` | L618-623 | ⭐ 注释：为什么只存 `trace_id` |
| `backend/app/services/chat_service.py` | L624 | ⭐ 落库 `trace_id`（**已移除 trace_url**） |
| `backend/app/services/chat_service.py` | L670 | 文件总行数 |
| `backend/app/core/observability.py` | L90-104 | `build_trace_url`（本章新增的调用方） |
| `backend/app/api/schemas/chat.py` | L336-353 | `_parse_verify_result`（本章解析函数的参照范本） |
| `frontend/src/components/TraceIdPanel.tsx` | L15-16 / L42 | 第 10 章内容 —— **已就绪，未改** |
| `frontend/src/api/chatStream.ts` | L17 / L19 / L123-124 | 第 11 章内容 —— **已就绪，未改** |
| `frontend/src/pages/ChatPage.tsx` | L34 / L55-56 / L74-75 / L227-228 / L470 | 第 12 章内容 —— **已就绪，未改** |
| `frontend/src/client/types.gen.ts` | L1020 / L1024 | 契约已含两个字段 —— **不要跑 `gen:api`** |
