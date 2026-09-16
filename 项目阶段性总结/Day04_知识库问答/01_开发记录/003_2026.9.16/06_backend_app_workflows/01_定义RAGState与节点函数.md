# 01_定义 RAGState 与节点函数

> 章节：Day04 知识库问答 · 后端实现 · 第 9 章
> 对应目录：`backend/app/workflows/`
> 记录日期：2026.09.16

---

## 一、本章要解决的问题

RAG 有 4 个步骤：装历史 → 改查询 → 检索 → 生成。每个步骤都要读上一步的产出、写出自己的产出。

最朴素的做法是层层传参：

```python
history = await load_history(cid)
query   = await normalize(question, history)
chunks  = await retrieve(query)
answer  = await generate(question, chunks, history)
```

这样能跑，但四个函数**强耦合**：想换顺序、想插一个"重排序"步骤、想在中间加判断，就得改一串函数签名。

LangGraph 的思路是：**定义一个共享的"状态黑板"，每个节点只往黑板上写自己那部分**。谁读谁写由黑板约定，不由函数调用链约定。

所以本章只做两件事：

1. `rag_state.py` —— 定义黑板长什么样（状态契约）
2. 四个节点函数 —— 定义每个节点读哪些字段、写哪些字段

---

## 二、核心概念：状态契约与增量合并

### 2.1 RAGState 的分层结构

`backend/app/workflows/rag_state.py`

```python
class RAGState(TypedDict, total=False):
    # 1. 初始输入
    conversation_id: UUID
    question: str
    # 2. load_context 产出
    chat_history: list[Message]
    # 3. normalize_query 产出
    query: str
    # 4. retrieve 产出
    retrieved_chunks: list[RetrievedChunk]
    refused: bool
    # 5. generate 产出（实际由编排器写入）
    answer: str
    # 6. 持久化后回写
    user_message_id: UUID
    assistant_message_id: UUID
```

字段注释里都标了「(xxx 节点产出)」——这是刻意的：**一个字段只由一个节点写，就不会有写入冲突**。

### 2.2 `total=False` 到底控制什么（本章最大误区）

**结论：`total=False` 是类型层面的开关，不是运行时行为。**

它管的是静态类型检查器（pyright / mypy）允许节点返回什么，跟"运行时读几个字段、返几个字段"无关。LangGraph 运行时拿到 `{"query": "xxx"}` 照样会 merge；即使 `total=True` 也不会让它多跑一次。

真实的差异在**代码能不能通过类型检查**：

```python
def normalize_query(state: RAGState) -> RAGState:
    return {"query": state["question"]}      # ✅ total=False 时通过
```

若改成 `total=True`（所有字段必填），这一行**立刻报类型错误**，因为返回的字典缺其他 8 个键。

而四个节点**全都是这种"只返回自己那部分"的写法**。所以 `total=True` 的后果是：

> **四个节点函数全部无法通过类型检查——不是资源浪费，是代码根本写不成这个形状。**

一句话：`total=False` 是"增量状态契约"在类型系统里的表达，与 LangGraph "节点返回部分更新、引擎负责合并"的运行时机制一一对应。**注解描述协议，框架实现协议。**

### 2.3 默认是"覆盖"，不是"追加"（隐藏陷阱）

LangGraph 合并时，**普通字段是直接覆盖的**：

```python
# state 里已有 chat_history=[m1, m2]
return {"chat_history": [m3]}     # ← 结果是 [m3]，m1、m2 没了
```

将来若要写"多路召回"想**追加**多来源 chunks，必须给字段加 reducer：

```python
import operator
from typing import Annotated

retrieved_chunks: Annotated[list[RetrievedChunk], operator.add]   # 追加而非覆盖
```

**当前四个节点都是"首次写入"，所以默认覆盖语义是正确的。** 等加"重排序"或"多路召回"时才会撞上这个坑。

---

## 三、四个节点的职责边界

### 3.1 数据流转全景

```
                     ┌──────────────────────────────────────┐
   conversation_id ──┤ load_context                         │
                     │ 读: conversation_id                  │
                     │ 写: chat_history                     │
                     └──────────────┬───────────────────────┘
                                    │ chat_history
                     ┌──────────────▼───────────────────────┐
   question ────────►│ normalize_query                      │
                     │ 读: question                         │
                     │ 写: query                            │
                     └──────────────┬───────────────────────┘
                                    │ query
                     ┌──────────────▼───────────────────────┐
                     │ retrieve                             │
                     │ 读: query                            │
                     │ 写: retrieved_chunks, refused        │
                     │     (+ answer, 仅在拒答时)            │
                     └──────────────┬───────────────────────┘
                          refused? ─┤
                     False          └─ True → 直接返回 answer，跳过生成
                                    │ retrieved_chunks
                     ┌──────────────▼───────────────────────┐
                     │ stream_generate                      │
                     │ 读: question, retrieved_chunks,      │
                     │     chat_history                     │
                     │ 写: ★ 什么都不写，只 yield 字符串      │
                     └──────────────────────────────────────┘
```

### 3.2 逐节点说明

#### ① `load_context.py` —— 装配多轮历史

- **读** `conversation_id` → **写** `chat_history`
- `limit = chat_history_window * 2`（`load_context.py:42`）

**为什么乘 2**：一轮完整问答 = 用户 1 条 + 助手 1 条 = 2 条 Message。配置 `window=5` 表示"最近 5 轮"，落到数据库 limit 就是 10 条。

**为什么按条数而非字符数截断**，两个理由：

1. **角色配对完整性**：按字符截断可能切在"用户问完、助手还没答"处，历史里出现孤立的 `HumanMessage`，模型会误判当前轮次。按 `window*2` 截断永远一问一答成对。
2. **一次查询搞定**：按条数可以直接写进 SQL 的 `LIMIT`；按字符得先查出来再裁剪。

配合 `conversation_repo.recent_messages`（`conversation_repo.py:85`）的实现——**先倒序取 N 条再在 Python 中反转成正序**，避免为了正序而先 `count(*)` 总行数，单次查询解决。

#### ② `normalize_query.py` —— 查询标准化（当前透传，故意留的空位）

- **读** `question` → **写** `query`
- 当前实现就一行：`return {"query": state["question"]}`

**为什么必须有这个节点**：它建立了关键接口约定——**`retrieve` 依赖的永远是 `query`，不是 `question`**。

```
question = 用户原话（可能是"那它的学分呢？"这种残缺指代）
query    = 真正用于检索的词（将来会被改写为完整语义）
```

留出这个节点后，将来加"指代消解 / Query 改写 / HyDE"时，**改造范围被限制在一个文件里**，其他三个节点一行不动。这就是**防腐层**思路：把易变的部分（查询怎么来）和稳定的部分（检索怎么用）隔开。

> 注：rerank（重排序）作用于**检索结果**，与 `query` 无关，不是这个节点的用途。

#### ③ `retrieve.py` —— 检索 + 拒答熔断（本章设计含量最高）

- **读** `query` → **写** `retrieved_chunks` + `refused`（拒答时额外写 `answer`）

核心判断（`retrieve.py:47`）：

```python
refused = not chunks or chunks[0].score < settings.retrieval_min_score
```

**双重熔断条件**：召回列表为空 或 Top-1 相似度低于阈值（0.6）。

**为什么叫"熔断"**：命中后直接把标准文案写进 `update["answer"]`，**下游根本不会再调 LLM**。

| | 拒答放在生成层 | 拒答放在检索层（本项目） |
|---|---|---|
| 流程 | 检索 → 生成时判断"资料够不够" | 检索 → 分数不够就**不调 LLM** |
| 成本 | 每次都付 LLM 费用 | 拒答路径零 LLM 成本 |
| 可靠性 | 靠 prompt 约束模型"别编" | **机制上不给模型编的机会** |

**省资源只是副产品，让幻觉没有发生的机会才是目的**——把"不要幻觉"从一句 prompt 约束升级成了架构约束。

#### ④ `generate.py` —— 流式生成（不写 state）

- **读** `question` + `retrieved_chunks` + `chat_history` → **不写任何 state**
- 返回 `AsyncIterator[str]`，逐个 token `yield`

**为什么它不能自己写 `state["answer"]`**：

1. 它是**异步生成器**——一个函数不能既 `yield` 多个碎片、又 `return` 一个完整 dict；
2. `yield` 出去的是碎片，而 `answer` 需要是完整字符串。

拼接必须由外部完成，这正是注释（`generate.py:15`）交代的边界：

> "全局答案的拼接与最终回写 `state['answer']` 全权交由外层编排调度器负责。"

编排层（第 10 章）的活：

```python
buffer = []
async for token in stream_generate(state):
    buffer.append(token)
    yield sse_event("token", delta=token)     # 边吐给前端
state["answer"] = "".join(buffer)             # 边累积写回 state
```

**所以 `answer` 字段有两个写入者，这是设计而非疏漏：**

| 路径 | 谁写 `answer` | 写的是什么 |
|---|---|---|
| 拒答（`refused=True`） | `retrieve` 节点 | 完整拒答文案，一次写完 |
| 正常生成 | **编排器**（不是 generate 节点） | 流式累积拼接后的完整答案 |

**⚠️ 由此引出的坑（第 10 章必踩）**：编排器决定要不要调 `stream_generate` 时，**必须先看 `state.get("refused")`**。若忘判、无条件调用生成，`retrieve` 写的拒答文案会被模型输出**直接覆盖**，熔断就白做了。

另：`isinstance(text, str)` 的类型守卫是必要的——LangChain 的 `chunk.content` 类型为 `str | list[str | dict]`（为多模态设计），纯文本模型几乎总走 `str` 分支，但类型检查器不认，需要兜底。

---

## 四、本章三个设计要点

### 要点 1：拒答在检索层，不在生成层

见 3.2 ③ 的对比表。核心：**把"不要幻觉"从 prompt 约束变成架构约束。**

### 要点 2：相似度归一化让阈值有意义

`vector_retriever.py:87` 做了 `score = 1.0 - distance`。

pgvector 返回的是**余弦距离**（0 最相似，越大越不像），而全系统心智模型是"**分数越大越相关**"。不转换的话：

- 阈值判断要写成 `distance > 0.4` 这种反直觉形式；
- 将来换距离度量（L2、内积）时全部要改。

**归一化的意义是让 `0.6` 这个数字本身可读、可调。**

### 要点 3：引用能对得上，靠的是编号纪律

`llm/prompts.py:91` 给片段编号：

```python
for index, chunk in enumerate(chunks, start=1):
    parts.append(f"【片段 {index}】 ({meta})\n{chunk.content}")
```

**从 1 开始编号**，模型回答里的 `[1]` 就对应 `retrieved_chunks[0]`。这也是系统提示词花大篇幅严令"禁止 `[1,2]`、禁止同文档串号"的原因——**一旦模型乱标编号，`[N]` 就无法映射回正确 chunk，`AnswerCitation` 的溯源就是错的**。

**编号可靠性 = 引用功能可靠性。**

---

## 五、自测题与批改记录

### 题目与作答

| # | 题目 | 作答 | 判定 |
|---|---|---|---|
| 1 | 若 `RAGState` 改成 `total=True`，四个节点会出什么问题？ | "每次必须读所有节点，并强制返回字段（哪怕是空的），严重浪费资源" | ❌ 概念跑偏 |
| 2 | `chat_history_window=5` 为什么是"乘 2 条消息"而不是"5 条"？按字符数截断会更好吗？ | "一轮对话有一来一回；按字符数截断可能产生语义截断" | ✅ 沾边 |
| 3 | `retrieve` 拒答时为什么要写 `update["answer"]`？交给 `generate` 判断有什么区别？ | "直接拦截了，交给 generate 会多一步不必要的判断浪费资源" | ✅ 正确 |
| 4 | `stream_generate` 不写 `state["answer"]`，那 `answer` 字段谁负责写？ | "不知道" | ❌ |
| 5 | `normalize_query` 什么都没做，能不能删掉让 `retrieve` 直接读 `question`？ | "不能，以后重排序用得到" | ⚠️ 结论对理由偏 |

### 逐题批改

**第 1 题 —— 跑偏点：把类型注解当成运行时行为**

`total=False` 是静态类型开关，不影响运行时读几个字段。`total=True` 的真实后果是：**四个节点"只返回自己那部分"的写法全部无法通过类型检查**，而不是资源浪费。

→ 已写入 2.2 节。

**第 2 题 —— 逻辑对，补两个更硬的理由**

- 作答的"语义截断"成立，但更关键的是 **角色配对完整性**：按字符截断可能出现"用户问了但助手回答被切掉"的孤立 `HumanMessage`，模型会误判当前轮次；
- 补充：按条数截断可直接写进 SQL `LIMIT`，**一次查询搞定**；按字符需先查再裁。

→ 已写入 3.2 ①。

**第 3 题 —— 正确，把第二句说得更准**

机制判断完全正确（`retrieve` 已把 answer 写好，下游不必再算）。但**最狠的理由不是省资源，而是不给幻觉机会**：若交给 generate 判断，LLM 已被调用，此时只能靠 prompt 求模型"别编"，而模型在没有参考资料时极易用自身知识编答案。

→ 已写入 3.2 ③。

**第 4 题 —— 本章分水岭，答案是"编排器累积写入"**

证据在 `generate.py:15` 的注释。原因是异步生成器无法同时 `yield` 碎片又 `return` 完整 dict，所以拼接必须外移。

**附带发现一个第 10 章的坑**：编排器必须先判 `refused` 再决定是否调用生成，否则拒答文案会被覆盖。

→ 已写入 3.2 ④。

**第 5 题 —— 结论对，理由偏**

rerank 作用于**检索结果**（输入是 `retrieved_chunks`），与 `query` 无关，不是这个节点的用途。

真正理由是**契约稳定性**：删除后 `retrieve` 只能写成 `retriever.search(state["question"])`，将来加改写就必须回头改 `retrieve` 的函数体；保留后改造范围被限制在一个文件内。

→ 已写入 3.2 ②。

---

## 六、遗留问题与后续待办

### 6.1 本章代码完整性

第 9 章对应代码**已全部就位**：

```
backend/app/workflows/
├── rag_state.py             ✅ 状态契约
├── nodes/load_context.py    ✅
├── nodes/normalize_query.py ✅
├── nodes/retrieve.py        ✅
├── nodes/generate.py        ✅ stream_generate
└── nodes/__init__.py        ✅
```

第 8 章的配套仓储 `citation_repo.py` 也在（`bulk_add` 只 `flush` 不 `commit`，与 ORM 仓储"事务由外层控制"的约定一致 ✓）。

### 6.2 待第 10 章处理

- `workflows/graph.py` **图装配**尚未创建（四个节点目前是四个孤立函数，没有 `StateGraph`、没有边、没有条件路由）
- `services/chat_service.py` **编排层**尚未创建：负责落库、跑图、累积 answer、转 SSE
- 编排层必须先判 `refused` 再决定是否调用 `stream_generate`（见 3.2 ④ 的坑）

### 6.3 与本章相关但独立的遗留问题

**`page_no` / `section_path` 全库为 NULL**（9 篇文档、617 个 chunk 无一例外）。

- 读的地方有 4 处：`vector_retriever.py:82-83`、`prompts.py:95-99`、`api/schemas/documents.py:216-217` 与 `365-366`
- 写的地方 **0 处**：`parser.py` / `splitter.py` / `pipeline.py` 均未写入

根因在入库链路：`parser.py:117` 用 `export_to_markdown()` 把整篇文档拍平成一个字符串，`parser.py:159` 的 metadata 只剩 `{"source": filename}`，`splitter.py` 只补了 `chunk_index` 与 `chunk_hash`。**「整篇导出」的做法结构上就拿不到页码。**

后果：`prompts.py` 里 `if chunk.page_no is not None` 与 `if chunk.section_path` 永远不成立，`answer_citations.page_no` 永远是 NULL。

可行性：Docling 提供 `num_pages`、`export_to_markdown(page_no=N)`、`iterate_items(page_no=N)`，且每个 item 带 `prov`（provenance）含页码，标题层级可从结构化树取。**修法需要把"整篇导出"改为"按页导出 + 面包屑栈"，属于设计决策。**

### 6.4 阈值待校准

`retrieval_min_score = 0.6` 目前是合理起点，但**未经数据验证**。

实测一次（query = "课程目标是什么"）：Top-5 中仅 2 条过线（0.7174 / 0.7088），后 3 条为 0.5787 / 0.5768 / 0.5447。若某个问法的 Top-1 落在 0.58，就会直接拒答——**哪怕召回内容其实是对的**。

建议：等第 10 章能跑通编排后，用 10~20 个真实问题做批量检索，看 Top-1 分数的分布再定阈值。

---

## 七、本章一句话总结

> 四个节点各自只碰自己的字段，`total=False` 让这种"增量契约"在类型层面成立；`generate` 故意不写 state，把持久化推给编排层；`retrieve` 把拒答提到 LLM 之前——**所有设计都指向同一个目标：让每个节点的职责边界清晰到"可以单独替换"。**

---

## 八、关联文件索引

| 文件 | 关键位置 | 说明 |
|---|---|---|
| `app/workflows/rag_state.py` | L25 | `RAGState` 状态契约 |
| `app/workflows/nodes/load_context.py` | L40-43 | 滑动窗口 `window * 2` |
| `app/workflows/nodes/normalize_query.py` | L28 | 透传实现（预留改写扩展点） |
| `app/workflows/nodes/retrieve.py` | L47 | 双重拒答熔断 |
| `app/workflows/nodes/generate.py` | L15、L28 | 职责边界注释、异步生成器 |
| `app/retrieval/vector_retriever.py` | L30-44、L87 | `RetrievedChunk` DTO、距离转相似度 |
| `app/llm/prompts.py` | L30-56、L91 | 引用纪律系统提示词、片段编号 |
| `app/db/repositories/conversation_repo.py` | L85-107 | 倒序截断 + 反转正序 |
| `app/db/repositories/citation_repo.py` | L36-50 | 引用批量写入（只 flush） |
