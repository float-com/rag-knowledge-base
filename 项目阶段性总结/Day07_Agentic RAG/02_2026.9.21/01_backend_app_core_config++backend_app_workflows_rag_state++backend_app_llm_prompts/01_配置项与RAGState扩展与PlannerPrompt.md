# 01_配置项与RAGState扩展与PlannerPrompt

> 章节：Day07_Agentic RAG · 第 3 章 后端实现 · **第 1-4 步**
> 覆盖：
> - 第 1 章 装 LangGraph（`pyproject.toml`，**无需改动**：已装）
> - 第 2 章 Agentic RAG 配置项（`backend/app/core/config.py`）
> - 第 3 章 扩展 `RAGState`（`backend/app/workflows/rag_state.py`）
> - 第 4 章 Planner Prompt（`backend/app/llm/prompts.py`）
> 记录日期：2026.09.21

---

## 一、本批次的定位

这是本期「后端实现」的第一步。前 3 步都是**准备工作**（依赖 / 配置 / 状态契约），第 4 步是**决策器的提示词** —— 也就是说，**到本批次结束为止，还没有任何图或节点代码**，但所有"决策器要用的输入输出契约"都已就位。

| 步骤 | 文件 | 改动 | 行数变化 |
| --- | --- | --- | --- |
| 1. 装 LangGraph | — | **无需操作**（已装 1.2.11） | — |
| 2. 配置项 | `app/core/config.py` | +13 行 | 180 → **193** |
| 3. 扩展 `RAGState` | `app/workflows/rag_state.py` | +16 / -3 行 | 68 → **80** |
| 4. Planner Prompt | `app/llm/prompts.py` | +73 行 | 290 → **362** |

```
git diff --stat: 3 files changed, 99 insertions(+), 3 deletions(-)
```

---

## 二、第 1 章：装 LangGraph

### 2.1 依赖实测（本批次无需安装）

```
langgraph                    1.2.11     ← 图编排框架本体
langchain-core               1.6.2      ← ChatPromptTemplate / BaseMessage 来源
langchain                    1.4.0
langgraph-checkpoint         4.2.0      ← 持久化 / 断点续跑（本期未用）
```

**关键导入全部可用**：

```
OK  from langgraph.graph import StateGraph
OK  from langgraph.graph import END
OK  from langgraph.graph import START
OK  from langchain_core.prompts import ChatPromptTemplate
OK  from langchain_core.messages import BaseMessage
```

### 2.2 LangGraph 的核心概念（教程原话拆解）

> 它的核心是 `StateGraph`：你定义一个**状态类型**（`TypedDict` 或 Pydantic），
> 把每个节点写成「**拿 state、返回 partial state**」的函数，
> 再用 `add_edge` / `add_conditional_edges` **声明**节点之间的跳转关系，
> 编译出来的图就是一个可调用对象。

**四个概念对照**：

| 概念 | 具体是什么 | 本项目的对应物 |
| --- | --- | --- |
| 状态类型 | `TypedDict` 或 Pydantic | `RAGState`（`TypedDict, total=False`） |
| 节点 | 「拿 state、返回 partial state」的函数 | `async def retrieve(state) -> RAGState` |
| 边 | 声明跳转关系 | `add_edge` / `add_conditional_edges` |
| 编译产物 | 可调用对象 | `graph.compile()` |

### 2.3 ⭐ 教程列的三个"适合 RAG 场景"的特点

| # | 特点 | 为什么对本项目重要 |
| --- | --- | --- |
| ① | **节点函数本身和 LangGraph SDK 完全解耦**，单独跑 / 写测试都不需要图 | 前 6 期的 5 个节点**一行都不用改**就能当图节点用 —— 正因为它们本来就是 `async def xxx(state) -> dict`，**恰好就是 LangGraph 的节点规范** |
| ② | **条件边显式声明，图编译时就能校验「每条边都有去处」** | 不会出现 service 里手写 `if-elif` 时漏分支的情况 |
| ③ | **编译产物可复用**，无状态请求里直接 `ainvoke`，没有额外构造成本 | 图只编译一次，每次请求复用同一个编译产物 |

> **第 ① 条是本期的隐藏红利**：前几期坚持"节点只做纯逻辑、返回增量 dict"的写法，到这一期直接兑现 —— **不需要重写任何已有节点**。

---

## 三、第 2 章：两个配置项（`config.py` L168-179）

```python
    # ===== Agentic RAG =====
    # 关掉后图退化为单轮检索，作为单轮 vs agent 循环的对比开关。
    #   agent_loop_enabled=False 时整个图退化成「跑一轮就出图」，
    #   方便对比开 / 关 agent 循环的效果（与第 5 期 query_route_enabled 同一思路：
    #   给一条"退化回上一期行为"的退路，是排查"优化是否真的有效"的第一手段）。
    agent_loop_enabled: bool = True      # L173
    # 最大检索轮次（含首轮）。LLM 决策最多触发 max_rounds-1 次再检索，避免循环调用。
    #   【为什么必须是硬上限】：
    #   没有它，一个"永远觉得召回不够好"的决策器会让循环无限转下去，
    #   每轮一次 LLM 调用 + 两路检索，成本与延迟都会失控。
    #   有了它，最坏情况也有界：3 轮意味着最多 1 次首轮 + 2 次 LLM 决策。
    agent_max_rounds: int = 3            # L179
```

### 3.1 `agent_loop_enabled` 是什么性质

教程原话：

> **关掉后图退化为单轮检索，作为单轮 vs agent 循环的对比开关。**
> `agent_loop_enabled=False` 时整个图退化成「**跑一轮就出图**」，方便对比开 / 关 agent 循环的效果。

**它和第 5 期的 `query_route_enabled` 是同一套路**：

| 开关 | 置 False 的效果 | 服务于什么 |
| --- | --- | --- |
| `query_route_enabled`（第 5 期） | `route_query` **固定输出 `original`**，无条件退化回单路检索 | A/B 对比"Query 优化有没有用" |
| `agent_loop_enabled`（本期） | **只跑一轮就出图**，无条件退化回第 6 期的单链检索 | A/B 对比"agent 循环有没有用" |

**⚠️ 一个容易说错的点**：这类开关是「**无条件退化开关**」，**不是「失败兜底」** —— 它由**人为配置**触发，与运行时异常无关。两者目的相同（A/B 排查优化是否有效），但机制完全不同。

### 3.2 `agent_max_rounds = 3` 的语义拆解

教程原话：

> 最大检索轮次（**含首轮**）。LLM 决策最多触发 `max_rounds-1` 次再检索，避免循环调用。

**把 3 展开**：

| 轮次 | 调 LLM 吗 | 说明 |
| --- | --- | --- |
| 第 **1** 轮 | ❌ **不调** | 沿用 `route_query` 已算好的 `route` / `query` |
| 第 **2** 轮 | ✅ 第 **1** 次决策 | |
| 第 **3** 轮 | ✅ 第 **2** 次决策 | **最后一轮，只给 FINAL / REFUSE** |

```
3 轮 = 1 次首轮 + 2 次 LLM 决策 = 最多 3 次「两路并发检索」
```

**为什么"含首轮"这个措辞很重要**：如果把 3 理解成"3 次再检索"，那么实际轮次会变成 4 轮、调用 4 次检索。**"含首轮"把口径钉死为总轮数。**

**为什么必须硬上限**：没有它，一个"**永远觉得召回不够好**"的决策器会让循环无限转下去 —— 每轮一次 LLM 调用 + 两路检索，**成本与延迟都失控**。

---

## 四、第 3 章：扩展 `RAGState`（`rag_state.py` L63-73）

```python
    # --- 6. Agentic RAG 循环 (plan_retrieval / observe_context 产出) ---
    # agent_steps 每一项形如：
    #   {round, action, reason, route, query, retrieved_count, top_score}
    # 由 plan_retrieval 追加「决策」字段、observe_context 回填「观察」字段，
    # 避免同一轮分两条记录（否则轮次与观测的对应关系要靠下标去猜，极易错位）。
    agent_steps: list[dict]        # L68
    # 当前轮次（从 1 开始）。observe_context 用它和 agent_max_rounds 比较来判断是否收敛；
    # 注意它必须每轮自增，否则「轮次用尽」这个出口永远不触发 → 死循环。
    retrieval_round: int           # L71
    # observe_context 判定本轮候选是否足够；True 时图走向 END
    context_sufficient: bool       # L73
```

### 4.1 ⚠️ 字段名严格按教程（设计阶段我用的名字不同）

| 位置 | 设计阶段归档里用的 | **实际写入（按教程）** |
| --- | --- | --- |
| 轮次计数 | `rounds` | **`retrieval_round`** |
| 是否充分 | `sufficient` | **`context_sufficient`** |

**为什么必须字面一致** —— 这三个字段会被**四处**引用：

```
① RAGState 声明
② plan_retrieval 写入（agent_steps / retrieval_round）
③ prompt 输出契约（new_query / new_route）
④ observe_context 读取 + 回填（context_sufficient / top_score）
```

**名字对不上不会报错**（详见第七节 Q3 的实证），只会表现为**静默地读不到值**。

### 4.2 `agent_steps` 的项结构

```python
{round, action, reason, route, query, retrieved_count, top_score}
```

**七个字段的来源**：

| 字段 | 谁写 | 含义 |
| --- | --- | --- |
| `round` | `plan_retrieval` | 第几轮 |
| `action` | `plan_retrieval` | 本轮决策（首轮固定 `initial`） |
| `reason` | `plan_retrieval` | 决策理由（来自 planner 的 JSON） |
| `route` | `plan_retrieval` | 本轮生效的检索策略 |
| `query` | `plan_retrieval` | 本轮实际用于检索的查询词 |
| `retrieved_count` | **`observe_context` 回填** | 这一轮检了几条 |
| `top_score` | **`observe_context` 回填** | Top1 的相似度分 |

> 教程截图里的注释末尾被截断为 `top_sc...`，按「回填观察字段」的语义**补全为 `top_score`** —— 对应第 9 章官方原文的「Top1 多少分」。

### 4.3 ⭐ 「同一轮只留一条记录」的设计理由

教程原话：

> 由 `plan_retrieval` 追加「**决策**」字段、`observe_context` 回填「**观察**」字段，**避免同一轮分两条记录**。

**为什么这个决定重要 —— 对比两种写法**：

```
❌ 每轮两条记录（plan 追加一条 + observe 追加一条）：
   round 1 → steps[0] (决策), steps[1] (观察)
   round 2 → steps[2] (决策), steps[3] (观察)
   → 要定位"第 N 轮的观测结果"，必须算 steps[2N+1]
   → 【下标算术】一旦某轮 plan 追加了但 observe 提前退出，
     整个下标全部错位 —— 而且错位是静默的
     （拿到的仍是合法记录，只是轮次对不上）

✅ 每轮一条记录（先写决策字段，后补观察字段）：
   round 1 → steps[0]
   round 2 → steps[1]
   → 用 record["round"] 定位，不依赖下标
```

**核心区别在「定位方式」**：一条记录让"找第 N 轮"变成 **`round` 字段匹配**（稳）；两条记录就退化成 **`下标算术`**（脆）。

### 4.4 RAGState 的段号调整

新增第 6 段后，原来的两段顺延：

```
--- 5. 知识检索与熔断判断节点 (retrieve 节点产出) ---    L58
--- 6. Agentic RAG 循环 (plan_retrieval / observe_context 产出) ---  L63  ← 新增
--- 7. 大模型回答生成节点 (generate 节点产出) ---        L75  （原 6）
--- 8. 持久化后置落库节点 (chat_service 落库后回写) ---  L78  （原 7）
```

**字段总数：13 → 16。**

---

## 五、第 4 章：Planner Prompt（`prompts.py` L294-352）

### 5.1 三处改动

| 位置 | 行号 | 内容 |
| --- | --- | --- |
| 注释块 + 双花括号警告 | L294-303 | 说明决策器职责，并**专门警告 JSON 示例必须双写花括号** |
| `_AGENT_PLAN_SYSTEM` | L304-319 | system prompt |
| `_AGENT_PLAN_HUMAN` | L324-332 | human 模板（四个占位符） |
| `AGENT_PLAN_PROMPT` | L334-336 | `ChatPromptTemplate.from_messages([...])` |
| `build_agent_plan_messages` | L339-352 | 装配函数 |

### 5.2 system prompt 做的三件事（教程原话）

> - **显式列出四个 `action` 的含义**，避免模型自由发挥
> - **给出「策略选择建议」做启发式引导**，比如已经试过 `multi_query` 还没命中就直接 `refuse`
> - **强制输出单行 JSON，并给一条示例**，让模型对齐输出格式

**四个 action（L307-311）**：

```
- proceed:        当前候选已经足够回答问题，直接进入答案生成。
- rewrite_query:  当前 query 不够清晰 / 过于口语化 / 含指代，需要换一个表达再检索；必须给出 new_query。
- switch_route:   换一种检索策略。可选 new_route: original / rewrite / hyde / multi_query。
- refuse:         多轮都召回不到相关内容，知识库可能不覆盖，提前拒答。
```

**策略选择建议（L313-316）—— 这是 prompt 里最实用的部分**：

```
- 已经尝试过 rewrite 仍未命中     -> 试 hyde（抽象问题）或 multi_query（多角度）。
- 已经尝试过 multi_query 仍未命中  -> 试 refuse。
- 问题里包含明确实体 / 编号但都没检索到 -> 优先 refuse，避免无意义改写。
```

**⚠️ 第三条防的是"无意义改写"**：如果用户问的是一个明确型号 / 编号（`B200`、`DS-B200-2024`）而库里就是没有，**再怎么改写也召不回** —— 不如直接拒答，省掉一轮 LLM + 两路检索。

### 5.3 输出契约（L318-319）

```
只输出**单行 JSON**，键固定为 action / reason / new_query / new_route，缺失字段填 null。
示例: {"action": "rewrite_query", "reason": "原 query 含指代", "new_query": "差旅住宿标准", "new_route": null}
```

| 键 | 必需性 | 说明 |
| --- | --- | --- |
| `action` | **必需** | 四选一 |
| `reason` | 建议 | 决策理由（留痕 + 可排查） |
| `new_query` | 仅 `rewrite_query` | 新查询词 |
| `new_route` | 仅 `switch_route` | 新策略，取值 `original` / `rewrite` / `hyde` / `multi_query` |

**"缺失字段填 `null`"这个要求很重要** —— 这样解析时**四个键一定都在**，不用判"键是否存在"。**与第 6 期 `retrieval_meta` "保留 `None` 键"是同一个原则。**

### 5.4 human 模板的四个占位符（L324-332）

```
用户原始问题: {question}

当前 query: {current_query}
当前 route: {current_route}

历史轮次观察:
{history}

请输入下一步决策的 JSON。
```

**对应 `build_agent_plan_messages` 的四个入参**（L339-352）：

```python
def build_agent_plan_messages(
    question: str,
    current_query: str,
    current_route: str,
    history: str,
) -> list[BaseMessage]:
```

**为什么传 `question`（原始问题）而不只是 `current_query`**：改写后的查询可能已经跑偏，**原始问题才是"用户到底想要什么"的锚点**。

---

## 六、⭐ 写入过程中证实的一个坑：JSON 示例的花括号必须双写

### 6.1 现象

`ChatPromptTemplate` 把单个 `{xxx}` 当**变量占位符**。而 system prompt 里要放一段 **JSON 示例**，花括号天然冲突。

| 写法 | `from_messages()` | `invoke()` |
| --- | --- | --- |
| 示例用**单**花括号 | ✅ **能编译通过** | ❌ `KeyError: 'Input to ChatPromptTemplate is missing variables {'"action"'}` |
| 示例用**双**花括号 `{{ }}` | ✅ | ✅ 正常渲染为单花括号 JSON |

### 6.2 为什么这是最难查的一类错误

```
错误写法在【编译期】完全正常
  → 只在【每次调用】时才炸
  → 而调用发生在【用户提问那一刻】
```

**启动时不报错、单测不触发就不报错**，等上线后用户第一次提问才崩。所以我在 `prompts.py` L299-303 专门写了一段警告，并在写入前**先实测验证过**。

**规则**：

```
JSON 示例的花括号  →  双写 {{ }}
真正的变量占位符    →  单写 {question}
```

### 6.3 验证结果

```
AGENT_PLAN_PROMPT 消息数 = 2
模板变量 = ['current_query', 'current_route', 'history', 'question']     ← 四个占位符全被识别

system 渲染末尾: 示例: {"action": "rewrite_query", "reason": "原 query 含指代", ...}
  ✓ JSON 示例完好（单花括号）
  ✓ 无转义残留 '{{'

human 渲染: 用户原始问题：它的学分是多少 | 当前 query：... | 当前 route：original | 历史轮次观察：...
  ✓ 四个变量全部被正确替换
```

---

## 七、真实 LLM 验证：契约合规 4/4

把 prompt 直接送给真模型（`get_chat_model()`，1.8~2.3 秒 / 次），四个场景全部输出**合法单行 JSON、四键齐全、`action` 与 `new_route` 均在白名单内**：

| 场景 | 输入 | 模型输出 `action` | 是否符合预期 |
| --- | --- | --- | --- |
| ① 含指代 | 它的学分是多少 | `rewrite_query` | ✅ |
| ② rewrite 已试过仍未命中 | 怎么保证系统稳定 | `switch_route`（`new_route="hyde"`） | ✅ |
| ③ 两轮都没命中 | 公司年会在哪办 | `refuse` | ✅ |
| ④ 首位已高分（0.74） | 接口怎么认证 | `rewrite_query` | ⚠️ 见 7.2 |

### 7.1 模型输出的原文（节选）

```
① {"action": "rewrite_query", "reason": "原 query 含指代'它'，缺乏明确课程名称或实体…", "new_query": "课程学分", "new_route": null}
② {"action": "switch_route", "reason": "original 和 rewrite 均未突破语义相似度 0.45，需尝试生成假设性答案引导检索（hyde）…", "new_route": "hyde"}
③ {"action": "refuse", "reason": "连续两轮检索（original 和 multi_query）均未召回任何片段，表明知识库中很可能未收录…"}
```

**②③ 尤其值得注意**：模型的 `reason` **明确引用了 prompt 里的启发式规则**（"已经试过 rewrite → 试 hyde"、"已经试过 multi_query → refuse"），说明**策略选择建议是生效的**。

### 7.2 ⚠️ 但暴露了 prompt 的两个真实局限

**局限一：无论首位多高分，模型都不会主动说 `proceed`**

场景 ④ 首位相似度已达 **0.74**（明显达标），模型仍给了 `rewrite_query`。

**这其实符合教程的设计**（教程原话）：

> `proceed`：模型自己判断现有候选已经够回答了，跳出循环走生成。**兜底意义大于实际意义** —— 大部分情况 `observe_context` 已经判 `sufficient=True` 提前出去了

**所以这不是 bug，而是设计使然**：`proceed` 是"决策器抢在观测之前就认为够了"的兜底路径；**正常情况下应该由 `observe_context` 拦截**。

> **这条再次印证了 `observe_context` 的重要性**：它才是真正决定"停不停"的地方，`proceed` 只是保险。

**局限二：`rewrite_query` 给出的 `new_query` 并没有真正消解指代**

场景 ① 用户问"**它的**学分是多少"，模型给的新查询是 `"课程学分"` —— **仍然没有指出是哪门课**。

**根因**：`build_agent_plan_messages` 的入参里**没有对话历史**，只有 `question` / `current_query` / `current_route` / `history`（后者是**检索观察**，不是对话历史）。**模型根本没有语料来消解"它"。**

**这与第 5 期的已知缺口同源**：当时 `build_rewrite_messages(question)` 也不接收 `chat_history`，A/B 测试证明**有历史才能正确改写**（`JavaEE 课程的学分是多少`）。教程把 rewrite 的修复列在第 8 期。

**本批次不改 prompt**，但已记录为待办：等 `AgentPlanner` 实现完成后，把它与第 8 期的 rewrite 修复一起处理。

---

## 八、问题解析（本次五道自测题的详细批改）

### Q1　`agent_max_rounds = 3` 时最多几次 LLM 决策？几次两路并发检索？为什么第 1 轮不调 LLM？

**参考答案**：

```
LLM 决策  = 2 次（max_rounds - 1 = 3 - 1）
两路并发检索 = 3 次（每一轮都要检索，含首轮）
```

**第 1 轮不调 LLM 的真正原因**：**根本没有东西可给决策器看。**

```
第 1 轮时 agent_steps 还是空的
  → planner 的 human 模板里有 "历史轮次观察: {history}" 这个槽位
  → 但此时 history 为空，模型没有任何"上一轮为什么失败"的信息
  → 让它决策等于让它凭空猜
```

所以官方流程的第 1 条写的是：

> 首次进入 `plan_retrieval` 时 `agent_steps` 还是空，节点**不调 LLM**，直接沿用 `route_query` 给出的 `route` / `query`，只在 `agent_steps` 末尾追加一条 `action=initial` 的记录。

**⚠️ 一个容易说反的点**：不能把"省一次 LLM 花费"当成动机 —— 那是**结果**，不是**原因**。原因是**首轮无信息可依**。这个区别重要，因为它解释了为什么第 1 轮**必须**沿用 `route_query` 的结果（那是唯一可用的信息源），而不是"随便选一个策略省钱"。

**作答评价**：

| 项 | 结果 |
| --- | --- |
| 数字（2 / 2） | ⚠️ **第一个对，第二个错**：两路并发检索是 **3 次**（3 轮每轮都检索），不是 2 次 |
| 原因 | ⚠️ 方向对（"省一次 LLM"），但**把结果当原因**了 —— 真正原因是**首轮 `agent_steps` 为空、无观察信息可依** |

---

### Q2　`agent_loop_enabled = False` 时图退化成什么形状？和第五期哪个配置项是同一套路？

**参考答案**：

```
退化形状：整个图退化成「跑一轮就出图」
        → 等价于第 6 期的单链检索（normalize → route → retrieve → generate）
        → 没有循环、不会回到 plan_retrieval

同一套路：query_route_enabled（第 5 期）
```

**两者的共同点**：

| | 目的 | 机制 |
| --- | --- | --- |
| `query_route_enabled=False` | A/B 对比"Query 优化有没有用" | `route_query` **固定输出 `original`** |
| `agent_loop_enabled=False` | A/B 对比"agent 循环有没有用" | 只跑一轮就出图 |

**⚠️ 必须纠正的一个说法**：这类开关是「**无条件退化开关**」，**不是「失败兜底」**。

```
❌ "不符合要求就直接使用用户原 question 做兜底"
   → 这描述的是【运行时降级】（如 _safe_search 的 except）
   → 由异常触发，条件性

✅ "无条件退化回上一期行为"
   → 由【人为配置】触发，与运行时状态无关
```

**两者目的相同（都是 A/B 排查"优化是否有效"），但触发机制完全不同**，不能混为一谈。

**作答评价**：

| 项 | 结果 |
| --- | --- |
| 退化形状 | ✅ 正确（"退回第 6 期的单链检索"） |
| 同一套路 | ✅ 答出 `query` 相关开关 |
| 机制描述 | ⚠️ **把"无条件退化"说成了"失败兜底"** —— 这是本题唯一需要纠正的点 |

---

### Q3　写错 `retrieval_round` 字段名（比如写成 `state["rounds"]`）会报错吗？什么现象？

**⭐ 这是本次唯一答错的题，也是本批次最重要的知识点。**

**参考答案**：**不会报错，会静默返回 `None`。**

**实证（本批次实测）**：

```
.state.get('retrieval_round')  = 2        ← 正确字段名
state.get('rounds')           = None     ← 写错字段名：【没报错】，静默返回 None
state['rounds']               = KeyError ← 只有用 [] 下标才炸
```

**根因（必须理解这一层）**：

```
RAGState 是 TypedDict
  ├─ 字段声明（静态层）→ 【只有类型检查器】能看到
  └─ 运行时（动态层）  → 就是一个【普通 dict】
                          → .get(任何不存在的键) 永远返回 None，永远不报错
```

**而且真实节点必须用 `.get()` 而不是 `[]`**：因为节点是**增量返回**的，读一个"还没被任何节点赋过"的字段是**正常情况**，用 `[]` 反而会频繁 `KeyError`。

**这个 `None` 会继续向下传染**：

```
写错字段 → state.get("rounds") = None
         ├─ 若代码写 `if v >= max_rounds`  → TypeError（None 与 int 比较）
         └─ 若代码写 `if v is not None and v >= max_rounds`
              → v is not None 恒为 False
              → 【"轮次用尽"这个出口永不触发】
              → 死循环（每轮一次 LLM + 两路检索，请求永不返回）
```

**后一种更危险** —— 它正是本系列设计归档 §4.1 里预警的那个死循环。

> **结论**：`TypedDict` 的字段名写错，**编译不报、运行不报、只返回 `None`**。
> 唯一能提前发现的手段是**静态类型检查**（`mypy` / `pyright`）—— 而本项目**目前没有跑**，
> 所以只能靠**人工保证四处字段名字面一致**（声明 / 写入 / prompt 契约 / 读取）。

**作答评价**：

| 项 | 结果 |
| --- | --- |
| 会不会报错 | ❌ **答反了**：答"会报错"，实际**不会报错** |
| 现象 | ✅ 后半句对（"对应字段的最新数据填充失败，导致轮次出现问题"） |
| 遗漏 | ❌ 不知道 `TypedDict` 的运行时是普通 dict 这一层；也不知道会**传染成死循环** |

---

### Q4　为什么 `plan_retrieval` 和 `observe_context` 要**共用同一条** `agent_steps` 记录？

**参考答案**：

**不是为了"记录先后关系"，而是为了避免「下标算术」。**

```
❌ 每轮两条记录：
   round 1 → steps[0] (决策), steps[1] (观察)
   round 2 → steps[2] (决策), steps[3] (观察)
   → 定位"第 N 轮的观测结果"必须算 steps[2N+1]
   → 一旦某轮 plan 追加了但 observe 提前退出，【下标全部错位】
   → 而错位是静默的（拿到的仍是合法记录，只是轮次对不上）

✅ 每轮一条记录：
   → 用 record["round"] 定位，不依赖下标
```

**核心是「定位方式」**：

| 写法 | 定位第 N 轮的方式 | 稳健性 |
| --- | --- | --- |
| 每轮两条 | `steps[2N+1]` **下标算术** | ❌ 脆（少追加一条就全错） |
| 每轮一条 | `round == N` **字段匹配** | ✅ 稳 |

**"串联同一轮的决策与观测"是结果，不是理由** —— 用一个 `round` 字段就能串起来，关键收益是**摆脱下标算术**。

**作答评价**：

| 项 | 结果 |
| --- | --- |
| 方向 | ✅ 对（"是先后的逻辑串联"） |
| 理由 | ⚠️ **说成了"因为行为上要串联"**，漏了真正的工程理由：**避免下标算术、防止静默错位** |

---

### Q5　system prompt 里 JSON 示例的花括号写成单个会怎样？什么时候暴露？

**参考答案**：

```
单花括号 → ChatPromptTemplate 把它当成【变量占位符】
        → from_messages() 【编译期不报错】
        → 每次 invoke() 才抛：
          KeyError: 'Input to ChatPromptTemplate is missing variables {'"action"'}'
```

**⚠️ 「什么时候暴露」是关键**：

| 阶段 | 会不会暴露 |
| --- | --- |
| 写代码 | ❌ 不暴露 |
| `from_messages()` 编译 | ❌ **不暴露**（这是最坑的） |
| 应用启动 | ❌ 不暴露 |
| **用户第一次提问（`invoke`）** | ✅ **才抛 `KeyError`** |

**所以正确规则是**：

```
JSON 示例的花括号  →  双写 {{ }}
真正的占位符        →  单写 {question}
```

**作答评价**：

| 项 | 结果 |
| --- | --- |
| 本质 | ✅ 对（"会导致与字段自动匹配，不再是单纯的提示词"） |
| 什么时候暴露 | ⚠️ 未答 —— 应明确是「**延迟到 `invoke()`，即用户提问那一刻**」 |

---

### 批改汇总

| 题 | 结果 | 核心缺口 |
| --- | --- | --- |
| **Q1** | ⚠️ 一半 | 两路检索次数答成 2（应为 **3**）；把"省 LLM"当原因（真因是**首轮无观察可依**） |
| **Q2** | ⚠️ 一半 | 忘形对、开关对；把「**无条件退化**」说成「失败兜底」 |
| **Q3** | ❌ **错** | **`TypedDict` 字段名写错【不报错】，静默返回 `None`** —— 且会传染成**死循环** |
| **Q4** | ⚠️ 一半 | 方向对；漏了真正的理由「**避免下标算术、防止静默错位**」 |
| **Q5** | ⚠️ 一半 | 本质对；漏了「**延迟到 `invoke()` 才暴露**」 |

**总评：0 题满分、4 题半对、1 题答错。**

**两个必须补的硬伤**：

1. **Q3**：`TypedDict` 字段名写错**不报错、只返回 `None`** —— 这是本期最容易埋、最难查的一类 bug（且本系列已预警的"死循环"正是它的后果）。
2. **Q1 的数字**：`agent_max_rounds=3` 对应 **2 次 LLM 决策 + 3 次两路检索**（**每轮都检索**，含首轮）。

**三个需要精确化的措辞**：

| # | 说错的 | 应该说 |
| --- | --- | --- |
| ① | "省一次 LLM 花费"是首轮不调 LLM 的原因 | 原因是**首轮 `agent_steps` 为空、无观察信息可依** |
| ② | `agent_loop_enabled` 是"失败兜底" | 是「**无条件退化开关**」（人为配置触发，与运行时异常无关） |
| ③ | 共用一条记录是"因为先后关系" | 是为了「**避免下标算术**」，用 `round` 字段定位而非 `steps[2N+1]` |

---

## 九、当前状态

```
✅ 第 1 章 装 LangGraph（已装 1.2.11，无需改动）
✅ 第 2 章 配置项（agent_loop_enabled / agent_max_rounds）
✅ 第 3 章 扩展 RAGState（+3 字段，13 → 16）
✅ 第 4 章 Planner Prompt（system / human / 模板 / 装配函数）
────────────────────────────────────────────────────────────
⬜ 第 5 章 AgentPlanner 封装（教程截图已露出标题）
⬜ 第 6 章起 plan_retrieval / observe_context 节点
⬜ 组装 StateGraph + 接入 ChatService
```

**本批次无节点、无图** —— 所有"决策器的输入输出契约"已就位。

---

## 十、一句话总结

> 本期后端实现的第 1-4 步是**纯准备工作**：LangGraph 已装好（1.2.11），
> 新增两个配置项（`agent_loop_enabled` 是**无条件退化开关**，与第 5 期 `query_route_enabled` 同一套路；
> `agent_max_rounds=3` **含首轮**，对应 **2 次 LLM 决策 + 3 次两路检索**），
> `RAGState` 从 13 个字段扩到 16 个（`agent_steps` / `retrieval_round` / `context_sufficient`），
> 并写好决策器 prompt（显式四个 action + 策略选择建议 + 强制单行 JSON）；
> **写入过程中证实两个坑**：
> ① **JSON 示例的花括号必须双写** —— 单写时 `from_messages()` 能正常编译，
> **只在 `invoke()`（用户提问那一刻）才抛 `KeyError`**，是最难查的一类错误；
> ② **`RAGState` 是 `TypedDict`，字段名写错不会报错** ——
> 运行时它就是普通 dict，`.get()` 对任何不存在的键都返回 `None`，
> 而节点又**必须**用 `.get()`（因为是增量返回），
> 所以字段名不一致会**静默地读不到值**，并进一步传染成
> **「轮次用尽」出口永不触发的死循环**；唯一防线是四处字段名字面一致（声明 / 写入 / prompt 契约 / 读取）。
> **真实 LLM 验证契约合规 4/4**，并实测出两个 prompt 局限：
> **`proceed` 几乎不会被主动选中**（教程明确说它"兜底意义大于实际意义"，真正决定停不停的是 `observe_context`），
> 以及 **`rewrite_query` 无法消解指代**（因为入参不含对话历史，与第 5 期 rewrite 的缺口同源，教程列在第 8 期修）。

---

## 十一、关联文件索引

| 文件 | 关键位置 | 说明 |
| --- | --- | --- |
| `app/core/config.py` | L156-166 | 第 6 期混合检索配置（`rrf_k` L166） |
| `app/core/config.py` | **L168-179** | **Agentic RAG 配置（`agent_loop_enabled` L173 / `agent_max_rounds` L179）** |
| `app/workflows/rag_state.py` | L58-61 | 第 5 段：`retrieve` 产出（`refused` L61） |
| `app/workflows/rag_state.py` | **L63-73** | **第 6 段：Agentic 循环（`agent_steps` L68 / `retrieval_round` L71 / `context_sufficient` L73）** |
| `app/workflows/rag_state.py` | L65-67 | `agent_steps` 项结构注释（七字段） |
| `app/workflows/rag_state.py` | L75 / L78 | 顺延后的第 7 / 8 段 |
| `app/llm/prompts.py` | **L294-303** | **注释块 + 双花括号警告（L299-303）** |
| `app/llm/prompts.py` | **L304-319** | **`_AGENT_PLAN_SYSTEM`（四个 action L307-311 / 策略建议 L313-316 / 输出契约 L318-319）** |
| `app/llm/prompts.py` | L324-332 | `_AGENT_PLAN_HUMAN`（四个占位符） |
| `app/llm/prompts.py` | L334-336 | `AGENT_PLAN_PROMPT` |
| `app/llm/prompts.py` | L339-352 | `build_agent_plan_messages` |
| `app/llm/models.py` | L32 | `get_chat_model()`（本批次真实验证用的入口） |
| `app/llm/query_rewriter.py` | L96 / L128 / L138 / L149 | 其他 prompt 的调用范式（`ainvoke`） |

---

## 十二、待办与预警（传给后续章节）

| # | 事项 | 何时处理 |
| --- | --- | --- |
| ① | **`retrieval_round` 必须每轮自增** —— 否则「轮次用尽」出口永不触发 → **死循环** | 第 6 章 `plan_retrieval` / `observe_context` |
| ② | **四处字段名字面一致**（声明 / 写入 / prompt 契约 / 读取）—— 建议为这三个字段加一处集中常量 | 第 5-6 章 |
| ③ | **`new_query` 无法消解指代**（prompt 入参无对话历史） | 与第 8 期 rewrite 修复一起 |
| ④ | **`proceed` 几乎不会被选中** —— 正常路径由 `observe_context` 拦截 | 第 6 章 `observe_context` 的判据设计 |
| ⑤ | 关闭 `agent_loop_enabled` 后，`context_sufficient=False` 时该「带现有候选生成」还是「直接拒答」 | 第 5-6 章（官方未明确） |
