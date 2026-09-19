# 04_route_query 节点

> 章节：Day05_Query 优化 · 第 3 章 后端实现 · 第 4 节
> 对应文件：`backend/app/workflows/nodes/route_query.py`（新增）、`backend/app/workflows/nodes/__init__.py`（导出）
> 记录日期：2026.09.19

---

## 一、本节是"缝合点"

前几节各自产出一块：

```
第 1 节：RAGState 的四个新字段           ← 数据契约
第 2 节：配置项 + 四组 prompt            ← 素材
第 3 节：QueryRewriter（统一入口）        ← 能力
第 4 节：route_query 节点                ← ★ 把它们接起来
```

**本节点职责只有三件事**（教程原话："这个节点的职责很简单"）：

```
读开关  →  调用 optimize()  →  把结果写回 state
```

**它不 try/except、不判断策略、不拼 prompt** —— 那些都在 `QueryRewriter` 里闭环。**这是前几节"把复杂性往上收"的收益兑现。**

---

## 二、开关短路（L50-52）

```python
if not settings.query_route_enabled:
    # 关闭路由：直接走原始查询，保留 normalize_query 透传的 state["query"]
    return {"route": "original"}
```

### 为什么只返回 `route`，不返回 `query`

**不是漏写，是刻意的**：

```
state["query"] 已由 normalize_query 写入 = 原始提问
→ 不覆盖它，就是"用原问题检索"
→ 这正是我们要的基线行为（与 optimize() 降级时同一语义）
→ 同时省掉一次无意义的字段覆盖
```

**行为效果（比"省一次覆盖"更重要）**：

```
不覆盖 query = 用原问题检索 = 基线行为

开关开 → query 可能被改写
开关关 → query 一定是原问题（= 严格意义上的上一期链路）
```

**这让"对照实验"的因果链是干净的**：关闭时跑的就是上一期链路，没有任何多余动作。

### 零成本的实证

```
开关关闭 → 返回 {'route':'original'}，耗时 0ms，无任何模型调用
开关打开 → route 判定正确，耗时 1400~3100ms
```

**这就是第 2 章 `query_route_enabled` 那个"总开关"设计的落地**——它把"效果度量"变成了一次配置切换。

---

## 三、`query` 的覆盖语义（L64）

```python
update: RAGState = {"route": result.route, "query": result.query}
```

**这一行是第 1 节"覆盖 query 而非新增字段"决策的落地。** 实测四种策略：

| route | `query` 字段的值 | 明细字段 |
| --- | --- | --- |
| `original` | 原问题 | 无 |
| `rewrite` | **改写后的问句**（本次实测等于原文，见第六节） | `rewritten_query` |
| `hyde` | **假设答案**（实测约 200 字技术描述） | `hyde_answer` |
| `multi_query` | **原问题**（保底检索路径） | `multi_queries` |

### HyDE 实测结果（最能说明"覆盖"的意义）

```
输入: 如何理解向量检索

query: 「向量检索是一种基于语义相似度的搜索技术，它将文本、图像或音频等数据映射到
        高维向量空间中，通过计算查询向量与候选向量之间的余弦相似度或欧氏距离实现匹配。
        核心步骤包括嵌入模型编码、向量索引构建（如FAISS、Annoy）、近似最近邻搜索（ANN）
        以及重排序优化。常用嵌入模型有BERT、Sentence-BERT、CLIP；索引结构支持IVF、HNSW
        等加速算法；评估指标包括Recall@K、MRR和NDCG。」
```

**一个 6 字的抽象提问，变成了约 200 字的技术描述**，而这段描述就装在 `query` 字段里——**它会被直接向量化**。这正是第 2 章所说"陈述句 + 高术语密度"在运行时的样子。

---

## 四、可选字段的按需回写（L68-73）

```python
if result.rewritten_query is not None:
    update["rewritten_query"] = result.rewritten_query
if result.hyde_answer is not None:
    update["hyde_answer"] = result.hyde_answer
if result.multi_queries is not None:
    update["multi_queries"] = result.multi_queries
```

### 教程注释与理由

> 我们只需要把非 None 字段写回 state，因为 `RAGState` 用了 `total=False`，没产出的字段自然不存在。

### 两种写法的区别（**具体后果**）

```python
# ❌ 无条件全写 → 产生"字段存在但值为 None"
update = {"route":..., "query":..., "rewritten_query": None, "hyde_answer": None, ...}
→ 下游这样读会踩坑：
     state.get("multi_queries", [])    # 以为拿到默认 []
     实际拿到 None！                    # 因为 key 存在，default 不生效
     → 接下来 for q in None  → TypeError
```

> **关键：`.get(key, default)` 只在 key 不存在时才用 default。**
> 把字段写成 `None`，`default` 就成了摆设——**下游以为有保护，实际没有**。

```python
# ✓ 按需写 → "字段不存在"就是"没产出"
update = {"route":..., "query":...}
→ 下游 state.get("multi_queries", []) 干净地拿到 []
```

**实测已验证**：四种策略的返回键集合**精确对应各自产出**，无任何多余 None 字段：

```
original     → ['query', 'route']
rewrite      → ['query', 'rewritten_query', 'route']
hyde         → ['hyde_answer', 'query', 'route']
multi_query  → ['multi_queries', 'query', 'route']
```

> 这是 `total=False` 契约的**正确用法**——"没产出"与"产出了 None"是两种语义，前者更干净。

---

## 五、为什么不写 `try/except`（L57）

```python
# 本节点没有 try/except
result = await get_query_rewriter().optimize(...)
```

**因为 `QueryRewriter.optimize()` 已保证"任何失败都降级 original 且不抛异常"**：

```
optimize() 内部：
  try:
      ...四种策略...
  except Exception:
      logger.exception(...)
      return QueryRouteResult(route="original", query=question)   ← 不抛
```

**在节点里再包一层是重复防御**，且会带来新问题：

```
节点若也 try/except 并降级
  → 它得自己构造 QueryRouteResult(route="original", query=state["question"])
  → 降级规则就有了两处实现（optimizer 与节点）→ 迟早不一致
```

> **降级规则只应有一处实现。** 节点保持"无脑"是有意设计——这是第 3 节"复杂性往上收"的延续。

---

## 六、⚠️ 实测重现的问题（已知缺口，不修）

第 3 节的发现二在本节被真实重现：

```
输入: 它的学分是多少
route: rewrite
query: 它的学分是多少          ← 改写未生效（等于原文）
rewritten_query: 它的学分是多少
```

### 连锁效应（比单纯"没优化"更糟）

```
rewrite 返回的是非空字符串 → 不触发任何降级
→ route 被写成 "rewrite"
→ query 被"覆盖"成与原文相同的值
→ 前端 QueryRoutePanel 会渲染 [Rewrite] 标签
   面板文字是"已改写为独立完整问题"
   ↑ 但实际没改 → 这是**主动的错误声明**
```

**对比另一种失败：**

| | 降级到 `original` | 标成 `rewrite` 但实际没改 |
| --- | --- | --- |
| 错误信号 | 有（日志 warning） | **无** |
| 前端展示 | 不渲染面板（诚实） | 显示"已改写"标签（**误导**） |
| 额外成本 | 省掉改写调用 | 白付一次调用 |
| 排查难度 | 日志可查 | **完全无痕** |

> **"静默的质量劣化"比"显式失败"更糟**——后者会报警、用户会反馈、日志有记录；前者什么都没有。

**额外教训**：该结果**通过了所有技术性校验**（非空、策略名合法），任何 `if` 都拦不住。**再次印证第 3 节的结论**：

> 降级机制覆盖"技术性失败"；"业务质量失败"永远不触发降级，只能靠度量。

**待办**：此现象在补上历史注入（教程第六节记录、计划第八期实现）后自然消失。在此之前若做路由准确率评测（教程第六节第 3 条建议），**要注意区分"路由判对但改写无效"，否则会把 rewrite 的失败算到路由头上**。

---

## 七、`nodes/__init__.py` 的导出更新

```python
# L17
from app.workflows.nodes.route_query import route_query

# L20-26
__all__ = [
    "load_context",     # 1. 历史消息加载节点
    "normalize_query",  # 2. Query 标准化与意图透传节点
    "route_query",      # 3. 查询优化策略路由节点（判定并按策略产出最终检索词）
    "retrieve",         # 4. 向量检索与拒答熔断节点
    "stream_generate",  # 5. 大模型流式输出生成节点
]
```

**两处改动**：新增导入与 `__all__` 条目、**注释编号顺延**（原 retrieve 是 3、generate 是 4，插入新节点后变为 4 和 5）。

**该文件的存在意义**（第 9 章学过）：**门面导出**——让外部写 `from app.workflows.nodes import route_query`，而不是深层路径。

**重要性**：下一节编排层接入时会从这个包统一导入：

```python
from app.workflows.nodes import (
    load_context, normalize_query, route_query, retrieve, stream_generate,
)
```

---

## 八、自测题与批改记录

| # | 题目 | 作答 | 判定 |
| --- | --- | --- | --- |
| 1 | 开关关闭时为何只返回 route，不返回 query？行为效果？ | 「normalize_query 在前，已是原始 question，不需要多余覆盖」 | ✅ |
| 2 | 三个明细字段为何要 `if not None` 才回写？无条件写会怎样？ | 「前者是没产出，后者是产出了 None，前者语义更干净」 | ✅ 需补后果 |
| 3 | 本节点为何不写 `try/except`？加了有什么新问题？ | 「optimize() 已保证降级不抛；节点若再 try 并降级，又要构造降级结果，会有不一致的地方」 | ✅ |
| 4 | `route_query` 在链路中的位置？为何必须在 normalize_query 之后、retrieve 之前？ | 「有点忘了」 | ❌ |
| 5 | `rewrite` 时 query 等于原文但 route 标成 rewrite，比降级成 original 更糟吗？ | 「更糟，因为不会触发任何降级策略，且生成的是无效回答没有报错，不好排查」 | ✅ 需补"误导" |

### 逐题批改

**第 1 题 —— 正确**

补行为效果：不覆盖 query 即"用原问题检索"——**与 optimize() 降级是同一语义**，所以开关关闭时跑的是严格意义上的上一期链路。

**第 2 题 —— 正确，但需说出具体后果**

```
无条件写 → key 存在但值为 None
→ state.get("multi_queries", []) 拿到 None（default 不生效）
→ for q in None → TypeError
```

**第 3 题 —— 正确**。降级规则只应有一处实现。

**第 4 题 —— 答"忘了"，本题讲透**

**新链路（本期后）：**

```
load_context → normalize_query → route_query → retrieve → stream_generate
                                      ↑
                                 本期插入这里
```

**为何必须在 normalize_query 之后：**

```
route_query 读 state["question"]（由初始输入写入）与 state["chat_history"]（由 load_context 写入）
并且第 1 节的"覆盖 query"——被覆盖的那个 query 正是 normalize_query 写入的
→ 没有 normalize_query 打底，route_query 覆盖谁？
```

**为何必须在 retrieve 之前：**

```
retrieve 读 state["query"]
→ route_query 的产出必须先写入 state，retrieve 才能拿到改写后的查询词
→ 顺序反了则 retrieve 拿到的仍是原始提问，优化白做
```

> **一句话记忆**：`route_query` 是"**检索前的最后一次查询加工**"。

**⚠️ 当前状态提醒**：`chat_service.stream_answer` 里**尚未调用 `route_query`**（当前编排仍是 `load_context → normalize_query → retrieve → ...`）。**接入编排是后面小节的事**——本节属于"节点写好但尚未生效"阶段。

**第 5 题 —— 正确，但需补"主动误导"这一层**

作答对（无降级、无报错、难排查）。补更严重的点：**它不只是"没优化"，而是"谎称优化了"**。

```
route='rewrite'  → 前端渲染 [Rewrite] 标签，文案"已改写为独立完整问题" → 主动错误声明
route='original' → 前端不渲染面板（QueryRoutePanel.tsx:23）→ 诚实
```

**且白付一次改写调用**（降级则省掉）。

---

## 九、本节一条结论

**节点应该"薄"到只剩三件事：**

```
读开关  →  调用统一入口  →  写 state 增量
```

**任何"策略判断、失败处理、结果构造"都不该出现在节点里**，因为它们各有归属：

| 职责 | 归属 |
| --- | --- |
| 策略怎么分发 | `QueryRewriter.optimize()` |
| 失败怎么降级 | `QueryRewriter` 内部（唯一实现处） |
| 明细怎么回写 | 节点（这是节点的本职：状态翻译） |

---

## 十、验证记录

```
1) py_compile（route_query.py / __init__.py）    → exit 0 ✅
2) 节点导出                                      → __all__ 五项，route_query 可导入 ✅
   签名：(state: RAGState) -> RAGState，异步函数 ✅
3) 开关关闭                                      → 返回 {'route':'original'}，耗时 0ms，未调模型 ✅
   返回键仅一处，证明保留了 normalize_query 写入的 query ✅
4) 开关打开 × 四种策略                             → route 判定全部正确 ✅
   返回键集合精确对应：original['query','route'] / rewrite 加 rewritten_query
                       / hyde 加 hyde_answer / multi_query 加 multi_queries ✅
5) multi_query 子查询多样性                        → 三条子查询角度各不相同，未退化为同义替换 ✅
```

**multi_query 实测子查询（真实输出）**：

```
输入: 对比一下 HNSW 和 IVFFlat 的优缺点
  [1] HNSW 和 IVFFlat 在近似最近邻搜索中的算法原理与索引结构差异分析
  [2] HNSW 与 IVFFlat 在高维向量检索场景下的查询延迟、内存占用及召回率性能对比
  [3] HNSW 的图遍历机制与 IVFFlat 的聚类+暴力搜索范式在可扩展性和训练开销上的优劣比较
```

**三条分别覆盖"算法原理 / 性能指标 / 可扩展性"**，符合第 2 节 prompt 里"角度必须真正错开"的要求。

---

## 十一、一句话总结

> `route_query` 节点只做三件事——**读开关短路、委托 `optimize()`、把结果翻译成 state 增量**；关闭时只返回 `{"route":"original"}` 而不覆盖 `query`，使关闭状态严格等价于上一期链路（实测 0ms、零模型调用），从而让总开关成为干净的对照实验入口；明细字段按"非 None 才回写"落盘，避免产生"键存在但值为 None"污染下游的 `.get(default)` 语义；节点刻意不写 `try/except`，因降级规则只应有一处实现；位置固定在 `normalize_query` 与 `retrieve` 之间（前靠它打底 query、后为检索提供改写词）。

---

## 十二、关联文件索引

| 文件 | 关键位置 | 说明 |
| --- | --- | --- |
| `app/workflows/nodes/route_query.py` | L31 | `route_query` 节点函数签名 |
| `app/workflows/nodes/route_query.py` | L50-52 | 总开关短路（只返回 route） |
| `app/workflows/nodes/route_query.py` | L57 | 委托 `optimize()`（无 try/except） |
| `app/workflows/nodes/route_query.py` | L64 | `query` 覆盖语义 |
| `app/workflows/nodes/route_query.py` | L68-73 | 明细字段按需回写 |
| `app/workflows/nodes/__init__.py` | L17 | `route_query` 导入 |
| `app/workflows/nodes/__init__.py` | L20-26 | `__all__` 五项与注释编号 |
| `app/llm/query_rewriter.py` | L164 | `optimize()`（降级唯一实现处） |
| `app/core/config.py` | L151 | `query_route_enabled`（本节点读取） |
| `app/workflows/rag_state.py` | L51-56 | 本节点写入的四个字段 |
| `frontend/src/components/QueryRoutePanel.tsx` | L23 | `original` 时不渲染面板 |
