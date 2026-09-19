# 01_扩展 RAGState

> 章节：Day05_Query 优化 · 第 3 章 后端实现 · 第 1 节
> 对应文件：`backend/app/workflows/rag_state.py`
> 记录日期：2026.09.19

---

## 一、为什么先扩展 state，而不是先写节点

这一节的顺序是刻意的。回顾第 9 章的核心结论：

> **`total=False` 是"增量状态契约"在类型系统里的表达。**

state 就是**节点之间的接口定义**。因此：

```
先定 state  →  等于先把"节点之间要传什么"定死
              →  再写节点时，每个节点"读什么、写什么"已被约束住
```

**反过来做的风险**：先写 `route_query` 节点，边写边想"我需要往外传什么"——容易漏字段，或传一堆下游用不上的东西。

> **先定契约，后填实现** —— 接口设计的常规顺序。

---

## 二、新增的类型别名

```python
from typing import Literal, TypedDict     # ← Literal 为新增 import

QueryRoute = Literal["original", "rewrite", "hyde", "multi_query"]
```

### 为什么用 `Literal` 而不是 `str`

与第 11 章 `MessageRoleValue` 同理：

```
str      → 类型检查器不知道有哪些合法值，写错 'rewirte' 不报错
Literal  → 四个候选值被固化，写错立刻类型报错
```

### 为什么定义在模块级

因为会被**三处引用**：

```
① RAGState.route 的字段类型
② route_query 节点函数的返回值类型
③ 后续 QueryRouteRead schema（契约模型）
```

**模块级定义 = 单一事实来源（single source of truth）。** 若写死在 `RAGState.route` 注解里，第 ② ③ 处就得重复字面量——**三处重复，改一处忘两处是迟早的事**。

### 与前端契约的一致性

`QueryRoute` 的四个值必须与前端 `QueryRouteRead.route` **完全一致**：

```typescript
// frontend/src/client/types.gen.ts
route: 'original' | 'rewrite' | 'hyde' | 'multi_query';
```

**错一个字母的后果**：前端 `QueryRoutePanel` 的 `ROUTE_META` 是按 key 索引的 `Record`，查不到会拿到 `undefined`，面板渲染异常——**且不会在编译期暴露**。

（本项目 `Literal` 统一用双引号，与 `app/api/schemas/chat.py:32` 的 `MessageRoleValue` 风格一致。）

---

## 三、新增的四个状态字段

```python
class RAGState(TypedDict, total=False):
    # ... 已有字段 ...

    # route_query 产出
    route: QueryRoute
    rewritten_query: str | None
    hyde_answer: str | None
    multi_queries: list[str] | None

    # retrieve 产出
    retrieved_chunks: list[RetrievedChunk]
```

### 关键设计 1：`query` 被**覆盖**，而非新增字段

这是本节最重要的一句设计决策。对比两种方案：

```
方案 A：新增 retrieval_query 字段
  retrieve 必须判断：
    route == 'rewrite'     → 读 rewritten_query
    route == 'hyde'        → 读 hyde_answer
    route == 'multi_query' → 读 multi_queries
    route == 'original'    → 读 query
  ✗ 下游被迫懂"策略有哪些"，与优化逻辑强耦合
  ✗ 以后加第 5 种策略，retrieve 也要跟着改

方案 B：覆盖 query ← 本项目采用
  retrieve 永远只读 query，完全不知道策略存在
  ✓ 新增策略只改 route_query 一个文件，下游零改动
```

**这是第 9 章 `normalize_query` 建立的契约的延续：**

```
question  = 用户原话（可能残缺）
    ↓ normalize_query 加工
query     = 真正用于检索的词
    ↓ route_query 进一步加工
query     = 最终用于检索的词        ← 节点在变，契约不变
```

> **`retrieve` 从头到尾只依赖 `query` 这一个字段。**

**注意区分 `route` 与"策略知识"**：`route` 虽也传给了 `retrieve`（`multi_query` 时需要），但它是**行为开关**（要不要做多路召回），不是**策略知识**（每种策略该读哪个字段）。

### 关键设计 2：五个字段的职责划分

| 字段 | 谁写 | 谁读 | 用途 |
| --- | --- | --- | --- |
| `route` | route_query | retrieve、chat_service | **控制信号** + 调试 |
| `query` | route_query 覆盖 | **retrieve** | **控制信号**（真正喂检索） |
| `rewritten_query` | route_query | 目前无人读 | **纯调试** |
| `hyde_answer` | route_query | 目前无人读 | **纯调试** |
| `multi_queries` | route_query | **retrieve（待实现）** | **召回输入** + 调试 |

**`rewritten_query` / `hyde_answer` 目前"无人读"，为什么保留？**

```
① 前端调试面板要显示"LLM 到底改写成了什么"（QueryRouteRead 的字段）
② 出问题时能看到 LLM 的中间产物

它不是冗余设计，是可观测性：
  只有最终答案 → 答案很差 → 不知道怪谁：
      是改写跑偏了？还是改写没问题但检索召回了错的？
  有中间产物 → 一看便知："改写成『差旅费报销的规定』，但用户问的是住宿标准"
             → 定位到改写环节
```

> 一个只暴露输入输出的系统，出问题时无法定位。

### 关键设计 3：`multi_query` 时两个字段都要保留

教程明确："multi_query 路径下保留原问题，但另外补充 multi_queries"。

```
query         = 原始问句（本身仍是有效检索输入，也是兜底）
multi_queries = N 条子查询（多路召回用）
```

**两个理由：**

1. **保底**：子查询是 LLM 生成的，可能丢失原问题的约束或本身质量不高；原问题**不依赖 LLM 生成质量**
2. **防语义漂移**：若把 `query` 改成某个子查询，就丢失了原问题语义，且"N 个里挑哪个"是无法回答的问题

> **所以 `multi_query` 实际是"原问题 + N 条子查询"的 N+1 路召回。**

---

## 四、本节埋下的实现约束

加了字段，但**没有任何节点读 `multi_queries`**。当前检索节点：

```python
# app/workflows/nodes/retrieve.py:41
chunks = await retriever.search(state["query"], top_k=settings.retrieval_top_k)
                                  ↑ 只读 query
```

**因此 `multi_query` 的实现工作 100% 压在 `retrieve` 节点上**，共五件事：

### ① 把"单查询"变成"查询列表"

```
route == 'multi_query' → [state["query"], *state.get("multi_queries", [])]
其他策略                → [state["query"]]
```

### ② 循环检索 vs 批量检索（性能决策点）

```
循环调 N 次 VectorRetriever.search     → 改动小，但 embedding 也调 N 次
批量方法（推荐）                        → 用 aembed_documents 一次算完 N 条向量
                                        → 省掉 N-1 次网络往返
```

> embedding 单次实测约 1 秒（第 12 章验证记录），**3 条子查询：循环 3 秒 vs 批量 1 秒**。
> 现有 `app/ingestion/embedder.py` 已具备 `aembed_documents` 能力（`pipeline.py` 在用）。

### ③ 去重

多路召回必然命中重复 chunk（同一片段可能被原问题与子查询同时召回）：

```
去重键：chunk_id
保留谁：同一 chunk 被多路命中时保留最高分
```

**为什么保留最高分**：高分说明"某个角度的查询与它非常匹配"，这比平均分更有信息量。

### ④ 合并排序 + 截断（Top-K 语义）

```
✓ 推荐：每路各取 top_k 候选 → 合并去重 → 全局按 score 降序 → 取前 top_k
        优点：输出条数稳定，prompt 的 Token 预算可预测

✗ 另一种：每路各取 top_k 直接用（不去重不截断）
        缺点：条数随 N 波动，prompt 长度不可预测，可能撑爆上下文
```

### ⑤ 熔断判定沿用（无需改动）

```python
refused = not chunks or chunks[0].score < settings.retrieval_min_score
```

去重排序后 `chunks[0]` 即全局最高分。**多路召回天然会抬高 Top-1 分数**，这本身也是 `multi_query` 的效果来源之一。

### ⚠️ 边界：空子查询要过滤

`multi_queries` 类型是 `list[str] | None`，可能含空字符串：

```
queries = [q for q in queries if q and q.strip()]
→ 过滤后若只剩原始 query，自然退化成单查询路径
```

**不做这层防御，空子查询就会走进 embedding API** —— 正是第 2 章"避免空 query 去 embedding"那条降级规则的同一个坑。

---

## 五、实现细节提醒

`total=False` 意味着所有字段可能不存在：

```python
state["question"]                  # ✓ 初始输入必有，安全
state.get("multi_queries", [])     # ✓ 可选字段用 .get() 带默认值
state["multi_queries"]             # ⚠️ 若未写入会 KeyError
```

> 同类坑在第 10 章已踩过一次（`chat_service` 里的 `state.get("retrieved_chunks", [])`）。

---

## 六、验证记录

```
1) py_compile                        → exit 0 ✅
2) QueryRoute 取值（get_args）        → ['original','rewrite','hyde','multi_query']
   前端契约取值（正则解析 types.gen.ts）→ ['original','rewrite','hyde','multi_query']
   *** 前后端集合一致: True ***                                        ✅
3) RAGState 结构                      → 13 字段 / 必填 0 / 可选 13      ✅
```

**第 2 项特意不靠肉眼比对**：用 `typing.get_args(QueryRoute)` 取后端值，用正则从 `types.gen.ts` 抽取前端契约值，再做集合比较。因为**策略名写错不会在编译期暴露**（见第二节）。

---

## 七、自测题与批改记录

### 题目与作答

| # | 题目 | 作答 | 判定 |
| --- | --- | --- | --- |
| 1 | 为什么"覆盖 `query`"而不是"新增 `retrieval_query` 字段"？从下游耦合度说明 | 「下游节点被迫懂策略有哪些，与优化逻辑强耦合；以后加第 5 种策略 retrieve 也要跟着改。覆盖就不需要懂这么多策略」 | ✅ |
| 2 | `rewritten_query` / `hyde_answer` 无人读取，为何保留？ | 「防止系统只能看到输出质量的好坏，可以在调试中发现到底是哪种策略发生的问题」 | ✅ |
| 3 | `multi_query` 时为何 `query` 与 `multi_queries` 都要保留？ | 「multi_queries 是多路召回的子查询，而 query 是原始问题，至少要保证兜底，防止只有子查询导致语义被更改」 | ✅ |
| 4 | `QueryRoute` 为何定义在模块级？ | 「因为 route 的类型会被多处引用」 | ✅ |
| 5 | 加完字段后 `retrieve` 必须做哪些改动才能支持 `multi_query`？ | 「不知」 | ❌ |

### 逐题批改

**第 1 题 —— 正确**

表述可再精确一句：**策略知识只存在于 `route_query` 内部**。

```
覆盖 query 的方案 → retrieve 永远只有一行：q = state["query"]
```

**第 2 题 —— 正确**

精确价值是**能区分失败发生在哪一环**：

```
只有最终答案 → 答案很差 → 不知怪谁（改写跑偏？检索召回错？生成没用对？）
有 rewritten_query → 一看："改写成『差旅费报销的规定』，但用户问住宿标准" → 定位到改写环节
```

**第 3 题 —— 正确**

两个理由都抓到。补充：`multi_query` 实为"原问题 + N 子查询"的 **N+1 路召回**，原问题是**不依赖 LLM 的保底路径**。

**第 4 题 —— 正确**

具体是三处引用：`RAGState.route` 注解、`route_query` 返回类型、后续 `QueryRouteRead` schema。

**第 5 题 —— 本节重点，详见第四节**

五件事：**查列表 → 循环/批量检索 → 去重 → 合并排序截断 → 熔断沿用**，外加空串防御。

其中第 ② 项有个可落地的性能优化点：用 `aembed_documents` 批量算向量（现有 `embedder` 已具备该能力）。

---

## 八、本节两条结论

### 结论一：接口先行 —— 先定 state，再写节点

state 是节点间的接口契约。先定契约，写节点时"读什么、写什么"就已被约束住。

### 结论二：向下游隐藏变化 —— 用"覆盖"而非"新增字段"

```
覆盖 query  → 下游只依赖稳定契约（一个字段），与策略解耦
新增字段    → 下游被迫懂所有策略，与优化逻辑强耦合
```

> 这是第 9 章"契约稳定性 / 防腐层"思想的第二次应用。

---

## 九、一句话总结

> 本节为 Query 优化扩展状态契约：新增 `QueryRoute` 字面量类型（与前端 `QueryRouteRead.route` 严格对齐）与 `route` / `rewritten_query` / `hyde_answer` / `multi_queries` 四个字段；核心设计是**让 `route_query` 覆盖 `query` 而非新增检索字段**，使 `retrieve` 永远只依赖一个稳定契约、与策略解耦；`rewritten_query` / `hyde_answer` 是纯调试字段（提供可观测性的中间产物），`multi_queries` 则是待实现的召回输入——由此推出 `retrieve` 必须完成"多路检索 + 去重 + 合并截断"五件事。

---

## 十、关联文件索引

| 文件 | 关键位置 | 说明 |
| --- | --- | --- |
| `app/workflows/rag_state.py` | L16 | `from typing import Literal, TypedDict` |
| `app/workflows/rag_state.py` | L29 | `QueryRoute` 类型别名 |
| `app/workflows/rag_state.py` | L32 | `RAGState` 类定义 |
| `app/workflows/rag_state.py` | L47 | `query`（被覆盖字段，注释已标明） |
| `app/workflows/rag_state.py` | L51 | `route` |
| `app/workflows/rag_state.py` | L54-56 | `rewritten_query` / `hyde_answer` / `multi_queries` |
| `app/workflows/nodes/retrieve.py` | L41 | 单查询调用，`multi_query` 需扩展此处 |
| `app/retrieval/vector_retriever.py` | L58 | `search(query, top_k)` 单查询签名 |
| `app/ingestion/embedder.py` | — | 已具备 `aembed_documents` 批量向量化能力 |
| `app/api/schemas/chat.py` | L32 | `MessageRoleValue`（`Literal` 风格参考） |
| `frontend/src/client/types.gen.ts` | L1036 | `QueryRouteRead` 契约（`route` 四值须一致） |
