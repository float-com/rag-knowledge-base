# 05_ChatService接入图与AgentStep契约

> 章节：Day07_Agentic RAG · 第 3 章 后端实现 · **第 11-12 步**
> 覆盖：
> - **修复** 跨轮答案残留（`app/workflows/nodes/retrieve.py`）
> - 第 11 章 把 `ChatService.stream_answer` 接入图（`app/services/chat_service.py`）
> - 第 12 章 Schema 扩展 `AgentStep` + `MessageRead`（`app/api/schemas/chat.py`）
> 记录日期：2026.09.21

---

## 一、本批次定位

前一批编译出了图，但**没人调用它** —— `stream_answer` 还在手写 `await` 各节点。本批把图**真正接进服务层**，并把循环的决策轨迹**透出给前端**。

| 项 | 文件 | 改动 |
| --- | --- | --- |
| **修 bug** | `app/workflows/nodes/retrieve.py` | `answer` 改为"两分支都写" |
| **11** | `app/services/chat_service.py` | 改导入 + 新增 `_serialize_agent_steps` + `stream_answer` 四处改动 + 落库带轨迹 |
| **12** | `app/api/schemas/chat.py` | 新增 `AgentActionValue` / `AgentStep` / `_parse_agent_steps` + `MessageRead` 加字段 |

**行数**：`chat_service.py` 450 → **482**；`chat.py` 295 → **370**；`retrieve.py` 143 → **149**。

---

## 二、先修 bug：跨轮答案残留

### 2.1 现象（上一批实测发现）

```
query='学分是多少'
最终: refused=False  但  state["answer"] = "抱歉，知识库中没有找到与该问题相关的可靠依据。"
```

### 2.2 根因

`retrieve.py` 原本只在熔断时写 `answer`：

```python
    # 6. 若触发熔断，直接置入标准拒答文案，下游无需再调用大模型推理
    if refused:
        update["answer"] = REFUSAL_ANSWER
```

而 LangGraph 的状态是**跨轮累积**的：

```
第 1 轮  retrieve → refused=True  → 写 answer = 拒答文案
第 2 轮  retrieve → refused=False → 只写 refused=False，【不动 answer】
                                    → 第 1 轮的拒答文案留在 state 里
```

**这是"状态残留"的第三次出现**（第 7 章是 `multi_queries`，这里换成了 `answer`）。

**为什么第 11 章必须先修它**：服务层在非拒答路径会把 `state["answer"]` 当模型答案用（见 §3.3 的 `state["answer"] = "".join(answer_parts)` 之前那个分支）。

### 2.3 修法（`retrieve.py` L79-87）

```python
    # 6. 按拒答与否写入 answer（第 7 期修正：两个分支都要写）
    #    【为什么"不拒答"时也要显式写空串】：
    #    Agentic 循环里本节点会被执行多轮，而 LangGraph 的状态是【跨轮累积】的 ——
    #    若第 1 轮熔断写了拒答文案、第 2 轮不熔断却不覆盖它，
    #    这段上一轮的文案就会残留在 state 里，最终出现
    #    「refused=False 却带着拒答文案」的矛盾状态，
    #    而服务层在非拒答路径会把 state["answer"] 当模型答案用。
    #    所以不熔断时必须显式清空 —— 与 plan_retrieval 的 rewrite 分支"显式写 None 清残留"同一手法。
    update["answer"] = REFUSAL_ANSWER if refused else ""
```

**为什么写 `""` 而不是 `None`**：`RAGState.answer` 声明为 `str`，写 `""` 合类型；且服务层在非拒答路径会用 `state["answer"] = "".join(answer_parts)` 覆盖它，空串是安全的初值。

**实测确认修复**：

```
第1轮: refused=True   answer='抱歉，知识库中没有找到与该问题相关的可靠依据。'
第2轮: refused=False  answer=''                                        ← 已清空 ✓
```

---

## 三、第 11 章：把 `ChatService.stream_answer` 接入图

### 3.1 改导入（L46-49）

```python
from app.workflows.graph import get_rag_graph          # 新增
from app.workflows.nodes import load_context, stream_generate   # 只留两个
```

**为什么只留 `load_context` 与 `stream_generate`**：

| 节点 | 去留 | 理由 |
| --- | --- | --- |
| `load_context` | **留** | 唯一带 DB IO、需要 `AsyncSession` 的节点 |
| `stream_generate` | **留** | 要逐 token yield 给 SSE，与 `ainvoke` 的"一次拿完整结果"冲突 |
| `normalize_query` / `route_query` / `retrieve` | **移除** | 都已成为图内节点 |

### 3.2 新增 `_serialize_agent_steps`（L54-61）

```python
def _serialize_agent_steps(state: RAGState) -> list[dict]:
    """SSE / metadata 共用的 agent_steps 载荷格式。

    把每一条决策记录浅拷贝成新 dict 再下发，避免两种后果：
    ① 下游（前端或落库序列化）意外就地改动 state 里的原始记录；
    ② 后续节点继续追加字段时，与已发出的载荷共享同一份对象引用。
    """
    return [dict(step) for step in state.get("agent_steps", [])]
```

**两个要点**：

| # | 要点 |
| --- | --- |
| ① | **SSE 事件与落库共用同一个函数** —— 与 `_build_query_route_payload` / `_build_retrieval_meta` 同一原则，避免"实时展示"与"历史回看"格式各自演化 |
| ② | **浅拷贝 `dict(step)`** —— 不把 state 里的原始记录直接交出去 |

### 3.3 `stream_answer` 的四处改动

#### ① 三次手动调用 → 一次图调用（L278-289）

```python
                state.update(await load_context(state, session))

                # 3.1 图执行：加载上下文之后、检索之前。
                #     按最初设计，load_context 与 stream_generate 仍由 service 直接调用 ——
                #     前者需要 AsyncSession（唯一带 DB IO 的节点），后者要逐 token yield 给 SSE；
                #     两者都不适合放进图。图内只负责
                #     normalize_query → route_query → plan_retrieval → retrieve → observe_context
                #     这条带分支与循环的决策链路。
                #    【这不是"固定公式"】：从手写 await 三个节点收缩成一次 ainvoke，
                #     图里有几个节点、走了几轮，本层都不需要感知。
                final_state = await get_rag_graph().ainvoke(state)
                state.update(final_state)  # type: ignore[arg-type]
```

**⚠️ 这是本章的核心价值**：本层从"知道有哪三个节点、按什么顺序调"变成"**交给图，我不关心它怎么走的**"。

#### ② 新增 `agent_steps` 事件（L318-320）

```python
                # 6.1 下发 Agentic 循环的决策轨迹，供前端渲染"每一轮检索做了什么"。
                #     放在 query_route 之后：前端先拿到策略面板，再逐轮展开决策链。
                yield {
                    "event": "agent_steps",
                    "data": _serialize_agent_steps(state),
                }
```

**SSE 协议升级**：

```
原：message_start → query_route → citations → token(N) → message_end
新：message_start → query_route → agent_steps → citations → token(N) → message_end
```

#### ③ ⚠️ 删掉已由图接管的检索调用（L322-326）—— **教程截图遗漏的一步**

```python
                # 7. 检索与观察已由第 3.1 步的图执行完成 —— 原先这里手写的
                #    `state.update(await retrieve(state))` 已被删除：
                #    检索（retrieve）与观测（observe_context）都是图内节点，
                #    在 ainvoke 时一并跑完，本层不再需要、也无法单独调用它们。
                #
                # 8. 引用回填：ordinal 用 enumerate(start=1) 生成，与 prompt 中给模型的「片段 N」编号一致
```

**⚠️ 教程截图只画了 §3.3① 的替换（L272-275），没提这一处。**

**后果实测**（第一次端到端测试直接炸了）：

```
事件序列: message_start → query_route → agent_steps → error
token 数 = 0

Traceback (most recent call last):
  File ".../chat_service.py", line 327, in stream_answer
    state.update(await retrieve(state))
NameError: name 'retrieve' is not defined
```

**教训**：**替换一段代码时，要检查被替换掉的能力在**别处**是否还有残留调用点。** 这里 `retrieve` 从导入里移除后，下方还有个孤立的调用。

#### ④ 引用回填加拒答守卫（L334-347）

```python
                # 8. 引用回填：ordinal 用 enumerate(start=1) 生成，与 prompt 中给模型的「片段 N」编号一致
                #    【拒答路径不下发引用】：
                #    state["retrieved_chunks"] 可能还留着循环中间轮召回到的片段，
                #    但拒答本身就意味着"这些片段不足以作为依据"，
                #    因此拒答时不下发引用 —— 否则前端会展示出与实际结论相矛盾的"参考资料"。
                citations_payload = (
                    []
                    if state.get("refused")
                    else [
                        _serialize_citation(chunk, ordinal=i)
                        for i, chunk in enumerate(
                            state.get("retrieved_chunks", []), start=1
                        )
                    ]
                )
```

**⭐ 为什么这条守卫在 Agentic 循环下变成必需的**：

```
前六期（单轮）：拒答意味着"一条都没召回或相似度过低" → 引用列表本来就是空或很少，矛盾不明显
本期（多轮）  ：循环中间轮可能召回到一批尚可的片段 → 最后一轮仍判 refused
                → state["retrieved_chunks"] 是【非空】的
                → 若照发引用，前端会展示"参考资料"却告诉用户"没有找到依据"
                → 自相矛盾
```

### 3.4 落库带上决策轨迹（L445-451）

```python
            extra_metadata={
                # 拒答标志：历史回看时无需再推断
                "refused": bool(state.get("refused")),
                "query_route": _build_query_route_payload(state),
                # Agentic 循环的决策轨迹：与 agent_steps SSE 事件共用同一份序列化函数，
                # 保证"实时展示"与"历史回看"看到的是同一条链（与 query_route 同一原则）。
                "agent_steps": _serialize_agent_steps(state),
            },
```

---

## 四、第 12 章：Schema 扩展

文件：`app/api/schemas/chat.py`（295 → **370 行**）

### 4.1 新增 `AgentActionValue`（L224-226）

```python
# 决策动作字面量联合类型：
# 取值必须与 app.llm.agent_planner.AgentAction 一致，另加首轮的 "initial"
# （首轮不是 planner 给的，而是"沿用 route_query 决策"，因此单独列出）
AgentActionValue = Literal[
    "initial", "proceed", "rewrite_query", "switch_route", "refuse"
]
```

**注意比 `AgentAction` 多一个 `"initial"`**：

```
AgentAction（第 5 章，决策器能输出的） = proceed / rewrite_query / switch_route / refuse
AgentActionValue（第 12 章，状态里会出现的） = 上面四个 + initial
                                              ↑ 首轮的 action 不是决策器给的
```

**这是两个**不同层**的枚举** —— 决策器的输出空间 ⊆ 状态里可能出现的动作集合。

### 4.2 新增 `AgentStep`（L229-252）

```python
class AgentStep(BaseModel):
    """Agentic 循环单轮决策 + 观察快照。

    与 state 里 `agent_steps` 的 dict 结构【逐字段对应】：
    - `plan_retrieval` 先填决策字段（round / action / reason / route / query）；
    - `retrieve` 之后由 `observe_context` 回填观察字段
      （retrieved_count / top_score / sufficient）。
    前端 TypeScript 类型由 OpenAPI 自动生成，因此字段名不允许随意改。

    【观察字段为什么可空】：
    回填只发生在 `observe_context` 执行之后。若图在 `plan_retrieval` 的 refuse 分支
    直接结束（不经过检索与观察），这条记录就只有决策字段、观察字段为 None。
    这不是"缺数据"，而是如实表达"这一轮没有执行检索"。
    """

    round: int
    action: AgentActionValue
    reason: str
    route: QueryRouteValue
    query: str
    # 以下三个字段由 observe_context 回填；未执行检索时为 None
    retrieved_count: int | None = None
    top_score: float | None = None
    sufficient: bool | None = None
```

**实测的必填/可空划分**：

```
必填: ['round', 'action', 'reason', 'route', 'query']        ← 决策字段
可空: retrieved_count / top_score / sufficient                 ← 观察字段
```

**⭐ "观察字段可空不是缺数据，而是如实表达'这一轮没有执行检索'"** —— 这条与第 6 期 `retrieval_meta` "保留 `None` 键"、第 3 期 `agent_steps` "决策与观察同一条 dict"是**同一条原则的第三次应用**。

### 4.3 新增 `_parse_agent_steps`（L255-283）

```python
def _parse_agent_steps(metadata: dict | None) -> list[AgentStep] | None:
    """从 messages.metadata 解析 agent_steps：缺失 / 非法静默返回 None。

    与 `_parse_query_route` / `_parse_retrieval_meta` 同样的兜底风格，
    但多一层"逐条过滤"：agent_steps 是【数组】，不能因为其中一条脏数据
    就丢掉整条决策链。因此这里逐条校验、跳过非法项，而不是整体失败。
    """
    # 第一层：元数据本身缺失（老数据 / user 消息）
    if not metadata:
        return None
    raw = metadata.get("agent_steps")
    # 第二层：必须是【非空】列表 —— 空列表与 None 都视为"没有决策轨迹"
    if not isinstance(raw, list) or not raw:
        return None
    # 第三层：逐条校验，跳过不是 dict 的项，再交给 Pydantic
    parsed: list[AgentStep] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            parsed.append(AgentStep.model_validate(item))
        except Exception:
            # 单条非法只跳过这一条，不影响其余决策的展示
            continue
    return parsed or None
```

**⭐ 与另两个解析函数的关键差异**：

| 解析函数 | 数据结构 | 失败策略 |
| --- | --- | --- |
| `_parse_retrieval_meta` | 单个 dict | 整体返回 `None` |
| `_parse_query_route` | 单个 dict | 整体返回 `None` |
| **`_parse_agent_steps`** | **数组** | **逐条过滤**，跳过非法项、保留合法项 |

**为什么数组要逐条过滤**：一条脏记录不该让**整条决策链**消失 —— 用户会看到"这一轮检索记录突然空了"，而实际上只是某一条的结构不对。

**⚠️ 一个可能反直觉的设计**：`return parsed or None` —— **解析结果为空列表时返回 `None`**。理由：前端按 `agent_steps === null` 隐藏折叠面板；返回 `[]` 会让前端渲染一个**空面板**（"有轨迹但没内容"），而 `None` 表达的是"这条消息没有决策轨迹"。

### 4.4 `MessageRead` 加字段（L304-306 / L345-348）

```python
    # Agentic 循环决策轨迹：同样只挂在 assistant 消息上。
    # user 消息 / 旧消息 / 关闭 agent loop 时为 None，前端按缺失隐藏折叠面板。
    agent_steps: list[AgentStep] | None = None
```

```python
            # 决策轨迹同理：只取 assistant 消息的元数据，解析失败静默为 None
            agent_steps=(
                _parse_agent_steps(message.extra_metadata) if is_assistant else None
            ),
```

**复用 `is_assistant` 缓存**（第 6 期就有的优化）：引用过滤、`query_route`、`agent_steps` 三处都依赖它，只判断一次。

---

## 五、验证结果

### 5.1 跨轮答案残留：已修复

```
第1轮: refused=True   answer='抱歉，知识库中没有找到与该问题相关的可靠依据。'
第2轮: refused=False  answer=''                                        ← 已清空 ✓
```

### 5.2 `_serialize_agent_steps`

```
条数 = 2
浅拷贝验证: 是同一对象? False   （应为 False）✓
```

### 5.3 `_parse_agent_steps` 防御：11/11

| 输入 | 期望 | 实测 |
| --- | --- | --- |
| `None` | `None` | ✓ |
| 空 dict | `None` | ✓ |
| 没有 `agent_steps` 键 | `None` | ✓ |
| `agent_steps` 是空列表 | `None` | ✓ |
| `agent_steps` 非列表 | `None` | ✓ |
| 正常一条 | 1 条 | ✓ |
| **观察字段缺失（refuse 分支）** | **1 条** | ✓ |
| **含 1 条非法 + 1 条正常** | **1 条**（逐条过滤） | ✓ |
| 全是非法 | `None` | ✓ |
| **含非 dict 项** | **1 条**（跳过非 dict） | ✓ |
| `action` 非法值 | `None` | ✓ |

### 5.4 `MessageRead.from_orm` 三种情况

```
assistant + 有轨迹     : agent_steps = 1 条
user 消息              : agent_steps = None
历史 assistant（无轨迹）: agent_steps = None
```

### 5.5 OpenAPI 暴露

```
components.schemas 含 AgentStep ✓
  属性: ['round','action','reason','route','query','retrieved_count','top_score','sufficient']
  必填: ['round','action','reason','route','query']
MessageRead 含 agent_steps ✓
  类型: {"anyOf":[{"items":{"$ref":"#/components/schemas/AgentStep"},"type":"array"},{"type":"null"}]}
```

### 5.6 ⭐ 端到端 SSE：两条路径全通

**正常路径**（`接口怎么认证`）：

```
事件序列: message_start → query_route → agent_steps → citations → token ×26 → message_end
  round=1 initial  route=hyde  检了=5  top=0.7425  够=True
  citations = 5 条
  message_end: {"message_id":"b5f088ab-...","refused":false}
```

**拒答路径**（`学分是多少` —— 走满 3 轮）：

```
事件序列: message_start → query_route → agent_steps → citations → token ×1 → message_end
  round=1 initial        route=rewrite   检了=5  top=0.5244  够=False
  round=2 rewrite_query  route=original  检了=5  top=0.5778  够=False
  round=3 switch_route   route=hyde      检了=5  top=0.5316  够=False
  citations = 0 条
  message_end: {"message_id":"9138fd23-...","refused":true}
```

**⭐ 这一组同时验证了 4 件事**：

| # | 验证项 | 证据 |
| --- | --- | --- |
| ① | **闭环真的转** | 3 轮，且用了**两种不同的重试动作**（`rewrite_query` → `switch_route`） |
| ② | **拒答路径不下发引用** | `citations = 0`（而 `retrieved_chunks` 实际有 5 条） |
| ③ | **拒答不调用大模型** | `token = 1`（只有预置文案那一个增量） |
| ④ | **answer 残留已修** | 拒答文案正确、非拒答路径无残留 |

### 5.7 历史回看

```
content='接口认证方式如下：...'                          引用=5  agent_steps=1
content='抱歉，知识库中没有找到与该问题相关的可靠依据。'     引用=0  agent_steps=3
```

**`agent_steps` 通过 API 正确透出**（第 12 章的目标达成）。

---

## 六、⚠️ 本批发现的问题：教程截图遗漏了一步

**教程截图只覆盖了「三次手动调用 → 一次图调用」的替换，没有提到要删除下方原有的、单独的 `retrieve` 调用。**

**后果**：`retrieve` 从导入里移除后，`chat_service.py:327` 仍保留 `state.update(await retrieve(state))` →

```
NameError: name 'retrieve' is not defined
事件序列: message_start → query_route → agent_steps → error
token 数 = 0
```

**已补**：删除那段代码并写明注释。**上一次归档里"真跑图验证闭环转动"是直接调 `get_rag_graph()`，绕过了服务层**，所以没暴露这个问题 —— **这说明"单元级验证通过"不等于"接入层没问题"**。

**教训**：**替换一段代码时，要检查被替换掉的能力在别处是否还有残留调用点。**

---

## 七、当前状态与待办

```
✅ 教程第 1-12 章  全部完成
────────────────────────────────────────────────────────────
⬜ 前端：接收 agent_steps SSE 事件 + 折叠面板（教程第 12 章末提到"后面实现前端"）
```

| # | 待办 | 优先级 |
| --- | --- | --- |
| ① | 前端接收 `agent_steps` 事件 + 折叠面板 | — |
| ② | ⚠️ 前端 `MessageRead.agent_steps` 类型需重新生成（`npm run gen:api`）—— 但**受第 6 期那个"超前字段"问题影响，现在仍不能跑**，见 Day06 归档 §7.2 | 中 |
| ③ | `graph.py` 的 6 处 IDE 类型警告（第 9-10 章发现，结论：不管） | 低 |
| ④ | `new_query` 无法消解指代（prompt 入参无对话历史） | 中（与第 8 期 rewrite 修复一起） |

---

## 八、一句话总结

> 本批把图**真正接进服务层**，并**先修掉一个跨轮状态残留 bug**：`retrieve` 原本只在熔断时写 `answer`、
> 从不写空值，而 LangGraph 状态跨轮累积，导致第 2 轮达标后仍残留第 1 轮的拒答文案
> （`refused=False` 却带着拒答文案）—— 改为"**两个分支都写**"（不熔断时显式写 `""`），
> 与第 7 章 `rewrite_query` 分支"显式写 None 清残留"是**同一手法**（这是"状态残留"的第三次出现）；
> 第 11 章把 `stream_answer` 里手写的三次节点调用**收缩为一次 `ainvoke`**，
> 本层从此**不需要感知图里有几个节点、走了几轮**（`load_context` 与 `stream_generate` 仍留在服务层）；
> 新增 `agent_steps` SSE 事件（协议升级为 `message_start → query_route → agent_steps → citations → token(N) → message_end`），
> 并给引用回填加**拒答守卫** —— 因为多轮循环下 `retrieved_chunks` 可能非空，照发引用会与"没找到依据"的结论**自相矛盾**；
> 第 12 章新增 `AgentStep` 契约（决策字段必填、观察字段可空，**"可空不是缺数据，而是如实表达这一轮没执行检索"**）
> 与 `_parse_agent_steps`（与前两个解析函数的关键差异是**逐条过滤**：数组不能因一条脏数据丢掉整条链）；
> **端到端实测两条路径全通**：正常路径 5 条引用 / 26 个增量；拒答路径走满 3 轮、用两种不同重试动作、
> **0 条引用 / 1 个增量**（一次同时验证了闭环转动、拒答不发引用、拒答不调模型、残留已修）；
> **本批还发现教程截图遗漏一步**：只替换了三次调用、没删下方孤立的 `retrieve` 调用，导致 `NameError` 与 `error` 事件 ——
> **上一批"图能跑"是绕过服务层直调图验证的，说明"单元级验证通过"不等于"接入层没问题"**。

---

## 九、关联文件索引

| 文件 | 关键位置 | 说明 |
| --- | --- | --- |
| `app/workflows/nodes/retrieve.py` | **L79-87** | **answer 两分支都写（残留 bug 修复）** |
| `app/services/chat_service.py` | L46-49 | 导入：`get_rag_graph` + 只留 `load_context` / `stream_generate` |
| `app/services/chat_service.py` | **L54-61** | **`_serialize_agent_steps`（浅拷贝）** |
| `app/services/chat_service.py` | L278 | `load_context`（仍在服务层） |
| `app/services/chat_service.py` | **L280-289** | **图执行：`ainvoke` + `state.update`（L288-289）** |
| `app/services/chat_service.py` | L311-313 | `query_route` 事件 |
| `app/services/chat_service.py` | **L315-320** | **`agent_steps` 事件（新增）** |
| `app/services/chat_service.py` | **L322-326** | **原 `retrieve` 调用已删除（教程遗漏的一步）** |
| `app/services/chat_service.py` | **L327-345** | **引用回填（L327 注释 / L332-341 `citations_payload` / L334 拒答守卫 / L343 事件）** |
| `app/services/chat_service.py` | L420 | `_persist_assistant_message` |
| `app/services/chat_service.py` | **L445-451** | **落库带 `agent_steps`（L451）** |
| `app/api/schemas/chat.py` | **L224-226** | **`AgentActionValue`（5 值：4 个动作 + `initial`）** |
| `app/api/schemas/chat.py` | **L229-252** | **`AgentStep`（L244 `round` 起必填；L250 `retrieved_count` 可空）** |
| `app/api/schemas/chat.py` | **L255-283** | **`_parse_agent_steps`（L266 一层 / L270 二层 / L274-280 逐条过滤）** |
| `app/api/schemas/chat.py` | L289 / **L304-306** | `MessageRead` / `agent_steps` 字段 |
| `app/api/schemas/chat.py` | **L345-348** | `from_orm` 调用 `_parse_agent_steps` |
| `app/api/schemas/chat.py` | L190-215 | `_parse_query_route`（兜底风格参照） |
| `app/workflows/graph.py` | L128 / L131 | `_rag_graph` 单例 / `get_rag_graph()` |
| `app/workflows/nodes/plan_retrieval.py` | L128-131 | `refuse` 兜底写 `answer`（第 7 章） |
