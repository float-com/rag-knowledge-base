# 08_ChatService透传trace_id

> 期：**Day09 · 可观测性**
> 章：第 8 章 ChatService.stream_answer 透传 trace_id
> 记录日期：2026.09.23
> ⚠️ **后续修订**：本档 §五「改动 4：落库」里"同时落库 `trace_url`"的做法，
> 已在第 9 章被**移除**（改为 API 层现拼）。详见同目录 `09_MessageRead暴露trace字段.md` §五。

---

## 一、本章做什么

只动 **1 个文件**（`backend/app/services/chat_service.py`，669 行），共 **4 处**改动。

| # | 位置 | 改什么 |
| --- | --- | --- |
| 1 | L360-373 | **取号**：函数体内取一次 `trace_id`，塞进 `RAGState` |
| 2 | L400-409 | **下发**：`message_start` 载荷追加 `trace_id` / `trace_url` |
| 3 | L326-347 | **文档**：docstring 补"事件协议"与"时序约束" |
| 4 | L617-623 | **落库**：`extra_metadata` 追加 `trace_id` / `trace_url` |

**做完的效果**：一次回答的追踪标识**在实时对话就能看到，刷新页面后历史回看也还在**。

---

## 二、本章解决的正是第 1 章预判的那个决策点

第 1 章 §3.6 提过：`trace_id` 只能在函数体内取，而 `message_start` 发得太早 —— 当时给了三个备选方案。

**教程选的正是推荐方案 A**：

```
✅ 不新增事件类型
✅ 不调整 message_start 的位置
✅ 只是把两个字段塞进 message_start 已有的载荷里
→ 前端一行都不用改（chatStream.ts 早已在读这两个字段）
```

**为什么方案 A 是最优的**：前端 `chatStream.ts` 的 `message_start` 解析分支**本来就写了**
`data.trace_id` / `data.trace_url`（L123-124），只是后端一直没发。
**补上后端，正好接上那条已经铺好的线** —— 这也解释了为什么前几期反复出现"前端字段超前"的现象。

---

## 三、改动 1：取号（L360-373）

```python
                # 2.1 【第 9 期】取本次回答的 trace_id。
                #     【为什么必须写在这里】观测 SDK 的取号函数是从"当前执行上下文"里读的，
                #       而上下文只在本函数体内有效 —— 一旦函数返回就被清掉，外面再取必然是 None。
                #       本函数带着 @traceable 装饰器，所以进入函数体时根 span 已经建好。
                #     【未启用观测时】该函数直接返回 None，下面所有透出逻辑自然全部退化为"没有追踪信息"，
                #       不需要在这里写任何特判。
                trace_id = get_current_trace_id()

                state: RAGState = {
                    "conversation_id": conversation_id,
                    "question": question,
                    # 塞进状态后全链路可读（图内节点只透传、不产出）
                    "trace_id": trace_id,
                }
```

**两个设计点**：

| 点 | 说明 |
| --- | --- |
| **必须在函数体内** | 第 6 章已实测：出了被装饰函数的函数体，取号一律返回 `None` |
| **不写"未启用"特判** | 未启用时取号函数自己返回 `None`，**整条透出链自然退化** —— 这正是第 4 章 fail-soft 设计的兑现 |

---

## 四、改动 2：下发（L400-409）

```python
                yield {
                    "event": "message_start",
                    "data": {
                        "user_message_id": str(state["user_message_id"]),
                        "trace_id": trace_id,
                        "trace_url": build_trace_url(trace_id),
                    },
                }
```

| 字段 | 未启用时 | 未配 URL 前缀时 |
| --- | --- | --- |
| `trace_id` | `None` | 有值 |
| `trace_url` | `None` | **`None`**（前端只显示"复制"按钮） |

> **`None` 而不是空字符串**：前端 `chatStream.ts` 用的是 `??`（只兜 `null` / `undefined`），
> **空串会原样透传**，反而让前端以为"有链接"。这个细节第 4 章的 `build_trace_url` 里已经处理过。

---

## 五、改动 4：落库（L617-623）

```python
            "agent_steps": _serialize_agent_steps(state),
            # 【第 9 期】LangSmith trace 信息落库：刷新页面 / 翻历史时前端仍能展示与跳转。
            #   【为什么 trace_url 也要存】前端 TraceIdPanel 只读它收到的那一个 URL 字段、
            #   并不会自己拿 trace_id 去拼（拼接需要私有的 URL 前缀，前端无从得知），
            #   所以只存 trace_id 会让历史回看退化成"只有 ID、没有跳转链接"。
            #   两处都走同一对取号/拼链接函数，保证实时下发与历史回看看到的是同一份内容。
            "trace_id": state.get("trace_id"),
            "trace_url": build_trace_url(state.get("trace_id")),
        }
```

### ⭐ 这里比教程**多存了一个字段**

教程只让落 `trace_id`：

```python
# 教程
"trace_id": state.get("trace_id"),
```

**本档补了 `trace_url`，依据是"先查消费者"**：

```tsx
// frontend/src/components/TraceIdPanel.tsx:42-46（全文件 50 行）
{traceUrl ? (
  <a href={traceUrl} target="_blank" rel="noreferrer">
    在 LangSmith 中查看 ↗
  </a>
) : null}
```

**前端只读 props 里的 `traceUrl`，没有任何"拿 trace_id 去拼"的逻辑。**

而拼 URL 需要 `langsmith_run_url_prefix`（含 org / workspace 信息，**属于后端私有配置，前端无从得知**）。

→ **只存 `trace_id` 的后果**：
- 实时对话：有跳转链接（因为 SSE 那条路发了 `trace_url`）
- 刷新页面后：**跳转链接消失**，只剩下一个能复制的裸 ID

**这是一个"实时与历史表现不一致"的隐性缺口，靠落库补上。**

> ### ⚠️ 本节的结论**已被第 9 章推翻**（保留原文以便对照）
>
> 第 8 章我按"前端不会自己拼 URL"推断出"那就把 URL 也落库"。
> **第 9 章给出了更好的答案**：拼接 URL 是**表现层关切**，应该放在 **API 层每次响应现拼**，
> 而不是把派生数据写进数据库。
>
> | | 第 8 章的做法 | 第 9 章的最终做法 |
> | --- | --- | --- |
> | metadata 里存什么 | `trace_id` + `trace_url` | **只有 `trace_id`** |
> | `trace_url` 从哪来 | 读数据库 | **每次响应按当前配置现拼** |
> | 换 LangSmith 工作区后 | 老消息是**旧链接**，要回填 | **所有历史消息自动跟着新规则** |
>
> **当前代码状态**：`chat_service.py` 里那行落库**已被第 9 章删除**
> （见 `09_MessageRead暴露trace字段.md` §五）。
> **本节以下关于"落库 trace_url"的描述只反映当时的中间状态，不是最终形态。**

---

## 六、⭐ 三层时间约束（比第 1 章预想的更紧）

第 1 章只发现了"函数体内"这一条约束。实施时发现**是三条同时压在同一段代码上**：

```
① 取 trace_id 必须在函数体内              （出了函数体 → None）
② 落库用户提问必须早于图执行              （否则 load_context 把本轮提问当历史读回来）
③ message_start 必须晚于用户提问落库       （要带 user_message_id）
```

**三条的交集只有一个位置** → 就是现在这个顺序。

**所以本档在 docstring 里加了一段显式告诫**（L337-347）：

```python
        【第 9 期 · trace_id 的时序约束（本方法最容易被改坏的一处）】：
        `get_current_trace_id()` 读的是"当前执行上下文"，**只在被 @traceable 装饰的函数体内有效**，
        函数返回之后上下文即被清掉 —— 所以在外面（例如路由层、图节点里）取一律是 None。
        与此同时，`message_start` 必须在**落库用户提问之后**才发（要带 user_message_id），
        而落库又必须早于图执行（否则 load_context 会把本轮提问当成历史读回来）。
        因此本方法的顺序被三重约束钉死，**不要把取号或 message_start 往上/往下挪**。
```

> **为什么值得专门写一段**：这三条约束**单独看都合理，合起来才致命** ——
> 以后有人为了"让前端更早拿到 ID"把 `message_start` 往上挪一行，程序**不会报错**，
> 只会静默失去 `trace_id`。**静默失效比报错危险**，所以留字。

---

## 七、验证（两种模式各跑一轮真实问答）

### 未启用观测（默认状态）

```
事件数 = 10  首 = message_start  末 = message_end
message_start 载荷 = {'user_message_id': 'b66f963f-...', 'trace_id': None, 'trace_url': None}
trace_id 是 None 而不是空串？ True
trace_url 是 None 而不是空串？ True
落库 metadata 键 = [agent_steps, query_route, refused, trace_id, trace_url, verify_result]
落库 trace_id = None        与实时下发一致？ True
```

### 启用观测

```
事件数 = 9   首 = message_start  末 = message_end
message_start 载荷 = {
  'user_message_id': '368e9a91-...',
  'trace_id': '01a0cd28-59c9-75f3-ba9e-5c919b74cb15',
  'trace_url': 'https://smith.langchain.com/o/probe-org/projects/p/rag-knowledge-base/runs/01a0cd28-...'
}
trace_url 拼对？ True
落库 trace_id  = 01a0cd28-59c9-75f3-ba9e-5c919b74cb15
落库 trace_url = https://smith.langchain.com/o/probe-org/projects/p/rag-knowledge-base/runs/01a0cd28-...
与实时下发一致？ True
```

### 汇总

| 检查项 | 结果 |
| --- | --- |
| 4 处改动静态核对 | ✅ |
| 未启用：字段为 `None` 而非空串 | ✅ |
| 启用：`trace_id` 有值 | ✅ |
| 启用：`trace_url` 拼接正确（无双斜杠、以 trace_id 结尾） | ✅ |
| 落库：metadata 含两个键 | ✅ |
| **落库值与实时下发一致** | ✅ |
| 应用启动正常、路由数 8 不变 | ✅ |
| 临时会话自动清理（无残留） | ✅ |

---

## 八、两条数据通路（本章补齐了第二条）

```
                    ┌─ 实时对话：SSE message_start 载荷 ──→ chatStream.ts:123-125
trace_id / trace_url ┤                                          ↓
                    │                                     ChatPage 灌进 UI 消息
                    │                                          ↓
                    │                                     TraceIdPanel 渲染
                    └─ 历史回看：落库 assistant.metadata ──→ ⚠️ MessageRead（下一章才补）
                                                               ↓
                                                          ChatPage.tsx:74-75
```

> ⚠️ **本章只把数据写进了数据库，接口还没暴露它。**
> 前端历史回看读的是 `MessageRead` 的**顶层字段**（`ChatPage.tsx:74-75`），
> 而后端 `MessageRead`（`schemas/chat.py:360-380`）**目前没有** `trace_id` / `trace_url`。
>
> → **要等第 9 章给 Schema 加字段 + 在 `from_orm` 里从 metadata 取出来，历史回看才能真正显示。**

---

## 九、本档遗留

| # | 遗留项 | 说明 |
| --- | --- | --- |
| 1 | `MessageRead` 未暴露 `trace_id` / `trace_url` | **第 9 章**解决；否则历史回看拿不到 |
| 2 | 未配 `LANGSMITH_RUN_URL_PREFIX` 时无跳转链接 | 设计如此（前缀含私有 org 信息）；想要链接必须配 |
| 3 | `cache_hit` 仍未实现 | 属**第 12 期**语义缓存；本章不补（本章做才补的判据） |
| 4 | `load_context` 仍未加装饰器 | 第 6 章遗留，非本章范围 |

---

## 关联文件索引

| 文件 | 位置 | 说明 |
| --- | --- | --- |
| `backend/app/services/chat_service.py` | L48 | `from app.core.observability import build_trace_url, get_current_trace_id` |
| `backend/app/services/chat_service.py` | L326-332 | docstring：事件协议（含本章新增的两个字段说明） |
| `backend/app/services/chat_service.py` | L334-343 | ⭐ docstring：三层时序约束告诫 |
| `backend/app/services/chat_service.py` | L360-365 | 取号处的 5 行注释（为什么必须在这里） |
| `backend/app/services/chat_service.py` | L366 | ⭐ `trace_id = get_current_trace_id()` |
| `backend/app/services/chat_service.py` | L368-373 | `RAGState` 初始化（含 `trace_id`） |
| `backend/app/services/chat_service.py` | L372 | `"trace_id": trace_id,` |
| `backend/app/services/chat_service.py` | L396-401 | `message_start` 的注释（为什么放这里、为什么不用新事件） |
| `backend/app/services/chat_service.py` | L402-409 | ⭐ `message_start` 载荷（三个字段） |
| `backend/app/services/chat_service.py` | L406 | `"trace_id": trace_id,` |
| `backend/app/services/chat_service.py` | L407 | `"trace_url": build_trace_url(trace_id),` |
| `backend/app/services/chat_service.py` | L617-621 | ⭐ 落库处的 5 行注释（为什么 `trace_url` 也要存） |
| `backend/app/services/chat_service.py` | L622-623 | ⭐ 落库两个字段 |
| `backend/app/services/chat_service.py` | L669 | 文件总行数 |
| `backend/app/core/observability.py` | L59-87 | `get_current_trace_id`（本章的取号来源） |
| `backend/app/core/observability.py` | L90-104 | `build_trace_url`（本章的拼链接来源） |
| `frontend/src/api/chatStream.ts` | L123-124 | **前端早已在读** `data.trace_id` / `data.trace_url`（本章补上的正是这条线的后端端） |
| `frontend/src/components/TraceIdPanel.tsx` | L42-46 | **只读 props.traceUrl、不自己拼** —— 本档补存 `trace_url` 的直接依据 |
| `frontend/src/pages/ChatPage.tsx` | L74-75 | 历史回看读 `m.trace_id` / `m.trace_url`（**要等第 9 章才通**） |
| `backend/app/api/schemas/chat.py` | L360-380 | `MessageRead` —— **本章尚未暴露这两个字段**，第 9 章补 |
