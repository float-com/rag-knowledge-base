# 04_检索节点接入HybridRetriever与召回元信息落库

> 章节：Day06_全文检索、混合检索与 RRF · 第 3 章 开发实现 · **第 9-10 步（两章合并）**
> 覆盖：
> - 第 9 章 `retrieve` 节点接入 `HybridRetriever`（`app/workflows/nodes/retrieve.py`）
> - 第 10 章 召回元信息落库 + SSE 载荷（`app/services/chat_service.py`）
> 记录日期：2026.09.20

---

## 一、为什么这两章必须合并理解

第 8 章 `HybridRetriever` 做了**一个正确但有代价的决定**：把 `score` 改写成 `rrf_score`。这个决定**连锁要求**后两章同步调整：

```
第 8 章：HybridRetriever 把 score 改写成 rrf_score（契约上正确）
        ↓
第 9 章：① retrieve 节点若继续用 score 比阈值 → 恒拒答（必须改用 vector_score）
        ② retrieve 签名去掉 session（因为 HybridRetriever 刻意不接收会话）
        ↓
第 10 章：一份元信息要同时走「实时 SSE」与「历史落库」两条路
```

**所以把两章放在一份归档里，才能看清这条因果链。**

| 文件 | 改动 | 行数变化 |
| --- | --- | --- |
| `app/workflows/nodes/retrieve.py` | 整文件改写 | 99 → **143** |
| `app/services/chat_service.py` | 新增 1 函数、改 3 处 | 398 → **450** |

---

## 二、第 9 章：六处改动全景

```python
# ① 签名去掉 session
async def retrieve(state: RAGState) -> RAGState:                    # L18

# ② 换检索器（原来 VectorRetriever(session)）
retriever = HybridRetriever()                                       # L35

# ③ 两个召回口径分开
recall_top_k = settings.retrieval_recall_top_k    # 20              # L40
final_top_k  = settings.retrieval_top_k           # 5               # L41

# ④ multi_query 不再并入原始问题
for sub_query in state["multi_queries"] or []:                      # L53

# ⑤ 拒答判定抽成函数（并从 score 改成 vector_score）
refused = _should_refuse(chunks)                                    # L71

# ⑥ _merge_chunks 改用 rrf_score 排序（含 or 0.0 兜底）
if prev is None or (chunk.rrf_score or 0.0) > (prev.rrf_score or 0.0):   # L139
ranked = sorted(best.values(), key=lambda c: c.rrf_score or 0.0, reverse=True)  # L142
```

### 2.1 改动①的原因：不是简化，是被动必然

```
HybridRetriever 要并发跑两条腿
        ↓
一个 AsyncSession = 一个连接 + 一个事务 → 不能共用
        ↓
检索器必须自己开两个会话（_safe_search 里 async with AsyncSessionLocal()）
        ↓
所以它压根不接受 session 参数
        ↓
所以 retrieve 节点也没有 session 可传
```

**连带效果**：`chat_service.stream_answer` 里那个 `session` 从此**只服务于写操作**。

### 2.2 改动③：两个召回口径写反不会报错

两者都是 `int`，写反只会静默地"用 5 去召回、再截到 20"，让融合失去素材。**实测关键词腿对 `接口` 能召回 16 条**，若 `recall_top_k` 只有 5，其中至少 11 条根本没机会参与融合。

### 2.3 改动④：一个容易被忽略的行为变化

```python
# 第 5 期（纯向量，成本低，原问题是唯一不依赖模型的保底路径）
for sub_query in [state["query"], *(state["multi_queries"] or [])]:

# 第 6 期（每条子查询本身就是混合检索，但原问题的保底没了）
for sub_query in state["multi_queries"] or []:
```

**代价**：多路由一旦整体跑偏（子查询退化成同义替换），**没有原问题兜底了**。
**收益**：少一次完整的两路并发召回（省 1 次嵌入调用 + 2 次数据库查询）。

---

## 三、第 9 章的拒答口径：`_should_refuse`（L86-115）

```python
def _should_refuse(chunks: list[RetrievedChunk]) -> bool:
    # 场景 A：一条都没召回 → 无任何依据，必然拒答
    if not chunks:
        return True

    top = chunks[0]

    # 场景 B：Top1 仅命中关键词路，缺乏语义佐证 → 保守拒答
    if top.vector_score is None:
        return True

    # 场景 C：向量路命中了，但语义相似度低于阈值 → 不达标，拒答
    return top.vector_score < settings.retrieval_min_score
```

### 3.1 为什么必须从 `score` 换成 `vector_score`（实测）

```
query           score(=rrf)  vector_score      差倍数   阈值=0.6
接口               0.03201844      0.658601     20.6x
B200             0.03278689      0.667720     20.4x
DDR5-4800        0.03278689      0.647560     19.8x

k=60 时 rrf 理论上限（两路都第1）= 2/(k+1) = 0.03278689
实测 'B200' 第1名 = 0.03278689  →  精确等于上限 ✓
```

**`rrf_score` 的上限是死的**：`2/(k+1)`。

```
2/(k+1) < 0.6   ⟺   k + 1 > 3.333   ⟺   k > 2.333
```

**即只要 `k >= 3`，上限就恒小于阈值 → 恒拒答是数学必然，不是偶发。** 当前 `k=60` 时上限只占阈值的 **5.5%**。

### 3.2 实测四场景

```
真实切片 high: v_score=0.6586  src=('vector','keyword')
构造切片 kwonly: v_score=None  v_rank=None  src=('keyword',)   ← _with_keyword 的产物

  场景A 空列表            -> refused=True   拒答
  场景B 仅关键词命中        -> refused=True   拒答
  场景C 向量分低于阈值       -> refused=True   拒答
  场景D 向量分达标          -> refused=False  放行
```

### 3.3 ⚠️ 更正：`vector_score is None` 时是**拒答**，不是放行

**本系列归档曾写过一句错误结论**（已在本节更正）：

> ~~`vector_score is None` 必须**放行**。因为如果按"没有向量分就不达标"拒答，
> 等于把本期新增的关键词能力直接关掉。~~

**错。教程代码与流程图都是拒答。** 而且当初那张 `retrieve` 节点流程图也读反了：

```
Top1 vector_score is None   →  ↓  ┐
Top1 vector_score < 阈值     →  ↓  ├→ 拦截拒答 (REFUSAL_ANSWER)
                                ┘  │
Top1 vector_score >= 阈值    →  放行处理
```

**三条线里的两条都汇进"拦截拒答"**，只有 `>= 阈值` 一条放行。

**修正后的三种情形**：

| `chunks[0].vector_score` | 含义 | 决策 |
| --- | --- | --- |
| 有值 ≥ 0.6 | 向量语义相关 | 放行 |
| 有值 < 0.6 | 向量语义不相关 | 拒答 |
| **`None`** | **向量路压根没召回它，只有关键词路命中** | **拒答（保守）** |

**那关键词腿的价值在哪**（修正后的正确理解）：

| 作用 | 机制 | 实测证据 |
| --- | --- | --- |
| **① 提升正确切片的排名** | 两路都命中 → 融合分翻倍 | `B200` 第 1 名 `rrf = 0.03278689 = 2/(60+1)` |
| **② 把向量路排位靠后的捞上来** | 关键词路名次高即可上浮 | `B200` 第 2 名：向量路**第 6**、关键词路第 2 → 升至第 2 |
| **③ 对字面唯一命中的切片做交叉验证** | 弱向量分 + 高关键词分 = 双重佐证 | `DDR5-4800` 唯一关键词命中恰是向量路第 1 → 融合分翻倍 |

**三条的前提都是 Top1 同时被向量路召回。** 纯关键词命中被拒答，不影响这三条。

**保守拒答的工程理由**：关键词召回**"精确但脆弱"** —— 它找的是**包含这个词的片段**，而不是**回答这个问题的片段**。

---

## 四、第 10 章：一份元信息，两条路径

```
                    _build_retrieval_meta(chunk)      ← 唯一的构造函数
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
    _serialize_citation                _persist_assistant_message
    （实时 SSE 下发给前端）              （落库 AnswerCitation.retrieval_meta）
    L130                               L440
```

### 4.1 六个字段

```python
    return {
        "sources": list(chunk.sources),                    # L82
        "vector_rank": chunk.vector_rank,
        "vector_score": (round(chunk.vector_score, 4) if chunk.vector_score is not None else None),   # L85
        "keyword_rank": chunk.keyword_rank,
        "keyword_score": (round(chunk.keyword_score, 4) if chunk.keyword_score is not None else None),
        "rrf_score": (round(chunk.rrf_score, 6) if chunk.rrf_score is not None else None),            # L94
    }
```

**用途**：最终排序依据是融合分，但**"这段为什么被选中"无法从最终分数反推** —— 它可能来自向量路第 1，也可能只来自关键词路第 8。

**实测三种来源**：

```
两路都命中  {"sources":["vector","keyword"],"vector_rank":1,"vector_score":0.6586,
            "keyword_rank":4,"keyword_score":0.0865,"rrf_score":0.032018}
仅向量      {"sources":["vector"],"vector_rank":2,"vector_score":0.6522,
            "keyword_rank":null,"keyword_score":null,"rrf_score":0.016129}
仅关键词    {"sources":["keyword"],"vector_rank":null,"vector_score":null,
            "keyword_rank":1,"keyword_score":0.1884,"rrf_score":0.016393}
```

### 4.2 `score` 字段的语义已经变了（需要传给前端）

| 时期 | `score` 的含义 | 典型值 |
| --- | --- | --- |
| 纯向量（第 4~5 期） | 余弦相似度 | 0.65 |
| **混合检索（本期）** | **RRF 融合分** | **0.032** |

**原始相似度现在从 `retrieval_meta.vector_score` 取。** 前端引用卡片若仍把 `score` 标为"相似度"，展示的 `0.0320` 会**数字和标签都不对**。本批次前端未改动。

### 4.3 落库验证

```
ordinal=1 meta={"sources":["vector","keyword"],"rrf_score":0.032018,
                "vector_rank":1,"keyword_rank":4,"vector_score":0.6586,"keyword_score":0.0827}
OK  本次落库 2/2 条有 retrieval_meta

retrieval_meta：非空 5 条 / 历史 NULL 40 条（共 45）
```

历史引用读出来是 `NULL`，**如实表示"当时系统还没有这个能力"** —— 第 4 章设计该列时确定的语义，到这里才真正被用上。

**⚠️ 过程中一次诊断误判（记录备查）**：初次用 `ORDER BY message_id DESC` 查看落库结果，看到全是 `NULL` 一度以为落库失败。**根因是 `message_id` 为 UUID、排序与时间无关**，那条 SQL 取到的是随机的历史行。改为按本次主键精确查询后 `5/5 有 retrieval_meta`。

> **教训**：**UUID 主键不能代替时间排序**，要按时间取最近记录必须用 `created_at`。

---

## 五、⭐ 实测发现：关键词腿在多词查询下几乎全灭

这是本章最有价值的实测产出。用完整聊天链路（含 `route_query` 策略路由）跑 6 个问题，统计 citations 的 `sources` 分布：

| 提问 | `route` | 改写后 query | `sources` 分布 | Top1 `v_score` |
| --- | --- | --- | --- | --- |
| `接口` | original | `接口` | **vector+keyword ×5** | 0.6586 |
| `接口怎么认证` | hyde | `接口认证通常采用 API Key…` | **vector ×5** | 0.7433 |
| `B200 的散热` | original | `B200 的散热` | **vector+keyword ×1**、vector ×4 | 0.6183 |
| `DDR5-4800` | original | `DDR5-4800` | **vector+keyword ×1**、vector ×4 | 0.6476 |
| `Java 学习路线` | hyde | `Java 学习路线应从基础语法入…` | **vector ×5** | 0.8382 |
| `上传 报错` | rewrite | `上传时出现报错` | **vector ×5** | 0.5807 |

### 5.1 规律与根因

```
单关键词查询（'接口'、'B200'）          → 关键词腿满分命中
多词自然语言查询（中文分词后多于一个词）  → 关键词腿几乎全灭（0/5）
```

`plainto_tsquery` 按 **AND** 拼接：

```
'接口'          → '接口'                  → 命中 16 条
'接口怎么认证'    → '接口' & '怎么' & '认证'  → 命中 0 条
'上传 报错'      → '上传' & '报错'          → 命中 0 条
```

### 5.2 ⚠️ 与策略路由的叠加效应

**两个 `route=hyde` 的样本关键词腿完全失效**：

```
HyDE 改写把查询扩展成一段假设答案 → 分词后词项更多
        ↓
AND 语义下"全部命中"的门槛更高
        ↓
关键词腿更容易归零
```

**第 5 期引入的查询优化策略，在本期的 AND 语义下会进一步削弱关键词腿。**

### 5.3 影响与建议

**真实用户提问几乎都是多词自然语言 → 关键词腿在生产流量下大概率不生效 → 混合检索实际退化成纯向量检索。**

本期投入的迁移 / ORM 契约 / `keyword_search` / `KeywordRetriever` / RRF 融合，**目前只在用户直接搜单个关键词时才真正兑现**。

**建议**：先用本批次已落库的 `retrieval_meta` **统计线上 `sources` 分布，用数据确认占比**，再决定是否改 OR 语义。**不要凭感觉改。**

---

## 六、自测题五道（含完整参考答案）

### Q1（量纲题）为什么 `score` 用于阈值判定会恒拒答

**题目**：

```
query           score(=rrf)  vector_score
接口               0.03201844      0.658601
B200             0.03278689      0.667720
阈值 retrieval_min_score = 0.6
k = 60
```

1. 为什么 `score` 用于阈值判定会**恒拒答**？
2. 这个上限 `2/(k+1)` 是**死的** —— 说明为什么"恒拒答"是数学必然而非偶发。

---

**参考答案**：

**第 1 问：先算出 `rrf_score` 的理论上限。**

`rrf_score = Σ 1/(k + rank_i)`，两路各贡献一项。要让和最大，两路的 rank 都要取最小值 1：

```
上限 = 1/(k+1) + 1/(k+1) = 2/(k+1)
```

代入 `k=60`：

```
上限 = 2/61 = 0.03278689
```

**实测印证**：`B200` 查询的第 1 名（向量路第 1、关键词路第 1）`rrf_score = 0.03278689` —— **精确等于这个理论上限**，说明公式推导无误。

**与阈值比较**：

```
0.03278689 < 0.6
```

**而且这不是"这次运气不好"，而是任何结果都逃不掉** —— 因为 `0.03278689` 是**所有可能 rrf_score 的天花板**。既然天花板都低于阈值，那么 `chunks[0].score < 0.6` **恒成立** → **恒拒答**。

**第 2 问：为什么是数学必然。**

把不等式解出来：

```
2/(k+1) < 0.6
⟺ 2 < 0.6(k+1)
⟺ k + 1 > 2/0.6 = 3.3333
⟺ k > 2.3333
```

**即只要 `k >= 3`，`rrf_score` 上限就恒小于 0.6。** 实测不同 `k`：

```
k=1     上限 1.00000000   可超过 0.6
k=2     上限 0.66666667   可超过 0.6
k=3     上限 0.50000000   超不过 → 拒答
k=10    上限 0.18181818   超不过 → 拒答
k=60    上限 0.03278689   超不过 → 拒答（当前值，仅为阈值的 5.5%）
k=1000  上限 0.00199800   超不过 → 拒答
```

**关键洞察**：`k` 是**平滑常数**，设计上就取较大的值（默认 60）来压平名次差异。**正因为 `k` 大，`rrf_score` 的量级必然很小** —— 所以它**在数学上不可能**与"按 [0,1] 标定的余弦相似度阈值"比较。

> **结论**：`score` 是给**排序**用的（只需要相对大小），`vector_score` 才是给**阈值**用的（需要绝对量级）。**两个字段的用途不同，所以不能互相替代** —— 这与第 6 章契约里那句"`score` = 本次排序依据"完全一致。

---

### Q2（设计题）`None` guard 的性质

**题目**：

1. 如果删掉 `if top.vector_score is None: return True`，直接执行 `return top.vector_score < settings.retrieval_min_score`，会发生什么？
2. "必须处理 `None`"和"`None` 时放行还是拒答"，分别属于什么性质的问题？

---

**参考答案**：

**第 1 问：会抛 `TypeError`。**

Python 3 **禁止** `None` 与数值比较。实测复现（用 `_with_keyword` 产出的真实切片，其 `vector_score=None`）：

```
TypeError: '<' not supported between instances of 'NoneType' and 'float'
```

**注意这不是"结果不对"，而是"直接崩"** —— 异常会一路冒泡到 `stream_answer`，用户看到的是错误事件，**整个问答直接失败**。

**第 2 问：两件事的性质完全不同。**

| 事项 | 性质 | 能不能选 |
| --- | --- | --- |
| **必须有一个 `None` 分支** | **语言约束**（Python 3 的硬性规则） | **不能选 —— 不写就崩** |
| **该分支返回放行还是拒答** | **业务选择** | 可以选（教程选保守拒答） |

**这个区分为什么重要**：它解释了为什么这个 `None` 分支**无论如何都要写**。

```
❌ 错误的理解："如果业务上想放行，那就不用写这个分支"
✅ 正确的理解："分支必须写；写完后里面是 return True 还是 return False，才是业务决策"
```

**我当初就是在这里出错的** —— 心里想着"业务上纯关键词命中应该放行"，就顺带以为"代码没处理这个分支、需要我改成放行"。实际上代码**处理了**，只是 `return True`（拒答），与我预设的相反。**我把"该不该处理"和"怎么处理"混成了一件事。**

> **可推广的原则**：任何"可选分数"进入阈值判断前，**必须**有一个显式的 `None` 分支。
> 这个分支的**存在**是语言约束，分支的**内容**是业务决策。
> 而且 `None` 分支的选择应当有明确语义：教程选"保守拒答"，理由是**关键词召回的"精确但脆弱"** —— 它找的是包含这个词的片段，而不是回答这个问题的片段。

---

### Q3（精度题）4 位 vs 6 位

**题目**：把下面 6 个实测值按 4 位小数舍入，写出结果，并回答舍入后**有哪几对会变成完全相同的值**、会带来什么后果。

```
0.0320184412  （两路都命中，v=1, k=4）
0.0315449650  （两路都命中，v=6, k=1）
0.0312805470  （两路都命中，v=6, k=2）
0.0310245310  （两路都命中，v=3, k=6）
0.0310096150  （两路都命中，v=4, k=5）
0.0310096150  （两路都命中，v=5, k=4）
```

---

**参考答案（实算结果）**：

| # | 原始值 | **4 位** | 6 位 | 8 位 |
| --- | ---: | ---: | ---: | ---: |
| 1 (v=1, k=4) | 0.0320184412 | **0.0320** | 0.032018 | 0.03201844 |
| 2 (v=6, k=1) | 0.0315449650 | **0.0315** | 0.031545 | 0.03154497 |
| 3 (v=6, k=2) | 0.0312805470 | **0.0313** | 0.031281 | 0.03128055 |
| 4 (v=3, k=6) | 0.0310245310 | **0.0310** | 0.031025 | 0.03102453 |
| 5 (v=4, k=5) | 0.0310096150 | **0.0310** | 0.031010 | 0.03100962 |
| 6 (v=5, k=4) | 0.0310096150 | **0.0310** | 0.031010 | 0.03100962 |

**舍入后的并列情况（实测统计）**：

```
保留 4 位 -> 6 个值只剩 4 个唯一值，出现 1 组三方并列:
             [4 (v=3,k=6), 5 (v=4,k=5), 6 (v=5,k=4)] 全部 = 0.0310

保留 6 位 -> 6 个值有 5 个唯一值，只剩 1 组两方并列:
             [5 (v=4,k=5), 6 (v=5,k=4)] = 0.031010   ← 这两个原始值本就相同（合法的真并列）

保留 8 位 -> 同上
```

**⚠️ 关键结论**：

**① 4 位把 3 条不同的切片压成了同一个数**。

第 4 条 `(v=3, k=6)` 与第 5 条 `(v=4, k=5)` 的**真实融合分并不相同**：

```
0.0310245310  vs  0.0310096150
差 = 0.0000149160
```

但舍入到 4 位后**双双变成 `0.0310`**，与第 6 条一起形成**三方并列**。

**② 后果：排序信息永久丢失。**

这不仅是"显示不出来"的问题 —— 元信息一旦**按 4 位落库**，就再也无法还原真实顺序：

```
用户看到：第 3、4、5 条引用 "融合分都是 0.0310"
线上排查："为什么这条排第 3 而不是第 5？"  →  答不出来
A/B 对比："改 OR 语义后排序有没有变好？"      →  无从判断（精度不够，差异看不见）
```

**③ 而 `vector_score` 保留 4 位是够的** —— 因为它的量级不同：

```
vector_score 典型值 0.65，相邻名次差约 0.01 量级
4 位小数的最小刻度 0.0001  →  刻度是差值的 1%
```

对比融合分：

```
rrf_score 典型值 0.032，相邻名次差约 0.0005 量级（本例最小 0.0000149）
4 位小数的最小刻度 0.0001  →  刻度与差值同量级，甚至更大
```

**⚠️ 这里有一个反直觉的现象值得注意**：`DDR5-4800` 那种"单路命中"的切片 `rrf_score = 1/(60+1) = 0.01639344`，**它的值比上表的 0.031 系列还小**。所以"两路命中"与"单路命中"的融合分跨度是 `0.0164 ~ 0.0328`，**整体只有 2 倍** —— 4 位精度（刻度 0.0001）相对于这整个跨度也只占 0.6%。**当分值被压缩到这么窄的区间时，位数不足会立刻造成可见的并列。**

> **核心原则：精度必须匹配量级。**
> 不是"6 位比 4 位更精确所以更好"，而是**"4 位对于融合分的量级来说太粗"**。
> 判断方法：比较"最小刻度"与"需要区分的差值" —— 如果同量级，就必须加位数。
> **而"需要区分的最小差值"来自业务**：这里它就是"相邻名次"的差，因为融合分的**唯一用途就是排序**。

---

### Q4（架构题）为什么两处必须共用同一个构造函数

**题目**：

1. 为什么 `_serialize_citation` 与 `_persist_assistant_message` 必须共用 `_build_retrieval_meta`？各写一份会怎样？
2. 追问：保留 `None` 字段的键，好处是什么？而 `sources` 为什么不可能为 `None`？

---

**参考答案**：

**第 1 问：因为这是"同一份数据的两个出口"，必须单一来源。**

```
                    _build_retrieval_meta(chunk)      ← 唯一来源
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
    _serialize_citation              _persist_assistant_message
    （实时 SSE 下发）                  （落库 JSONB）
```

**各写一份的后果**：两条路径会**独立演化**。典型场景：

```
第 1 周：SSE 加了 "vector_rank"，落库忘了 → 实时能看、刷新历史看不到
第 3 周：有人优化 SSE 的精度为 4 位，落库还是 6 位 → 同一份数据两个精度
第 5 周：落库加了 "rerank_score"，SSE 没有 → 历史比实时信息更全
```

**最糟的不是"不一致"本身，而是它无法被测出来** —— 单测通常只覆盖其中一条路径。用户会报"刷新一下字段就少了"，而开发在本地复现不了（因为本地恰好两条路径都走过）。

**这与第 5 期 `query_route` 载荷的处理原则完全相同**：那里也是 SSE 事件与 `extra_metadata` 落库共用 `_build_query_route_payload`。**同一个原则在本期第二次应用，说明它是通用约束，不是巧合。**

**第 2 问：保留 `None` 键 —— 让"键不存在"与"值为空"不再混淆。**

实测输出：

```
仅向量      {"sources":["vector"], ..., "keyword_rank":null, "keyword_score":null, ...}
仅关键词    {"sources":["keyword"], ..., "vector_rank":null, "vector_score":null, ...}
```

`"keyword_rank": null` **明确告诉前端**："这条没被关键词路召回"。若改成**省略键**，前端代码必须写成：

```typescript
// 键存在但为 null —— 一个判断
if (meta.keyword_rank !== null) { ... }

// 键可能缺失 —— 两个判断，且容易漏掉第二个
if ('keyword_rank' in meta && meta.keyword_rank !== null) { ... }
```

**`null` 与"缺失"在 TypeScript 类型系统里含义完全不同**：

```typescript
{ keyword_rank: number | null }        // 键一定有，值可能为 null
{ keyword_rank?: number | null }       // 键本身都可能不存在
```

前者让 TS 能确定"这个键存在，只是没有值"，消费方**无需额外判断键是否存在**。**这与第 5 期 `query_route` 载荷"始终携带 4 个可选字段（值为 None 也保留）"是同一个理由。**

**`sources` 为什么不可能为 `None`**：它是**契约的不变式**。

`RetrievedChunk` 的产生路径只有三条：

```
_with_vector(hit, ...)        → sources = ("vector",)
_with_keyword(hit, ...)       → sources = ("keyword",)
_merge_keyword_into(...)      → sources = ("vector", "keyword")
```

**三个函数无一例外都赋了至少一个元素**，因此 `sources` 永远非空。这是**结构性保证**，不依赖运行时的防御性判断 —— 也就是说，它比"约定"更强，是"代码写完就不可能违反"。

---

### Q5（数据题）为什么单词命中、多词全灭

**题目**：

| 提问 | 向量路 | 关键词路 | `sources` 分布 |
| --- | --- | --- | --- |
| `接口` | 命中 | 命中 16 条 | `vector+keyword ×5` |
| `接口怎么认证`（HyDE 改写后） | 命中 | **0 条** | `vector ×5` |
| `上传 报错`（rewrite 成 `上传时出现报错`） | 命中 | **0 条** | `vector ×5` |

1. 为什么**单关键词**能满分命中，而**多词**几乎全灭？
2. 为什么 **HyDE 改写会让这个问题更严重**？
3. 如何用 `retrieval_meta` 先确认这个问题的真实影响，再决定改不改？

---

**参考答案**：

**第 1 问：`plainto_tsquery` 是 AND 语义，命中门槛随词数指数上升。**

`plainto_tsquery('chinese_zh', q)` 把查询切词后**用 `&` 拼接**，要求切片**同时包含全部词项**：

```
'接口'          → '接口'                    → 1 个词项，命中 16 条
'上传 报错'      → '上传' & '报错'            → 2 个词项，命中 0 条
'接口怎么认证'    → '接口' & '怎么' & '认证'    → 3 个词项，命中 0 条
```

**为什么门槛上升这么快** —— 因为要求的是**共现（co-occurrence）**：

假设各词项在语料中的命中率分别为 `p1, p2, ..., pn`，若近似独立，则"同一切片同时包含全部"的概率约为 `p1 × p2 × ... × pn`。**词项越多，乘积衰减越快。**

而**切片本身很小**（259 个切片、平均几百字），一段文字里**同时出现 3 个特定词**的概率天然很低。

**更麻烦的是中文分词的副作用**：`'接口怎么认证'` 切出的 `'怎么'` 是**口语虚词**，很多相关切片根本不含它 —— **只要一个词项不命中，整个 AND 就为假**。这解释了为什么"加一个无关虚词就把命中数打回 0"。

**实测印证**：`'B200 的散热'` 只命中 **1** 条，而 `'B200'` 单独查能命中 **7** 条 —— **加了"的散热"三个字，命中数从 7 掉到 1**。

**第 2 问：HyDE 改写把查询"变长"，而变长在 AND 语义下等于"门槛更高"。**

HyDE（Hypothetical Document Embeddings）的做法是：**先让模型生成一段"假设答案"，再用这段假设答案去检索**。

```
用户提问:   '接口怎么认证'                     （3 个词项）
HyDE 产出:  '接口认证通常采用 API Key 方式…'    （十几个词项）
```

**同一批实测数据里，两个 `route=hyde` 的样本关键词腿完全失效**：

| 提问 | 改写后 query | `sources` |
| --- | --- | --- |
| `接口怎么认证` | `接口认证通常采用 API Key…` | `vector ×5` |
| `Java 学习路线` | `Java 学习路线应从基础语法入…` | `vector ×5` |

**机制**：

```
HyDE 把查询扩展成一段话 → 词项数量成倍增加
        ↓
AND 语义要求"全部命中" → 共现概率指数衰减
        ↓
关键词腿几乎必然归零
        ↓
混合检索静默退化为纯向量检索
```

**⚠️ 这是一个"两个正确设计组合出错误结果"的典型案例**：

- **HyDE 对向量路是有益的** —— 假设答案与真实文档在语义空间上更接近，向量召回确实变准（实测 `接口怎么认证` 的 Top1 `v_score` 达到 **0.7433**，比 `接口` 的 0.6586 更高）；
- **但同一条改写后的查询喂给关键词路就是灾难** —— 它把一个"适合语义匹配的长文本"当成了"要求全部词项共现的布尔查询"。

**根因**：两条腿**共用同一个查询串**，但两条腿对"查询串该怎么用"的假设完全不同：

| 路 | 对查询的期望 | 查询变长的影响 |
| --- | --- | --- |
| 向量 | 一段**语义完整的文本** | ✅ 有益（语义更聚焦） |
| 关键词 | **少数几个关键**词项 | ❌ 有害（AND 门槛指数上升） |

**这提示一个可能的改进方向**：关键词腿**不应该用改写后的查询**，而应该用**原始问题**（`state["question"]`）—— 因为原始问题通常更短、更接近"关键词"的形态。**这是本期之后可以考虑的优化，本批次未做。**

**第 3 问：用 `retrieval_meta` 做数据驱动的判断（而不是凭感觉改）。**

元信息刚落库，**这正是它的用途**。具体做法：

**① 定义可度量的指标** —— "关键词腿贡献率"：

```sql
-- 含关键词命中的引用占比（分母只算有元信息的新记录）
SELECT
  count(*) FILTER (WHERE retrieval_meta->'sources' ? 'keyword')::float
    / nullif(count(*), 0) AS keyword_hit_ratio
FROM answer_citations
WHERE retrieval_meta IS NOT NULL;
```

**② 按"查询长度"或"路由策略"分组，验证假设**：

```sql
-- 假设：多词查询 / hyde 路由的关键词命中率显著更低
SELECT
  m.metadata->'query_route'->>'route' AS route,
  avg(jsonb_array_length(c.retrieval_meta->'sources')) AS avg_source_cnt
FROM answer_citations c
JOIN messages m ON m.id = c.message_id
WHERE c.retrieval_meta IS NOT NULL
GROUP BY 1;
```

**预期**：`route='hyde'` 的 `avg_source_cnt` 接近 1.0（只有向量路），`route='original'` 明显更高。

**③ 结合"拒答率"看是否已经造成用户可感的影响**：

```sql
SELECT m.metadata->>'refused' AS refused, count(*) 
FROM messages m WHERE m.role='assistant' GROUP BY 1;
```

若"仅向量命中"的查询里拒答率显著偏高，说明关键词腿的失效**已经影响到答案可得性**（而不只是排序质量）。

**④ A/B 验证** —— 改 OR 语义后，用**同一组问题**重跑，对比：

| 指标 | 改前 | 改后 |
| --- | --- | --- |
| 关键词腿命中率 | ? | ? |
| 拒答率 | ? | ? |
| Top1 `v_score` 分布 | ? | ? |

**⚠️ 为什么必须先测再改**：

1. **改 OR 语义有代价** —— 单词召回容易过宽（`'怎么'`、`'的'` 这类虚词会把大量无关切片拉进来），可能**稀释精准度**；
2. **当前问题可能没有想象中严重** —— 从实测看，单关键词查询（用户搜型号 `B200`、`DDR5-4800`）时关键词腿**工作得很好**，而这恰恰是关键词路最有价值的场景（字面精确匹配）；
3. **有替代方案**（如上面提到的"关键词腿改用原始问题"），**成本更低、副作用更小**，值得先验证。

> **方法论**：**先把可观测性做出来（本批次已完成），再让数据决定改不改、以及改哪个方案。**
> 本批次落的 `retrieval_meta` 就是为此准备的 —— 它是"发现问题"和"验证修复"的共同依据。

---

## 七、五题批改汇总

| 题 | 考点 | 结论 |
| --- | --- | --- |
| **Q1** | RRF 上限 `2/(k+1)` 与恒拒答 | 需掌握推导：`k > 2.333` 即恒拒答；**当前 k=60 时上限仅为阈值的 5.5%** |
| **Q2** | `None` guard 的两重性质 | **语言约束（必须有分支）** vs **业务选择（分支返回什么）** —— 这两者必须分开 |
| **Q3** | 精度必须匹配量级 | 4 位下 **3 条不同切片被压成同一个数**（三方并列）；真实差仅 `0.0000149` |
| **Q4** | 单一来源 + `None` 键语义 | 两出口必须共用构造函数；`sources` 非空是**结构性保证**（三个工具函数无一例外） |
| **Q5** | AND 语义与 HyDE 叠加 | 两条腿**共用同一查询串**，但对"查询该怎么用"的假设相反 → HyDE 益向量、害关键词 |

**最值得记住的三条**：

1. **`score` 给排序、`vector_score` 给阈值** —— `rrf` 上限 `2/(k+1)` 在数学上不可能达到 0.6；
2. **`None` 分支必须写（语言约束），返回什么才是业务选择** —— 这是本系列第三次误判的根源；
3. **精度要匹配量级** —— 融合分跨度只有 2 倍（0.0164~0.0328），4 位不够用。

---

## 八、当前状态

```
✅ 迁移（镜像 + content_tsv + GIN + retrieval_meta 列）          ← 第 1-3 步
✅ ORM 模型同步（含 Computed 生成列契约）                        ← 第 4 步
✅ chunk_repo.keyword_search（中文全文检索 + ts_rank）            ← 第 5 步
✅ RetrievedChunk 契约统一（7 必填 + 6 调试字段）                  ← 第 6 步
✅ KeywordRetriever（与向量路严格对称）                           ← 第 7 步
✅ HybridRetriever（两路并发 + RRF 融合 + 单路降级）               ← 第 8 步
✅ config：rrf_k=60 / retrieval_recall_top_k=20
✅ retrieve 节点接入 + 拒答口径改 vector_score（None 时保守拒答）   ← 第 9 步
✅ 召回元信息落库 + SSE 载荷                                      ← 第 10 步
────────────────────────────────────────────────────────────
⬜ 前端：引用卡片展示 retrieval_meta（score 已变成融合分，语义需调整）
⬜ 关键词腿 AND 语义导致多词查询召回失效（建议先用数据确认占比）
⬜ 关键词腿可考虑改用原始问题而非改写后查询（成本更低，待验证）
⬜ 宽兜底的告警层（连续 N 次降级 → 主动告警）
⬜ 第 4~8 步的待补练习
```

---

## 九、一句话总结

> 第 9-10 章是**第 8 章那个正确决定的连锁后果**：
> `HybridRetriever` 把 `score` 改写成 `rrf_score` 之后，**`rrf_score` 的上限 `2/(k+1)` 在 `k=60` 时只有 0.0328，
> 仅为拒答阈值 0.6 的 5.5%，且 `k > 2.333` 即恒拒答（数学必然，非偶发）**，
> 因此 `retrieve` 节点的判定必须改用 `vector_score`；同时因为混合检索要并发两条腿、
> 会话不能共用，`retrieve` 签名也随之去掉了 `session`；
> 拒答的 `None` 分支**无论如何都必须写**——**"必须有分支"是语言约束（不写抛 `TypeError`），
> "分支返回拒答"才是业务选择**（关键词召回精确但脆弱，找不到"回答这个问题的片段"）；
> 第 10 章把调试元信息同时接进 SSE 与落库两条出口，**共用同一个构造函数**以避免格式各自演化，
> 并刻意保留 `None` 字段的键（让 TS 能区分"键存在但无值"与"键不存在"），
> 相似度取 4 位、融合分取 6 位——**因为融合分跨度只有 2 倍（0.0164~0.0328），
> 4 位会把 3 条真实不同的切片压成同一个数**（实测差仅 0.0000149）；
> 最后，元信息一落库**立刻暴露了一个真实缺口**：`plainto_tsquery` 的 AND 语义让
> **多词自然语言查询的关键词腿几乎全灭**（`'接口'` 5/5 vs `'接口怎么认证'` 0/5），
> 且 **HyDE 改写会加剧它**——两条腿共用同一查询串，但向量腿欢迎长文本、关键词腿的 AND 门槛随词数指数上升；
> **建议先用刚落库的 `retrieval_meta` 统计线上 `sources` 分布，用数据决定改不改、以及改哪个方案。**

---

## 十、关联文件索引

| 文件 | 关键位置 | 说明 |
| --- | --- | --- |
| `app/workflows/nodes/retrieve.py` | L18 | `retrieve(state)` 签名（无 session） |
| `app/workflows/nodes/retrieve.py` | L35 | `HybridRetriever()` 实例化 |
| `app/workflows/nodes/retrieve.py` | L40-41 | `recall_top_k` / `final_top_k` 两个口径 |
| `app/workflows/nodes/retrieve.py` | L44-61 | `multi_query` 分支 + `_merge_chunks` |
| `app/workflows/nodes/retrieve.py` | L53 | 子查询遍历（不再并入原始问题） |
| `app/workflows/nodes/retrieve.py` | L64-68 | 单路 `else` 分支 |
| `app/workflows/nodes/retrieve.py` | L71 | `refused = _should_refuse(chunks)` |
| `app/workflows/nodes/retrieve.py` | **L86-115** | **`_should_refuse`（L104 空列表 / L111 None / L115 阈值）** |
| `app/workflows/nodes/retrieve.py` | L118-143 | `_merge_chunks`（改按 `rrf_score` 排序） |
| `app/workflows/nodes/retrieve.py` | L139 / L142 | `rrf_score or 0.0` 兜底比较与排序 |
| `app/services/chat_service.py` | L59-98 | `_build_retrieval_meta`（六字段） |
| `app/services/chat_service.py` | L82 | `"sources": list(chunk.sources)`（tuple → list） |
| `app/services/chat_service.py` | L85 / L94 | `vector_score` 4 位 / `rrf_score` 6 位 |
| `app/services/chat_service.py` | L100-131 | `_serialize_citation`（L130 加 `retrieval_meta`） |
| `app/services/chat_service.py` | L306 | `await retrieve(state)`（去掉 session） |
| `app/services/chat_service.py` | L428-441 | `AnswerCitation(..., retrieval_meta=...)` 落库 |
| `app/services/chat_service.py` | L440 | `retrieval_meta=_build_retrieval_meta(chunk)` |
| `app/db/models.py` | L827-831 | `AnswerCitation.retrieval_meta`（JSONB / nullable） |
| `alembic/versions/8c44f95568ad_...py` | L99 | 该列的迁移出处 |
| `app/retrieval/hybrid_retriever.py` | L42-51 | `HybridRetriever` 不接收 session 的理由 |
| `app/retrieval/hybrid_retriever.py` | L157-229 | `rrf_fuse`（上限 `2/(k+1)` 的来源） |
| `app/retrieval/hybrid_retriever.py` | L268-300 | `_with_keyword`（产出 `vector_score=None` 的切片） |
| `app/core/config.py` | L143 | `retrieval_min_score=0.6`（阈值） |
| `app/core/config.py` | L161 / L166 | `rrf_k=60` / `retrieval_recall_top_k=20` |
