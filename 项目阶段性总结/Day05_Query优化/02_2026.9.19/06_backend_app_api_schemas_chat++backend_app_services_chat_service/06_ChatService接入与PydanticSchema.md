# 06_ChatService 接入与 Pydantic Schema

> 章节：Day05_Query 优化 · 第 3 章 后端实现 · 第 7 节（ChatService 接入 + SSE 事件）与第 8 节（Pydantic Schema）
> 对应文件：`backend/app/services/chat_service.py`、`backend/app/api/schemas/chat.py`
> 记录日期：2026.09.19

> **说明**：这两节合为一份记录。原因是它们构成一个**不可分的功能单元**——第 7 节把路由结果写进 metadata（持久化），第 8 节把它从 schema 暴露出去（可回看），缺任一节功能都是断的。

---

## 一、两节的范围

**第 7 节（接入 + SSE）三处改动：**

```python
# ① 导入
from app.workflows.nodes import (load_context, normalize_query, retrieve, route_query, stream_generate)

# ② 新增载荷构造函数（模块级）
def _build_query_route_payload(state: RAGState) -> dict: ...

# ③ stream_answer 执行顺序
state.update(await route_query(state))                                    # 紧跟 normalize_query
yield {"event": "query_route", "data": _build_query_route_payload(state)} # 紧跟 message_start

# ④ metadata 落库
extra_metadata={"refused": ..., "query_route": _build_query_route_payload(state)}
```

**第 8 节（Schema）结构：**

```
QueryRouteValue                    # 新增字面量别名
    ↓ 被引用
QueryRouteRead                     # 新增模型
    ↓ 被引用
_parse_query_route(metadata)       # 新增独立解析函数
    ↓ 被调用
MessageRead.from_orm               # 新增 query_route 字段并填充
```

---

## 二、第 7 节：一份数据、三个消费方

三处改动**不是同一件事做三遍**，而是服务于三个不同场景：

| 改动 | 消费方 | 缺了会怎样 |
| --- | --- | --- |
| ① `state.update(await route_query(state))` | **下游 `retrieve` 节点** | `state` 无 `route` 键 → 双路径 `if` 永为 False → **多路召回永不触发** |
| ② `yield query_route 事件` | **前端（实时）** | 面板只能等刷新后才可能显示，实时性丢失 |
| ③ 写 `metadata` | **前端（刷新后）** | 刷新即丢（但注意：只写还不够，须在第 8 节暴露到 schema） |

> **①是本期从"零件齐全"到"整机可跑"的转折点。**
> 这也是"写了但未生效"的典型状态——代码全对、测试全过，少一行接线功能就永不执行。

---

## 三、`_build_query_route_payload` 的两个设计点（L90）

```python
return {
    "route": state.get("route", "original"),          # ① 缺省 original
    "query": state.get("query", ""),
    "rewritten_query": state.get("rewritten_query"),  # ② 键保留，未产出为 None
    "hyde_answer": state.get("hyde_answer"),
    "multi_queries": state.get("multi_queries"),
}
```

### ① 缺省值 `"original"` 的两层作用

```
层面一 语义兜底：
  缺省 = "本次没有优化" = 用原问题检索 = 基线行为
  （与"降级回 original"同一语义）

层面二 契约兜底：
  前端按 route 索引面板配置（ROUTE_META）
  若拿到 undefined 会查不到配置而渲染异常
  → 缺省值保证前端永远收到四个合法值之一
```

**这与第 5 节 `retrieve` 里的 `.get()` 是配套的防御**：

```
retrieve 侧：state.get("route")             → 拿不到就不走多路（退化为单路）
payload 侧： state.get("route", "original") → 拿不到就给前端一个合法值
                                            ↑ 同一个前提：路由可能没被调用
```

### ② 键保留、值为 None（与 state 内做法相反）

| 边界 | 对 None 的处理 | 原因 |
| --- | --- | --- |
| `route_query` 节点 → state | **只写非 None 字段** | `total=False` 契约："字段不存在"是干净语义 |
| `_build_query_route_payload` → SSE | **始终带字段，值可为 None** | 前端 TS 类型是 `T \| null`，"存在且为 null"更利于类型推断 |

> **同一件事在两个边界上处理相反——因为契约的另一端不同。设计取舍要看"对面是谁"，没有普适答案。**

---

## 四、`query_route` 为什么放在 `message_start` 之后、`retrieve` 之前（L253）

**时间线归因（关键）**：

```
0ms        message_start   → 前端渲染用户气泡
~1.4-3.1s  query_route     ← 路由这次 LLM 调用完成（必须花的时间）
~2.4s      citations       ← 又要等一次向量化（约 1 秒）
```

**要"白等"的不是路由那 1.4~3.1 秒（那是必须算的），而是路由算完后、检索又花的那 1 秒：**

```
若放在 retrieve 之后：
  路由结果 1.4s 就有了，却压到 2.4s 才发 → 白白压着 1 秒

放在 retrieve 之前：
  路由一算完立刻发 → 面板 1.4s 就渲染出来
```

**与第 10 章同构的第二次应用**：

| | 第 10 章 | 本期 |
| --- | --- | --- |
| 优化对象 | `message_start` 提到 `retrieve` 之前 | `query_route` 提到 `retrieve` 之前 |
| 目的 | 不让 embedding 的 1 秒拖延首屏 | 不让 embedding 的 1 秒拖延路由展示 |

> **规律：凡是"已算好且前端需要"的数据，能早发就早发；凡是"还要等外部调用"的，别让它阻塞已有信息的展示。**

---

## 五、为什么 `original` 时也照常下发（**职责划分**）

```python
yield {"event": "query_route", "data": _build_query_route_payload(state)}
# 没有 if route != "original" 判断
```

**前端自己处理**（`QueryRoutePanel.tsx:23`）：

```tsx
if (queryRoute.route === 'original') return null;
```

**后端不特判的三个收益**：

```
① 少一个分支 → 少一处可能不一致的地方
② 契约更简单：客户端约定"总会收到 query_route 事件"
③ 前端将来想在 original 时显示别的（如"本次未优化"），不需后端配合
```

> **职责划分原则：谁负责展示，谁决定要不要展示；数据提供方只管把事实说全。**
> 后端特判 = 替前端做展示决策 = **职责越界**。

---

## 六、第 8 节：为什么"落库"不等于"可回看"

**落库成功了，但 Pydantic 响应模型不含该字段 → 序列化时被丢弃 → 前端 `m.query_route = undefined` → 面板不显示。**

**前端读的是顶层字段**（`ChatPage.tsx:71`）：

```tsx
queryRoute: m.query_route ?? null,
//  ↑ 顶层，不是 m.metadata.query_route
```

**完整链路是三级传递**：

```
① 写    state → _build_query_route_payload() → extra_metadata（DB）
② 读    DB → Message.extra_metadata → MessageRead.from_orm → query_route 字段
③ 用    前端 m.query_route → <QueryRoutePanel/>
```

> **"持久化"和"可回看"是两件事。数据落库只是第一步；要能回到用户眼前，必须让它在 API 契约里出现。**

**旁注**：前端 `MessageRead` 契约里还有 `agent_steps` / `verify_result` / `trace_id` / `trace_url` / `cache_hit` 五个字段，均属后续几期（Agent 编排、答案校验、可观测、语义缓存）。这与"成品前端按完整契约生成、后端逐期实现"的模式一致。

---

## 七、`_parse_query_route` 的三层防御（L135）

```python
def _parse_query_route(metadata: dict | None) -> QueryRouteRead | None:
    if not metadata:                                # 拦 ①
        return None
    raw = metadata.get("query_route")
    if not isinstance(raw, dict):                   # 拦 ② ③
        return None
    try:
        return QueryRouteRead.model_validate(raw)   # 拦 ④
    except Exception:
        return None
```

### 四类会被喂进来的输入

`metadata` 是 JSONB 自由结构，**不受 Schema 约束**，因此必须假设它可能是：

```
① None / {}             → 老数据、本功能上线前写入的消息
② 缺 query_route 键      → user 消息从不写该键
③ 键存在但不是 dict      → 极端脏数据（手工改库改错等）
④ 键是 dict 但结构不符   → 缺必需字段、route 值非法
```

### 去掉 `try/except` 的后果

```
第 ④ 类输入让 model_validate 抛 ValidationError
→ 从 _parse_query_route 冒出
→ 冒到 MessageRead.from_orm
→ 冒到 GET /api/conversations/{id}
→ 全局异常处理器兜住 → 500
→ 【整个会话历史打不开】

而实际上会话与消息都完好，
仅仅因为【一条消息里的一段可选元数据】格式不对，整个历史列表全废。
```

> **核心原则：可选的辅助数据，绝不能拖垮主接口。**
> 解析失败就当作"这条消息没有这个数据"，只让它的**调试面板不显示**——这是**局部降级**，而非全局失败。

> 这是第 2 章"业务级降级"思想用在数据读取上，也与第 9 章"拒答熔断"的取舍方向一致：**宁可少显示一点，不要让整条链路断掉。**

---

## 八、重构的两处细节（教程 diff 里的）

### ① 角色判断缓存（L203）

```python
is_assistant = message.role == "assistant"       # 只判断一次
...
if is_assistant                                   # 引用过滤
_parse_query_route(...) if is_assistant else None # 快照提取
```

**为什么**：引用过滤与快照提取都依赖它，**重复写 `message.role == "assistant"` 容易在改动时漏改其中一处**。

### ② 抽出 `QueryRouteValue` 别名（L37）

```
QueryRouteValue            ← schemas 层（本次新增）
QueryRoute                 ← workflows 层
前端 QueryRouteRead.route   ← 前端契约
        ↑ 三处必须是同一批字符串
```

**抽成别名的价值是单一事实来源**。跨层重复（schemas 与 workflows 各一份）无法避免，但**至少 schemas 内部只有一处**。

---

## 九、自测题与批改记录

| # | 题目 | 作答 | 判定 |
| --- | --- | --- | --- |
| 1 | 第 7 节三处改动分别服务什么场景？缺哪处会怎样？ | 「调用 query 优化；把路由结果推给前端调试面板；刷新历史时前端调试面板还能继续展示」 | ✅ 第①处需精确 |
| 2 | `state.get("route", "original")` 的缺省值有什么用？ | 「保证无论如何用户的初始问题都能兜底；有人漏调 route_query → state 里没有 route 键」 | ✅ 需补契约层面 |
| 3 | `query_route` 为何放在 `message_start` 之后、`retrieve` 之前？ | 「保证能把给前端的数据直接给了，否则前端会白等 1、2 秒」 | ✅ 归因需准确 |
| 4 | 为何 `original` 时也照常下发，而不加 `if` 跳过？ | 「忘了」 | ❌ |
| 5 | `_parse_query_route` 为何要三层防御？去掉 `try/except` 会怎样？ | 「不知」 | ❌ |

### 逐题批改

**第 1 题 —— 三个场景抓对了，第①处原因要精确**

```
① 的真实作用：让【下游 retrieve 节点】知道走单路还是多路
   （缺它：多路召回永不触发）
```

**第 2 题 —— 正确，补"契约兜底"层面**

```
层面一（作答）：语义兜底 —— 缺省 original = 基线行为
层面二：契约兜底 —— 前端按 route 索引 ROUTE_META，undefined 会渲染异常
```

**第 3 题 —— 方向对，归因需准确**

要"白等"的是**路由算完后、检索又花的那 1 秒**，不是路由本身耗时（那是必须算的）。

**第 4 题 —— 职责划分（详见第五节）**

后端只负责"如实下发这次用了什么策略"；**"要不要渲染面板"是展示逻辑，属于前端**。
后端特判 = 替前端做展示决策 = 职责越界，且会让两端耦合、契约变复杂。

**第 5 题 —— 详见第七节**

四类脏输入；去掉 `try/except` 会让**一条消息的坏元数据导致整个历史接口 500**。

---

## 十、验证记录

### ① `_parse_query_route` 防御层（10 组输入）

```
None                     -> None    ✅
空 dict                   -> None    ✅
缺 query_route 键          -> None    ✅
键不是 dict                -> None    ✅
键是 list                 -> None    ✅
结构不符（缺 query）        -> None    ✅
route 值非法               -> None    ✅
合法（original）           -> route=original query='问题'          ✅
合法 + 多余字段             -> route=rewrite，多余字段被忽略        ✅
合法（multi_query）        -> route=multi_query                  ✅
```

### ② `from_orm` 行为

```
user 消息                -> query_route = None          ✅ 角色过滤生效
assistant 无 metadata    -> query_route = None          ✅ 不报错
assistant 有快照          -> route=hyde query='假设答案'  ✅
```

### ③ 字面量一致性 + 端到端回归

```
QueryRouteValue == QueryRoute（四个值完全一致）           ✅
SSE 事件序列: message_start -> query_route -> citations -> token -> message_end  ✅
GET 历史接口 -> 200
  role=user       query_route = None
  role=assistant  query_route = route=multi_query
  顶层字段存在: True                                    ✅ 前端能取到
DB metadata keys = ['query_route', 'refused']            ✅
```

---

## 十一、一句话总结

> 第 7 节用**三处改动**把 Query 优化接入主链路——调用节点（让 `retrieve` 知道走哪条路径）、下发 `query_route` 事件（前端实时展示）、写入 metadata（前端刷新后可回看），三者为**一份数据、三个消费方**；事件位置紧贴 `message_start` 之后，避免让检索耗时压着已经算好的路由结果不发；`original` 时照常下发，因为展示决策属于前端。第 8 节补上**可回看**所需的最后一环：把 metadata 里的快照经 `_parse_query_route` 校验后暴露为 `MessageRead` 的顶层字段，并以三层防御保证"一段可选元数据格式错误不会让整个历史接口 500"。

---

## 十二、关联文件索引

| 文件 | 关键位置 | 说明 |
| --- | --- | --- |
| `app/services/chat_service.py` | L50 | 导入 `route_query` |
| `app/services/chat_service.py` | L90 | `_build_query_route_payload` 定义 |
| `app/services/chat_service.py` | L231 | 调用 `route_query`（紧跟 normalize_query） |
| `app/services/chat_service.py` | L253-254 | 下发 `query_route` 事件（紧跟 message_start） |
| `app/services/chat_service.py` | L367 / L372 | metadata 写入快照 |
| `app/api/schemas/chat.py` | L37 | `QueryRouteValue` 字面量别名 |
| `app/api/schemas/chat.py` | L113 | `QueryRouteRead` 模型 |
| `app/api/schemas/chat.py` | L135 | `_parse_query_route` 三层防御 |
| `app/api/schemas/chat.py` | L166 | `MessageRead` 新增 `query_route` 字段 |
| `app/api/schemas/chat.py` | L181 / L203 / L216 | `from_orm` 与角色缓存 |
| `app/workflows/nodes/route_query.py` | L31 | 被接入的节点 |
| `app/workflows/nodes/retrieve.py` | L36 | 消费 `route` 决定单路/多路 |
| `frontend/src/pages/ChatPage.tsx` | L71 | `queryRoute: m.query_route ?? null`（顶层字段） |
| `frontend/src/pages/ChatPage.tsx` | L472-473 | 有 `queryRoute` 才渲染面板 |
| `frontend/src/components/QueryRoutePanel.tsx` | L23 | `original` 时不渲染面板 |
