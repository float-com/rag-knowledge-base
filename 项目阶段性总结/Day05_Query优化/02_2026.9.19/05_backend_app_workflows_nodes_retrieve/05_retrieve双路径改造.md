# 05_retrieve 改造为双路径

> 章节：Day05_Query 优化 · 第 3 章 后端实现 · 第 5 节
> 对应文件：`backend/app/workflows/nodes/retrieve.py`（整段替换，59 → 99 行）
> 记录日期：2026.09.19

---

## 一、本节要解决什么

`route_query` 产出了 `multi_queries`，但 `retrieve` 只认单个 `query`。本节把它改成双路径：

```
multi_query 路径  →  N 路召回 → 合并去重 → 全局 Top-K
其他路径          →  单路召回（行为完全不变）
```

**关键设计**：两条路径产出的 `chunks` 结构**完全一致**，因此下游 `stream_generate` **零改动**。

---

## 二、双路径结构（L36-49）

```python
if state.get("route") == "multi_query" and state.get("multi_queries"):
    bundles: list[list[RetrievedChunk]] = []
    for sub_query in [state["query"], *(state["multi_queries"] or [])]:
        bundles.append(await retriever.search(sub_query, top_k=top_k))
    chunks = _merge_chunks(bundles, top_k)
else:
    chunks = await retriever.search(state["query"], top_k=top_k)
```

### 两个条件缺一不可

```
state.get("route") == "multi_query"   ← 策略确实选中了多路
and state.get("multi_queries")        ← 且子查询确实存在
```

**只判断前者会怎样？**

```python
for sub_query in None:     # TypeError: 'NoneType' object is not iterable
```

**更关键的是：两个条件都判断后，不会崩，而是走 `else` 分支**：

```
→ 自动退化成单路检索 = 退回 baseline
→ 这是一条【静默的安全降级路径】（与第 2 章 fail-safe 同方向）
→ 用户完全无感
```

> 所以这个 `and` 不只是防御，**它本身就是一条降级路径**。

---

## 三、`_merge_chunks` 的三个决策（本节真正的考点）

```python
def _merge_chunks(bundles: list[list[RetrievedChunk]], top_k: int) -> list[RetrievedChunk]:
    best: dict[str, RetrievedChunk] = {}
    for bundle in bundles:
        for chunk in bundle:
            key = str(chunk.chunk_id)
            prev = best.get(key)
            if prev is None or chunk.score > prev.score:
                best[key] = chunk
    ranked = sorted(best.values(), key=lambda c: c.score, reverse=True)
    return ranked[:top_k]
```

### 决策 1：去重键用 `chunk_id`（L92）

**为什么不用其他候选？**

| 候选键 | 问题 |
| --- | --- |
| `content` | 长字符串作键，内存与比较成本高 |
| `(document_id, chunk_index)` | 也能唯一，但要拼两个字段，没必要 |
| **`chunk_id`** | **数据库主键，唯一、稳定、必然存在** ✓ |

**为什么转 `str()`**：`chunk_id` 是 `UUID` 对象，虽可哈希也可作键，但转字符串更直观（日志可直接读），也避免不同 UUID 表示形式的哈希/相等性问题。

### 决策 2：保留最高分（L95）

```python
if prev is None or chunk.score > prev.score:
    best[key] = chunk
```

**为什么是最高分，而不是平均分或"最后一次"？**

```
保留最高分 = 保留「最有利的证据」
  某个子查询与这个 chunk 高度匹配（0.9）
  → 说明从【那个角度】看它确实是相关的 → 这个信号有价值

平均分     → 把它稀释成 0.7，丢掉"某一路强匹配"的信息
最后一次   → 分数取决于【遍历顺序】→ 让"哪条子查询排在后面"决定结果
            → 引入无意义的随机性（最不合理）
```

**单元测试实证**（构造 A 在两路出现，分数 0.5 / 0.9）：

```
输出: [1] A score=0.9   ← 保留最高分，不是 0.5、也不是平均 0.7
      [2] B score=0.8
      [3] C score=0.7
```

### 决策 3：Top-K 语义（L45 + L99）

```python
bundles.append(await retriever.search(sub_query, top_k=top_k))   # 每路各取 top_k
...
return ranked[:top_k]                                            # 合并后再截断
```

**这解决了第 1 节留下的第三问（Top-K 语义）：**

```
✓ 采用：每路 top_k 候选 → 去重 → 全局排序 → 取前 top_k
   收益：输出条数【恒定】为 top_k
        → prompt 的 Token 预算可预测（不随路数波动）
        实测：单路 5 条、多路（N=3）也是 5 条

✗ 另一种：每路 top_k 直接用（不去重不截断）
   风险：N=3 × top_k=5 → 最多 15 条 chunk
        → 全塞进 prompt 的【参考资料】
        → 而 prompt 还要装 5 轮历史 + 问题 → 上下文窗口有限
        → 可能直接撑爆
```

**去重与截断的分工**：

```
去重 → 解决「冗余」（同一片段重复出现，浪费 Token 且抬高无关片段相似度）
截断 → 解决「预算」（保证输出条数稳定）
      ↑ 后者才是主要目的
```

> 这也是 `_merge_chunks` 必须接收 `top_k` 参数的原因——**它的职责就是"把 N 路收敛回 K 条"**。

---

## 四、熔断判定为何一行都不用改（L55）

```python
refused = not chunks or chunks[0].score < settings.retrieval_min_score
```

**原因是排序保证**：

```
_merge_chunks 返回的列表已按分数降序（L98 的 sorted(..., reverse=True)）
→ chunks[0] 必然是【全局最高分】—— 无论它来自哪一路
→ 熔断逻辑只关心"最高分是多少"，不关心"这些 chunks 从哪来"
```

**对比两条路径的排序契约**：

```
单路:  VectorRetriever.search() 内部按 distance 升序 → score 降序
多路:  _merge_chunks() 显式 sorted(..., reverse=True)
       ↑ 主动对齐单路路径的排序契约
```

> **不是"两者没有联系"，而是多路路径刻意对齐了单路的排序契约。**
> 这是设计的价值：**把差异关在节点内部，对外保持同一个形状。**

**实测验证**：多路 Top-1 = 0.5014，与排序后第一项一致 ✓

---

## 五、⚠️ 两处需要决策/知情的地方

### 5.1 我把原始问题也并入了召回列表（与教程不同）

**教程实现**：

```python
for sub_query in state["multi_queries"] or []:      # 只有子查询
```

**本实现**：

```python
for sub_query in [state["query"], *(state["multi_queries"] or [])]:   # 原问题 + 子查询
```

**为什么改？** 若 multi_query 分支从不调用 `state["query"]`，第 1 节那条设计就自相矛盾：

```
第 1 节保留 state["query"]（原问题）的理由是：
  "它本身仍是有效的检索输入，也是兜底"
  "不依赖 LLM 生成质量的保底路径"

若 multi_query 分支从不调用它
→ 那份"保底"形同虚设
→ 一旦子查询生成质量不佳（退化成同义替换），多路会整体跑偏
→ 而原问题本可以兜住
```

**实测效果（诚实记录）**：

```
Top-1: 0.5014（含原问题）  与不含原问题完全相同
→ 本次原问题那一路【没有成为赢家】
→ 收益是"纯保险"，不是"本次更好"

代价：多一次 embedding 调用（0.6s → 0.8s，约 +200ms）
```

**取舍表**：

| 方案 | 优点 | 代价 |
| --- | --- | --- |
| 只遍历子查询（教程） | 省一次 embedding | 子查询质量差时无兜底 |
| 含原问题（本实现） | 有一条不依赖模型的保底路径 | 多一次 embedding |

> 这是**设计决策**，可改回教程版本。保留的理由是：让第 1 节"`query` 在 multi_query 下仍是原问题"的设计真正被消费。

### 5.2 实测：两种路径都触发拒答——**但这是正确的**

```
A 单路:  Top-1=0.4798  refused=True
B 多路:  Top-1=0.5014  refused=True   ← 阈值 0.6
```

**原因**：知识库里**根本没有讲 HNSW / IVFFlat 的内容**。召回的都是"Java 学习路线""核心概念"这类文档。

```
→ 系统正确判断"知识库无可靠依据"
→ 这是【正确的产品行为】，不是 bug
→ 反过来验证了第 9 章的拒答机制在真实场景下工作正常
   （向量检索对完全无关的问题能通过低分识别出来，不会硬答）
```

**但它也意味着这个用例无法展示 multi_query 的"答案变好"效果**——库里就是没有，怎么召回都是拒答。

**要真正验证 multi_query 的收益，测试问题必须满足两个条件：**

```
① 知识库里确实有相关内容（否则无论怎么召回都拒答）
② 问题本身"多角度/多实体"（否则路由不会判为 multi_query）

合适的候选（基于本项目语料）：
  「对比一下 MySQL 索引和 Redis 缓存的适用场景」   ← 库里有 MySQL、Redis
  「课程目标和考核方式分别是什么」                 ← JavaEE PDF 里有
  「Java 集合和并发分别要掌握什么」                ← 学习路线里有
```

**建议对比三项指标**：

```
① Top-1 分数是否提升
② 召回片段多样性（多路新增几条）
③ refused 是否从 True 变 False   ← 最有说服力：
    若某问题单路拒答、多路能答，就是 multi_query 收益的直接证据
```

---

## 六、自测题与批改记录

| # | 题目 | 作答 | 判定 |
| --- | --- | --- | --- |
| 1 | 为何 `if` 必须同时判断 route 与 multi_queries 非空？只判前者会怎样？ | 「只判前者，None 情况下直接抛异常，整条链路断了」 | ✅ 需点明退化成什么 |
| 2 | `_merge_chunks` 为何保留最高分而非平均分/最后一次？ | 「最高分是语义强化过的；平均分和最后一次会稀释与问题的相关性」 | ✅ 措辞可更准 |
| 3 | Top-K 为何选"每路 top_k → 合并去重 → 全局取 top_k"？后者风险？ | 「可能导致同一个 chunk 出现多次，浪费资源」 | ⚠️ 漏主要风险 |
| 4 | 熔断判定为何一行都不用改？ | 「因为两者没有必然的联系」 | ⚠️ 太笼统 |
| 5 | 实测两种路径都拒答，是 bug 吗？该如何选测试问题？ | 「验证了拒答机制正常；需要有更多实体的文档」 | ✅ |

### 逐题批改

**第 1 题 —— 正确，需点明退化成什么**

```
崩点:  for sub_query in None  → TypeError
但两条件都判断后 → 走 else 分支 = 单路检索（静默安全降级，不是崩溃）
```

**第 2 题 —— 正确**。"最后一次"最不合理之处：**分数取决于遍历顺序 → 引入无意义随机性**。

**第 3 题 —— 说到了次要风险，漏了主要的**

```
主要风险：Token 预算不可预测（N=3 × 5 = 最多 15 条 → 可能撑爆上下文）
次要风险：冗余重复（作答提到的）
分工：去重管「冗余」，截断管「预算」
```

**第 4 题 —— 太笼统**

真正原因是**排序保证**：`_merge_chunks` 已降序 → `chunks[0]` 必为全局最高分 → 熔断只关心最高分，不关心来源。

**不是"没有联系"，而是多路路径主动对齐了单路的排序契约。**

**第 5 题 —— 正确**。拒答是正确的产品行为；验证需选"库里有内容 + 多实体"的问题。

---

## 七、本节一条结论

**双路径设计的关键不是"多写一个分支"，而是"让两条路径对下游完全等价"：**

```
输入不同：multi_query → N 路；其他 → 1 路
输出相同：list[RetrievedChunk]，都按 score 降序，长度都是 top_k
          ↑
    所以 stream_generate 零改动
    所以熔断判定零改动
```

> **这是第 9 章契约稳定性思想的又一次应用——把差异关在节点内部，对外保持同一个形状。**

---

## 八、验证记录

### `_merge_chunks` 单元验证（构造重复/乱序数据，不依赖数据库）

```
输入: 3 路共 5 条（含 A 重复），top_k=3
输出: [1] A score=0.9  [2] B score=0.8  [3] C score=0.7

断言1 去重后 A 只出现一次                     ✅
断言2 A 保留最高分 0.90（而非 0.5 或均值 0.7）  ✅
断言3 按分数降序                              ✅
断言4 截断到 top_k                            ✅
断言5 输入列表未被修改（无副作用）              ✅
边界：空输入 / 全空路 → []                     ✅
```

### 端到端（真实向量检索，617 chunks）

```
问题: 对比一下 HNSW 和 IVFFlat 的优缺点
路由: multi_query（三条子查询角度各异）

A 单路         : 条数=5 Top-1=0.4798 refused=True 耗时=0.6s
B 多路(含原问题): 条数=5 Top-1=0.5014 refused=True 耗时=0.8s

A 分数: 0.4798, 0.4724, 0.4600, 0.4426, 0.4375
B 分数: 0.5014, 0.5010, 0.4999, 0.4868, 0.4822
两路结果 chunk 重合: 2/5   多路新增: 3
```

**多路召回的两个效果都验证到了**：

```
① Top-1 提升    0.4798 → 0.5014
② 结果多样性    5 条里只有 2 条重合，3 条是新召回的
   → 证实子查询确实从不同角度捞到不同片段，未退化为同义替换
```

**同时确认**：两条路径输出条数都是 5（Top-K 语义生效）；`refused=True` 属正确拒答（库中无相关文档）。

---

## 九、一句话总结

> 本节把 `retrieve` 改为双路径：`multi_query` 时对「原始问题 + N 条子查询」逐路召回，再用 `_merge_chunks` 以 `chunk_id` 为键去重、保留最高分、全局降序后截断回 `top_k`；**"每路 top_k → 去重 → 全局取 top_k"的语义保证输出条数恒定，使 prompt 的 Token 预算可预测**；因合并结果已降序，`chunks[0]` 必为全局最高分，**熔断判定零改动**；双路径对下游完全等价（同结构、同排序、同长度），故生成节点也零改动——**把差异关在节点内部，对外保持同一形状**。实测多路使 Top-1 从 0.4798 提升至 0.5014，且 5 条结果中 3 条为多路新增。

---

## 十、关联文件索引

| 文件 | 关键位置 | 说明 |
| --- | --- | --- |
| `app/workflows/nodes/retrieve.py` | L19 | `retrieve` 节点签名 |
| `app/workflows/nodes/retrieve.py` | L36 | 双路径判定条件（两个条件缺一不可） |
| `app/workflows/nodes/retrieve.py` | L44-45 | 多路召回（含原问题 + 子查询） |
| `app/workflows/nodes/retrieve.py` | L46 | 调用 `_merge_chunks` |
| `app/workflows/nodes/retrieve.py` | L49 | 单路路径（行为不变） |
| `app/workflows/nodes/retrieve.py` | L55 | 熔断判定（零改动） |
| `app/workflows/nodes/retrieve.py` | L70 | `_merge_chunks` 定义 |
| `app/workflows/nodes/retrieve.py` | L92 | 去重键 `str(chunk.chunk_id)` |
| `app/workflows/nodes/retrieve.py` | L95 | 保留最高分 |
| `app/workflows/nodes/retrieve.py` | L98-99 | 全局降序 + 截断 top_k |
| `app/retrieval/vector_retriever.py` | L58 | `search(query, top_k)` 单查询签名（未改动） |
| `app/workflows/rag_state.py` | L51-56 | `route` / `multi_queries` 的来源 |
| `app/core/config.py` | L139 | `retrieval_top_k`（两条路径共用） |
| `app/core/config.py` | L143 | `retrieval_min_score`（熔断阈值 0.6） |
