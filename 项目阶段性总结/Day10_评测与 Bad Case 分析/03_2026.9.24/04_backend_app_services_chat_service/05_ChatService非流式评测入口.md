# 05_ChatService非流式评测入口

> 期：**Day10 · 评测与 Bad Case 分析**
> 章：第 3 章 后端实现 · **第 5 步「ChatService 加非流式评测入口」**
> 记录日期：2026.09.24

---

## 一、本节在整条链路里的位置

> **前四步都在"造工具"：账本、考卷、判分标准、专业评委。
> 这一节第一次把工具接到真实的 RAG 链路上 —— 让评测真的"跑起来"。**

```
第 1 步  新建评测表      ← 账本（已归档）
第 2 步  构建评测集      ← 考卷 + 读卡机（已归档）
第 3 步  人工指标与归因   ← 判分标准（已归档）
第 4 步  RAGAS 自动指标  ← 请专业评委（已归档）
第 5 步  评测入口        ← 你在这里：把评测接到真实 RAG 链路上
第 6 步  评测 Service    ← 执行器主流程：把 1~5 串起来
第 7 步  评测 API        ← 暴露成 8 个接口
```

### ⭐ 本节的标题里藏着整期的关键一跃

教程的标题是「**让评测与线上走同一条链路**」。这句话的落地方式就一行代码：

```python
final_state = await get_rag_graph().ainvoke(state)   # stream_answer 与 answer_for_evaluation 都走这里
```

| 如果评测自己另写一套检索 | 本项目的做法 |
| --- | --- |
| 测出来的分数**不能代表线上表现** | 同一个 `get_rag_graph()`、同一个 `stream_generate()` |
| Day10 整套评测体系**失去意义** | **评测的价值 = 复现线上 + 不污染线上** |

**所以本节要同时解两个看似矛盾的约束**：

1. **必须复现线上** —— 走同一条 RAG 链路、同一条校验逻辑；
2. **绝不能污染线上** —— 不建会话、不写消息、不落引用。

---

## 二、本节与后续章节的**依赖关系**

```
   第 2 步（已归档）              第 5 步（本节）                 第 4 步（已归档）
   EvaluationCase                answer_for_evaluation()         evaluate_batch()
   ├─ question ─────────────────► state["question"]                    ▲
   ├─ expected_answer ──────────►（第 6 节拼 RagasSample）              │
   └─ should_refuse ────────────►（第 6 节喂 classify_bad_case）        │
                                         │                             │
   第 3 步（已归档）                       ├── answer ────────────────► RagasSample.answer
   classify_bad_case()                    ├── chunks（c.content）────► retrieved_contexts
   compute_citation_hit() ◄───────────────┤                             │
                                          └── citations（9 键）
                                         │
                                         ▼
                            第 6 步 执行器：组装 → 打分 → 归因 → 落库
                                         ▼
                            第 7 步 API + 前端看板
```

| 后续章节 | 依赖本节的什么 |
| --- | --- |
| 第 6 节 | **`answer_for_evaluation(question)` 是执行器每条 case 必调的一次**；`EvaluationAnswer` 被拆成三份：`answer`+`chunks`→RAGAS、`citations`→`compute_citation_hit`、`refused`/`error_message`→`classify_bad_case` |
| 第 7 节 | `evaluation_items` 表的输入快照列（`query_route` / `agent_steps` / `trace_id`）与耗时列（`latency_ms` / `first_token_latency_ms`）**直接取自本节返回值** |

### ⭐ 三个字段的"下游分派"关系（本节最该记住的一张图）

```
EvaluationAnswer
├── answer ─────────────────► RagasSample.answer      → answer_relevancy / faithfulness
├── chunks（取 c.content）───► RagasSample.retrieved_contexts → context_precision / recall
├── citations（9 键）────────► compute_citation_hit(actual_citations=...)   ← 第 3 节
├── refused ────────────────► classify_bad_case(...)  ← 第 3 节（Level 1 判定）
├── error_message ──────────► classify_bad_case(is_error=True) → other
└── 4 项耗时/追踪 ──────────► evaluation_items 的性能列 + 前端 trace 跳转
```

> 🎯 **一句话**：**第 5 节负责"取数"，第 6 节负责"分派"。**
> 本节把所有东西**一次跑齐、装进一个不可变对象**，就是为了让第 6 节只做组装、不再碰 RAG 链路。

---

## 三、返回值契约：`EvaluationAnswer`（L66-136）

### 3.1 四条设计要点（docstring）

| # | 要点 | 落地方式 |
| --- | --- | --- |
| 1 | **沙箱隔离** | 不落 `conversations` / `messages`（实测三表计数一条不变） |
| 2 | **指标底座** | 除 `answer` 外完整暴露 `chunks`，供外层映射成 `RagasSample.retrieved_contexts` |
| 3 | **归因画像** | 聚合路由 / Agent 轨迹 / 校验结果 / 耗时，让 Bad Case 能定位 |
| 4 | **不可变** | `@dataclass(frozen=True)`，进入并发评测与统计环节后不会被无意篡改 |

### 3.2 11 个字段（分四区）

| 区 | 字段 | 位置 | 要点 |
| --- | --- | --- | --- |
| **核心结果** | `answer` | **L82** | 可能已被 verify **整段替换**成拒答文案 |
| | `refused` | **L87** | 三条来源：① `plan_retrieval` 判定（只置位）② `refuse` 节点写文案 ③ verify 失败强制置位 |
| | `chunks` | **L92** | ⚠️ 文本字段是 **`content` 不是 `text`**；**拒答时照样有值** |
| **决策轨迹** | `query_route` | **L98** | 固定 5 键：`route`/`query`/`rewritten_query`/`hyde_answer`/`multi_queries` |
| | `agent_steps` | **L104** | 每项固定形如 `{round, action, reason, route, query, retrieved_count, top_score}` |
| | `verify_result` | **L108** | `VerifyResult(verified, reason)`；⚠️ **与敏感词无关**，拒答时恒 `None` |
| **可观测** | `trace_id` | **L113** | 本项目接 **LangSmith**；未启用观测时为 `None` |
| | `latency_ms` | **L116** | 起算点 = 进入函数那一刻 |
| | `first_token_latency_ms` | **L121** | ⭐ **与 `latency_ms` 共用同一个 `started_at`** |
| **异常与溯源** | `error_message` | **L129** | 只是**摘要**，完整堆栈在 `logger.exception` |
| | `citations` | **L136** | 9 键；⭐ 形状就是第 3 节 `compute_citation_hit` 的入参 |

### 🔴 3.3 一处必须知道的语法约束

`error_message` 与 `citations` **带默认值**，而 Python 规定
**带默认值的字段不能排在无默认值字段之前** → 所以 `citations` 只能压在最后，
哪怕它逻辑上属于"核心结果区"。**别顺手往上挪，一挪就 `TypeError`。**

---

## 四、`answer_for_evaluation` 的六个步骤（L627-854）

### 步骤 1 · 沙箱隔离与初始状态构造（L644-669）

```python
started_at = time.perf_counter()        # ① 单调时钟，避免 NTP 回拨算出负数
trace_id = get_current_trace_id()       # ② 第 9 期观测入口
state = {
    "conversation_id": UUID(int=0),     # ③ 全 0 UUID 占位：类型合法 + 流量染色
    "question": question,
    "chat_history": [],                 # ④ 强制置空 → 评测用例完全独立
    "trace_id": trace_id,
}
```

**为什么 `conversation_id` 要占位而不是 `None`**：`RAGState` 强类型要求 `UUID`；
`UUID(int=0)` 既能过类型检查，又能让日志/链路系统**一眼认出这是离线评测流量**。

### 步骤 2 · 执行 LangGraph 核心图拓扑（L673-685）

```python
final_state = await get_rag_graph().ainvoke(state)
state.update(final_state)   # type: ignore[arg-type]
```

- 图内黑盒完成：**意图分类 → 关键词/向量混合检索 → RRF 融合 → 重排打分 → 拒答裁定**；
- `type: ignore` 的原因：`ainvoke()` 返回 `dict[str, Any]`，与 `TypedDict.update()` 期望的映射类型不一致。

### 步骤 3 · 答案生成与 TTFT 捕获（L699-733）

```python
if state.get("refused"):
    answer = state["answer"]            # 拒答快速通道：不调 LLM
else:
    parts = []
    async for delta in stream_generate(state):
        if first_token_latency_ms is None:
            first_token_latency_ms = int((time.perf_counter() - started_at) * 1000)
        parts.append(delta)
    answer = "".join(parts)
    state["answer"] = answer            # 回填 state
```

| 设计点 | 说明 |
| --- | --- |
| **`parts` 列表缓冲** | 避免字符串频繁 `+=` 造成的内存重分配 |
| **`if ... is None` 守卫** | 只在**首个 token** 打点一次 |
| **拒答快速通道** | 跳过大模型调用 → `first_token_latency_ms` **保持 `None`**，如实反映"未发起生成" |

> ⭐ **`first_token_latency_ms` 的起算点与 `latency_ms` 相同** ——
> 它把检索、规划、重排全算进去了，测的是「**用户体感首字时间**」，不是「模型首 token 时间」。

### 步骤 4 · 后置事实校验与线上护栏对齐（L736-763）

```python
if settings.verify_answer_enabled:
    verify_result = await get_answer_verifier().verify(
        question, answer, chunks=list(state.get("retrieved_chunks", [])),
    )
    if not verify_result.verified:
        answer = REFUSAL_ANSWER          # 覆盖不可信回答
        state["answer"] = answer
        state["refused"] = True          # 强制置位
```

**这一步是"评测复现线上"的关键**：线上 `stream_answer` 校验失败后也是同一套动作
（替换成 `REFUSAL_ANSWER` + `refused=True`），**保证两边指标口径一致**。

### 步骤 5 · 切片序列化、角标提取与快照装配（L767-818）

```python
chunks = list(state.get("retrieved_chunks", []))
refused = bool(state.get("refused"))        # ← 最终定妆：verify 可能改过它
citations = [] if refused else [
    _serialize_citation(c, ordinal=i) for i, c in enumerate(chunks, 1)
]
return EvaluationAnswer(...)
```

> 🔴 **缩进位置很关键**：本段必须与上面的 `if/else` **平级**（12 空格）。
> 一旦误缩进到 `if verify_answer_enabled` / `else` 里面，
> 【前置拒答】与【关闭校验】两条路径就会跳过这里、掉出函数并**隐式返回 `None`**。
> **本项目真的踩过这个坑**（见 §8.1）。

**`refused = bool(...)` 这一行的意义**：它把"前置拒答"和"verify 失败"两条路径**归一化**，
所以后面只需要一次 `citations` 判断，就能让**两条拒答路径的引用都被清空** ——
杜绝"答案说拒绝回答、下方却展示参考资料"的自相矛盾。

### 步骤 6 · 异常隔离护栏（L821-854）

```python
except Exception as exc:
    logger.exception("Evaluation answer failed: question=%r", question)
    return EvaluationAnswer(answer="", refused=False, chunks=[], ...,
                            error_message=str(exc).strip() or exc.__class__.__name__)
```

| 关键点 | 说明 |
| --- | --- |
| **绝不向上抛** | 否则上层批处理循环直接中断，**后续测试用例全部报废** |
| **`question=%r`** | `repr` 输出带转义的原题，防止题干自带换行污染日志 |
| **保留现场** | `query_route` / `agent_steps` **照样透传**，即使崩溃也能看停在哪一步 |
| **`chunks=[]`** | ⚠️ 意味着下游 RAGAS **拿不到任何上下文** → 该 case 必然 Failed |
| **双重兜底** | `str(exc).strip() or exc.__class__.__name__`，保证 `error_message` 非空 |
| **`trace_id` 仍保留** | 支持在 LangSmith 上溯源崩溃点 |

---

## 五、本节的灵魂：一个类，两条入口

| | **`stream_answer`（L388）** | **`answer_for_evaluation`（L627）** |
| --- | --- | --- |
| 服务对象 | 线上真实用户 | 离线评测集 |
| 出口形态 | 逐条 `yield` SSE 事件 | 一次性返回 `EvaluationAnswer` |
| 会话 ID | 必须真实存在（先校验再 404） | `UUID(int=0)` **占位** |
| 历史消息 | `load_context` 读出来拼进 prompt | **`chat_history: []` 强制为空** |
| **落库** | 写 `conversations` / `messages` / `answer_citations` | **一个字都不写** |
| 生成方式 | 逐 token 吐给前端 | `"".join(parts)` **聚合成整段** |
| 校验结果 | 通过 SSE `verify_result` 事件下发 | 存进返回值的 `verify_result` 字段 |
| 出错时 | `yield` 一个 error 事件（HTTP 早已 200） | 存进 `error_message`，**不抛异常** |
| **RAG 链路** | **`get_rag_graph().ainvoke()`** | **← 完全相同** |

### ⭐ "不写库"是怎么实现的？

```
L856-984  _persist_user_message / _persist_assistant_message
          ↑ 只被 stream_answer 调用
          ↑ answer_for_evaluation 里【一次都没出现】
```

**翻遍 `answer_for_evaluation`：它从头到尾没碰过 `self.session`，也没调过任何 `_persist_*`。**

| 若返回空列表 | 实际做法（等长 + 空占位） |
| --- | --- |
| 第 6 节 `zip(samples, metrics)` **直接丢数据** | 长度严格对齐，**逐条**落库为"本轮没算出来" |

> 🎯 这比"传个 `write_db=False` 开关"干净得多 ——
> **没有开关，就没有"开关传错导致评测写脏生产库"的可能。**

**一个实用推论**：`answer_for_evaluation` **完全不需要数据库 session**
（它不碰 `self.session`）→ 第 6 节可以直接 `ChatService(None)` 调用，
**不必为每条 case 开一个数据库连接**。本节的验证就是这么跑的。

---

## 六、运行验证（真实执行，非纸面推导）

### 6.1 静态检查

| 检查项 | 结果 |
| --- | --- |
| 应用可正常导入、`len(app.routes) == 8` | ✅ |
| `EvaluationAnswer` 11 字段 / `frozen=True` | ✅ |
| **AST：`return` 挂在 `try` 顶层**（不在 `if` / `else` 里） | ✅ |

### 6.2 真机调用

| 检查项 | 结果 |
| --- | --- |
| **正常题**：返回 `EvaluationAnswer`（不是 `None`）、有答案、无报错 | ✅ |
| **`citations` 与 `chunks` 等长**（未拒答时） | ✅ 5 / 5 |
| **首字延迟是整数、`latency_ms` 为正** | ✅ 5112 ms / 6825 ms |
| **`trace_id` 有值** | ✅ |
| **降级/拒答路径返回的仍然是 `EvaluationAnswer`** | ✅（实测出现过 `refused=True`、`citations=[]`、`first_token=None`、`verify_result=None` 的正确快照） |
| **不写库**：`(conversations, messages, answer_citations)` 前后一致 | ✅ **`(7, 56, 80)` → `(7, 56, 80)`** |

### 6.3 ⭐ 一条必须记下来的实测现象：**同一条 case 多次跑，结论不一样**

拿同一道越界题（珠峰海拔）跑了三轮：

| 轮次 | `refused` | `chunks / citations` | `first_token_latency_ms` | `verify_result` | 实际回答 |
| --- | --- | --- | --- | --- | --- |
| A | `False` | 5 / 5 | 11206 | `True` | "知识库中没有找到相关信息。" |
| B | **`True`** | **20 / 0** | **`None`** | **`None`** | "没有找到与该问题相关的可靠依据。" |
| C | `False` | 5 / 5 | 11302 | `True` | （同上，被 verify 判为"符合拒答规范"） |

连**正常题**也抖：三轮的 `route` 分别是 `multi_query` / `original` / `original`，
`agent_steps` 条数与 `latency_ms` 也各不相同。

**根因**：`plan_retrieval`（Agentic 规划）与 `judge_context`（上下文裁定）**都是 LLM 决策**，
即使 `temperature=0` 也不保证稳定。

> 🎯 **这对 Day10 的意义很大**：评测分数天生带抖动，
> **同一份评测集跑两次，Bad Case 数就可能不一样**。
> 第 6 节的执行器必须把每一轮结果完整快照（`run_id` + `trace_id`），
> 将来才有"同一 case 跨批次对比"的依据 —— **这正是第 1 步建 `evaluation_runs` 表的原因**。

---

## 七、配图（1 张）

| 图 | 文件 | 讲清什么 |
| --- | --- | --- |
| **图一** | `01_answer_for_evaluation 非流式评测入口流程.png` | ⭐ **六个步骤 + 两条出口**（正常交付 / 降级交付） |

**图一覆盖的六个步骤**：沙箱初始化 → 图拓扑 → 流式聚合与 TTFT → 后置校验 → 快照装配 → 异常护栏。

**排版建议**：单独一张即可（本节只有一条主链路）。
如果后续想补，建议再画一张 **`ChatService` 两条入口对照图**（`stream_answer` vs `answer_for_evaluation`），
因为"一个类两条入口"才是本节的灵魂 —— 见 §5 的对照表。

### 本节画图沉淀出的 3 条规则

| # | 规则 | 出处 |
| --- | --- | --- |
| 1 | **`try/except` 的捕获范围要画全**：虚线不能只从"第一个动作"引出，否则读者会以为只有它才会抛错 | 图一（护栏原本只从步骤 2 引出） |
| 2 | **分支标签必须写真实原因**：写"风控/无权限"这类本项目**并不存在**的机制，会让人对代码行为产生错误预期 | 图一（拒答分支） |
| 3 | **长边的标签要贴近起点**：拒答「是」这条边太长，标签落在半路、紧挨着另一个菱形，容易被误读成它的分支标签 | 图一（待优化） |

---

## 八、遗留与待确认

### 🔴 8.1 缩进事故复盘（本节最重要的教训）

**现象**：给本节补注释时，**步骤 5 整段（含 `return`）被误缩进到了 `if settings.verify_answer_enabled:` 内部**。

**后果**（实测复现过）：

| 路径 | 结果 |
| --- | --- |
| **前置拒答**（`state.refused=True`） | 整个 `else` 分支不进 → 步骤 5 不执行 → **函数掉出 try、隐式返回 `None`** |
| **关闭 `verify_answer_enabled` 开关** | `if` 不成立 → 同样**返回 `None`** |

第 6 节执行器拿到 `None` 后，`result.answer` 立刻 `AttributeError` → **整轮评测中断**。
**最阴险的是它不抛异常**，只静默返回 `None` —— 比抛异常难查得多。

**修复**：L767-818 整体左移 4 空格（16 → 12），并在步骤 5 头部**加了一段防回归注释**说明为什么必须与 `if/else` 平级。

> **教训**：**大段补注释时，编辑器的自动缩进会把整段带偏**。
> 改完必须跑一次这个函数的两条路径（**正常 + 拒答**），
> 或者用 AST 断言 `return` 是否还在 `try` 顶层。

### 8.2 ⚠️ `refused` 标志与答案文本可能不一致

实测三轮里有 **2 轮**出现：**模型自己回答"知识库中没有找到相关信息"，但 `refused` 仍是 `False`**，
于是 **5 条 `citations` 被照常下发**。

这与 `refused=True` 时的行为（清空 `citations`）**自相矛盾** ——
"答案说没有依据、下方却给出 5 条参考资料"。

**归因**：真正的拒答闸门（`judge_context` / `plan_retrieval`）没有触发，模型只是"自己礼貌地说没找到"。
在第 3 节的分类体系里，这应当落到 **`context_judge_too_loose`**（闸门过松）。

> 🎯 这正是评测系统该抓的东西 —— 本节只是**如实快照**，判定交给第 3 节。

### 8.3 其余待确认

| # | 项 | 说明 |
| --- | --- | --- |
| 1 | ⚠️ **评测结果天然非确定** | 见 §6.3。同一条 case 三次跑出三种结果，根因是 LLM 决策不稳定。第 6 节要么多次跑取均值，要么固定路由策略 |
| 2 | 🔸 **`answer_for_evaluation` 未走 `load_context`** | 因为 `chat_history` 强制为空，`load_context`（唯一带 DB IO 的节点）被完全跳过 —— 这是设计意图，但意味着**多轮改写分支在评测中永远不会被覆盖** |
| 3 | 🔸 **verify 失败时的 `state["answer"]` 覆盖未画进图** | 代码里 verify 失败会同时改 `answer` 与 `state["answer"]`，图中只体现了前者 |
| 4 | ⚠️ **`planner.refuse` 设计缺口（跨期遗留）** | 第 8 期就记过的三项选择（A/B/C）至今未落地，本节实测到的"拒答标志与文案不一致"与它同源 |
| 5 | 🔸 **`types.gen.ts` 与 `EvaluationAnswer` 暂无交集** | 该类型是**后端内部契约**，不对外暴露；前端只消费第 7 节的响应模型（`EvaluationItemRead`） |

---

## 九、关联文件索引

### 9.1 本节修改（1 个文件）

| 文件 | 位置 | 说明 |
| --- | --- | --- |
| **`backend/app/services/chat_service.py`** | **670 行 → 984 行** | 新增 `EvaluationAnswer`（L66-136）+ `answer_for_evaluation`（L627-854） |

### 9.2 `chat_service.py` 精确锚点

| 位置 | 内容 |
| --- | --- |
| L1-32 | 模块 docstring（6 条设计要点：编排职责 / 双会话策略 / 引用序号契约 / 异常边界…） |
| L34-63 | imports + `logger` |
| **L66-136** | **`EvaluationAnswer`**（`@dataclass(frozen=True)`） |
| L68-77 | docstring：与 `stream_answer` 的三点差异 |
| L79-82 | 核心结果区 · `answer` |
| L84-87 | `refused`（三条来源 + 修复动作不同） |
| L89-92 | `chunks`（⚠️ 是 `content` 不是 `text`） |
| L94-108 | 决策链路区 · `query_route` / `agent_steps` / `verify_result` |
| L110-121 | 可观测区 · `trace_id` / `latency_ms` / `first_token_latency_ms` |
| L123-129 | ⚠️ **为什么带默认值的字段必须压在最后** |
| L131-136 | `citations`（9 键 + 跨节衔接） |
| **L139-276** | 5 个模块级纯函数（`_serialize_agent_steps` / `_build_verify_payload` / `_build_retrieval_meta` / `_serialize_citation` / `_build_query_route_payload`） |
| **L221-253** | ⭐ **`_serialize_citation`** —— `citations` 9 个键的定义处 |
| L255-276 | `_build_query_route_payload` —— `query_route` 5 个键的定义处 |
| **L278-380** | `class ChatService`：`__init__`（L289）+ 非流式 CRUD（L297-380） |
| **L382-615** | **`stream_answer`**（流式主链路，第 8/9 期的主战场） |
| **L617-854** | ⭐ **`answer_for_evaluation`**（本节核心） |
| L620-626 | `@traceable` 根节点声明与理由 |
| L628-642 | docstring：与 `stream_answer` 的四点差异 |
| **L644-669** | 步骤 1 · 沙箱隔离与初始状态构造 |
| L646-654 | ⭐ **`time.perf_counter()` vs `time.time()`** 的完整论证 |
| L660-668 | `UUID(int=0)` 的三条理由 |
| **L673-685** | 步骤 2 · 执行 LangGraph 核心图拓扑 |
| **L699-733** | 步骤 3 · 答案生成与 TTFT 捕获 |
| L702-707 | ⭐ **拒答快速通道**（不调 LLM） |
| **L736-763** | 步骤 4 · 后置事实校验与线上护栏对齐 |
| **L767-818** | 步骤 5 · 切片序列化与快照装配（**⚠️ 缩进位置敏感**） |
| L769-772 | `refused = bool(...)` 最终定妆 |
| L783-790 | `citations` 条件装配 |
| **L821-854** | 步骤 6 · 异常隔离护栏（Fail-Safe） |
| **L856-984** | `_persist_user_message`（L859）/ `_persist_assistant_message`（L897）—— **只被 `stream_answer` 调用** |

### 9.3 依赖与相关

| 位置 | 说明 |
| --- | --- |
| `backend/app/workflows/graph.py` | `get_rag_graph()` —— **两条入口共用的同一条图**（本节"复现线上"的载体） |
| `backend/app/workflows/nodes/__init__.py` | `stream_generate`（逐 token 生成器，评测里被聚合成整段） |
| `backend/app/workflows/rag_state.py` | L59-75：`retrieved_chunks` / `refused` / `agent_steps` / `retrieval_round` / `context_sufficient` 的声明与结构 |
| `backend/app/workflows/nodes/plan_retrieval.py` | **L134** `update["refused"] = True`（拒答来源①，只置位不写文案） |
| `backend/app/workflows/nodes/refuse.py` | **L39** `return {"refused": True, "answer": REFUSAL_ANSWER}`（拒答来源②） |
| `backend/app/llm/answer_verifier.py` | `VerifyResult(verified, reason)` + `get_answer_verifier()` —— 事实一致性校验（**与敏感词无关**） |
| `backend/app/llm/models.py` | `get_chat_model()` —— 模块级**单例**，`streaming=True` |
| `backend/app/retrieval/vector_retriever.py` | **L34-35** `@dataclass(frozen=True) class RetrievedChunk`（文本字段是 `content`） |
| `backend/app/core/observability.py` | `get_current_trace_id()` / `build_trace_url()` —— **只在 `@traceable` 函数体内有效** |
| `backend/app/core/config.py` | `settings.verify_answer_enabled`（步骤 4 的开关） |
| `backend/app/evaluation/ragas_runner.py` | `RagasSample` / `evaluate_batch` —— 消费本节的 `answer` 与 `chunks` |
| `backend/app/evaluation/scoring.py` | `compute_citation_hit(actual_citations=...)` 消费本节的 `citations`；`classify_bad_case(...)` 消费 `refused` 与 RAGAS 分数 |
| `backend/app/db/models.py` | L1150-1228：`evaluation_items` 的输入快照列与耗时列（第 6 节把本节返回值落库到这里） |

### 9.4 与第 4 节的对照

| | 第 4 节 `ragas_runner.py` | **第 5 节 `answer_for_evaluation`** |
| --- | --- | --- |
| 职责 | 把业务对象翻译给 RAGAS | **跑真实 RAG 取数** |
| IO | 网络（LLM + Embedding） | **网络 + 数据库（只读）+ 图执行** |
| 失败出口 | 0（全部降级成 `None`） | **0（降级成 `error_message`）** |
| 对 DB 的态度 | 完全不碰 | **只读检索，绝不写入** |
| 核心难点 | 把不确定的第三方 API 变成确定的类型 | **在"复现线上"与"不污染线上"之间同时成立** |
