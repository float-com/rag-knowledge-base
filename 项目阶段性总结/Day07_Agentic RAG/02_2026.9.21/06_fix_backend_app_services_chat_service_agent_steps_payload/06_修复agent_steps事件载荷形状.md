# 06_修复agent_steps事件载荷形状

> 章节：Day07_Agentic RAG · 第 4 章 前端实现 · **跨端契约修复**
> 覆盖：`backend/app/services/chat_service.py`（一处）
> 记录日期：2026.09.21

---

## 一、问题现象

**前端 Agent 决策面板在"实时问答"时不显示，但刷新页面后又正常显示。**

| 场景 | 面板 |
| --- | --- |
| 新开对话、提问、**不刷新** | ❌ **不显示** |
| 刷新页面（从历史加载） | ✅ 正常显示 |

---

## 二、定位过程（三条链路实测对比）

### 2.1 抓原始 SSE 帧（修复前）

```
data: [{"round": 1, "action": "initial", "reason": "\u9996\u8f6e\u68c0\u7d22\u6cbf\u7528 route_q...
JSON.parse 后类型: list
parsed.steps = 【数组没有 .steps 属性】
前端 L132 解析结果 steps = []
→ 前端面板 【不会渲染】（空数组）
```

### 2.2 前端解析代码（`frontend/src/api/chatStream.ts` L131-133）

```typescript
          case 'agent_steps':
            onEvent({ type: 'agent_steps', steps: (data.steps ?? []) as AgentStep[] })
            break
```

**它读 `data.steps`** —— 期望事件载荷是 `{"steps": [...]}`。

### 2.3 后端发出的（修复前，`chat_service.py`）

```python
                yield {
                    "event": "agent_steps",
                    "data": _serialize_agent_steps(state),      # ← 裸数组
                }
```

### 2.4 三条链路对照

| 路径 | 载荷形状 | 前端解析 | 面板 |
| --- | --- | --- | --- |
| **实时 SSE** | 后端发**裸数组** | `data.steps` → `undefined` → `[]` | ❌ **不渲染** |
| **历史 API** | `MessageRead.agent_steps` **顶层字段** | `m.agent_steps ?? null` | ✅ 正常 |

**这就解释了"刷新后正常"**：历史路径的形状本来就是对的，只有实时路径是坏的。

---

## 三、⭐ 根因：**教程第 7 期第 11 章的一处笔误**

### 3.1 git 时间线（决定性证据）

| 项 | 提交 | 时间 | 内容 |
| --- | --- | --- | --- |
| **前端** `chatStream.ts` 读 `data.steps` | **`4dfe28b`** | **2026.9.4** | `2026.9.4初次提交：后端项目初始化与前端整体项目构建` |
| ├ 同一次提交的 `types.gen.ts` | 同上 | | **已有 `export type AgentStep`** 与 `agent_steps?: Array<AgentStep> \| null` |
| └ 同一次提交的后端 | 同上 | | **完全没有 `agent_steps`**（搜索为空） |
| **后端** `_serialize_agent_steps` | **`d7d1f0d`** | 本批 | 照教程截图写的 |

### 3.2 结论

```
2026.9.4  前端【整体构建】完成 —— 按"后端最终会发的契约"预置：
             · SSE 事件：{"steps": [...]}
             · 历史字段：MessageRead.agent_steps（顶层数组）

2026.9.21 教程第 7 期第 11 章才实现后端 —— 截图里写的是：
             "data": _serialize_agent_steps(state)      ← 裸数组，与前端契约【不符】
```

**所以这不是"哪一期留下的历史问题"，而是教程第 11 章那行代码本身与成品前端契约不符。**
**照教程逐字写就会产生这个不匹配。**

### 3.3 为什么它这么难发现

```
① 不报错：SSE 帧合法、JSON 合法、前端不抛异常
② 只在【实时路径】坏：刷新后走历史路径，形状对，面板正常
③ 现象是"某个面板不显示" —— 很容易被当成"前端没实现"或"条件没满足"
④ 我上一批的验证是【直接调 get_rag_graph()】，绕过了服务层，因此没暴露
```

---

## 四、修复

### 4.1 改动（`chat_service.py` L317-326）

```python
                # 6.1 下发 Agentic 循环的决策轨迹，供前端渲染"每一轮检索做了什么"。
                #     放在 query_route 之后：前端先拿到策略面板，再逐轮展开决策链。
                yield {
                    "event": "agent_steps",
                    # 【为什么包一层对象】与 citations 的 {"citations": [...]} 保持同一风格。
                    # 成品前端 chatStream.ts 的解析是 `data.steps`，若这里直接发裸数组，
                    # data.steps 会是 undefined → 前端拿到空数组 → 【实时对话时面板不渲染】
                    # （历史回看不受影响：MessageRead.agent_steps 是顶层字段、形状本来就对）。
                    "data": {"steps": _serialize_agent_steps(state)},
                }
```

**只改一行**：`"data": _serialize_agent_steps(state)` → `"data": {"steps": _serialize_agent_steps(state)}`。

### 4.2 为什么选择"改后端"而不是"改前端"

| 方案 | 改动 | 评价 |
| --- | --- | --- |
| **A 改后端** ✅ **采用** | 后端包一层对象 | **与 `citations` 的 `{"citations": [...]}` 风格统一**；且前端的契约在 2026.9.4 就定了，历史路径也用同一形状 |
| B 改前端 | `(data ?? []) as AgentStep[]` | 会变成"有的事件包对象、有的不包"，`chatStream.ts` 的解析风格不再一致 |
| C 发 `{"agent_steps": [...]}` | — | 键名与事件名重复 |

---

## 五、验证

### 5.1 实时 SSE 路径（**修复前失败、修复后通过**）

```
修复前：
  顶层类型: list
  前端 L132 解析结果 steps = []
  → 面板【不会渲染】

修复后：
  原始帧前 60 字符: {"steps": [{"round": 1, "action": "initial", "reason": "\u99
  顶层类型: dict  （应为 dict）✓
  前端 L132 解析出 steps = 3 条
  → 面板【会渲染】✓
     round=1 initial        route=original   count=5    top=0.4319
     round=2 switch_route   route=hyde       count=5    top=0.4502
     round=3 refuse         route=hyde       count=None top=None
```

**注意第 3 轮**（`refuse`）：`count=None / top=None` —— **拒答不执行检索，观察字段如实缺席**（这是第 3 章"决策与观察同一条 dict"设计的正确表现）。

### 5.2 完整往返（含落库与历史回看）

| 问题 | 事件序列 | 实时解析 | citations | token | refused |
| --- | --- | --- | --- | --- | --- |
| `上海天气怎么样` | `message_start → query_route → agent_steps → citations → token → message_end` | **3 条 → 渲染** | 0 条 | 1 | `true` |
| `接口怎么认证` | 同上 | **1 条 → 渲染** | 5 条 | 18 | `false` |

**历史 API 路径（回归）**：

```
role=user       agent_steps=None      引用=0
role=assistant  agent_steps=有 3 条    引用=0     ← 拒答：无引用
role=user       agent_steps=None      引用=0
role=assistant  agent_steps=有 1 条    引用=5     ← 正常：5 条引用
```

**两条路径现在完全对齐** ✅

### 5.3 无回归

```
citations 事件仍为 {"citations": [...]}  → 顶层 dict，5 条 ✓
query_route 事件、token 事件、message_end 均不受影响 ✓
```

### 5.4 一条提醒（本次测试脚本的坑）

**必须消费完整条流，助手消息才会落库**：

```
第一次测试脚本只读事件到 agent_steps 就 break
    → 流未消费完 → 服务层没走到落库那一步
    → 历史接口只有 3 条 user 消息、没有任何 assistant 消息
    → 我一度以为"历史路径回归失败"，实际是测试脚本没跑完
```

**这与之前"用 TestClient 测 SSE 导致 asyncpg 报错"是同一类问题**：**测流式接口时，测试脚本本身的行为会决定落库结果**。

---

## 六、⭐ 这是"成品前端预置了完整契约"的第 3 次出现

| # | 现象 | 发现于 | 前端写法 | 教程/实现写法 | 后果 |
| --- | --- | --- | --- | --- | --- |
| 1 | `rerank_score` | 第 6 期 | 类型里有（注明"第 8 章"） | 后端还没实现 | `gen:api` 会删掉它、导致前端编译失败 |
| 2 | `score` 的语义 | 第 6 期 | 按"相似度"展示 | 后端改成了 RRF 融合分 | 展示语义不符（数字与标签都不对） |
| 3 | **`agent_steps` 事件载荷** | **第 7 期** | **`data.steps`** | **教程发裸数组** | **实时面板不显示** |

**共同规律**：

```
前端是 2026.9.4 一次性按"整个项目的最终契约"构建的
    → 它对每一期的字段形状都有明确预期
    → 当教程某一章的写法与这个最终契约不一致时，照抄就会产生不匹配
    → 而这类不匹配【不报错】，只表现为"某个面板不显示"或"某个字段用不了"
```

**这条规律对后续几期的实践意义**：

| 遇到的情况 | 该怎么处理 |
| --- | --- |
| 教程写法与前端类型/解析不一致 | **以前端为准**（它是 2026.9.4 定的最终契约） |
| 前端类型里有"还没实现"的字段 | **不要跑 `gen:api`**（会删掉它们），等对应期实现完 |
| 某个面板/字段没生效 | **先抓原始 SSE 帧**，比对前端解析表达式，而不是先怀疑前端没实现 |

---

## 七、当前状态

```
✅ 教程第 1-12 章  后端全部完成
✅ agent_steps 跨端契约  已对齐（实时 + 历史两条路径）
────────────────────────────────────────────────────────────
⬜ 前端：本轮仅修复后端载荷；前端代码本身无需改动
```

| # | 待办 | 优先级 |
| --- | --- | --- |
| ① | 前端 `types.gen.ts` 重新生成（`AgentStep` 已在，但其余字段仍受"超前字段"问题影响，**现在仍不能跑 `gen:api`**） | 中 |
| ② | `AgentStepsPanel` 的 `proceed` 标签是「继续生成」；当 `proceed` 源自降级时 `reason` 会是 `planner_missing_route` 之类的技术字符串，面板上看着像模型主动决策 | 低 |
| ③ | `proceed` 之后循环仍继续（`_after_observe` 有最终裁决权）—— **这是设计**（教程原话"`proceed` 兜底意义大于实际意义"），不是 bug，但值得记 | 记录 |
| ④ | `ChatPage.tsx` 3 个已存在的 eslint 错误（不在 build 链里） | 低 |

---

## 八、一句话总结

> 实时问答时 Agent 决策面板不显示、刷新后又正常 —— 根因是**后端 `agent_steps` 事件发的是裸数组，而成品前端读的是 `data.steps`**；
> git 时间线证明**这不是哪一期的历史遗留**：前端在 **2026.9.4 的「前端整体项目构建」**里就已经在读 `data.steps`、
> 类型里也已有 `AgentStep`，而同一次提交的后端**根本没有 `agent_steps`** ——
> 是**教程第 7 期第 11 章那行 `"data": _serialize_agent_steps(state)` 与成品前端契约不符**，照抄就会踩到；
> 修复只改一处：**包一层 `{"steps": [...]}`**，与 `citations` 的 `{"citations": [...]}` 风格统一（选择改后端而非改前端，
> 因为前端契约在 2026.9.4 就定了、历史路径也用同一形状）；
> **验证覆盖两条路径**：实时路径从"解析出 0 条、面板不渲染"变成"解析出 3 条、面板渲染"（且 `refuse` 那轮观察字段如实缺席）；
> 历史路径回归正常（拒答 3 条轨迹 0 条引用、正常 1 条轨迹 5 条引用）——**两条路径完全对齐**；
> 这是**"成品前端预置了完整契约"的第 3 次出现**（前两次是 `rerank_score` 与 `score` 语义），
> 由此固化一条实践原则：**教程写法与前端类型/解析不一致时，以前端为准**；
> 且**测流式接口必须消费完整条流**，否则落库那一步不会执行（本次测试脚本因此一度误判"历史路径回归失败"）。

---

## 九、关联文件索引

| 文件 | 关键位置 | 说明 |
| --- | --- | --- |
| `app/services/chat_service.py` | **L317-326** | **`agent_steps` 事件（L318 事件名 / L323 `{"steps": ...}` 包装）** |
| `app/services/chat_service.py` | L54-61 | `_serialize_agent_steps`（浅拷贝） |
| `app/services/chat_service.py` | L288-289 | 图执行 `ainvoke` |
| `app/services/chat_service.py` | L338 / L352 | 拒答守卫（两处 `refused` 判断） |
| `app/services/chat_service.py` | L455 | 落库 `agent_steps` |
| `frontend/src/api/chatStream.ts` | **L131-133** | **前端解析：`data.steps`（契约来源）** |
| `frontend/src/api/chatStream.ts` | L27-29 | `ChatAgentStepsEvent` 接口（`steps: AgentStep[]`） |
| `frontend/src/components/AgentStepsPanel.tsx` | L24-25 | `steps.length === 0` 时返回 `null`（**这就是面板不显示的直接原因**） |
| `frontend/src/components/AgentStepsPanel.tsx` | L90-95 | 观察字段渲染（`!= null` 才显示） |
| `frontend/src/pages/ChatPage.tsx` | L235-236 | SSE 分支：`agentSteps: event.steps` |
| `frontend/src/pages/ChatPage.tsx` | L72 | 历史映射：`agentSteps: m.agent_steps ?? null` |
| `frontend/src/pages/ChatPage.tsx` | L475-477 | 渲染分支 |
| `app/api/schemas/chat.py` | L304-306 / L345-348 | `MessageRead.agent_steps`（历史路径字段） |
| git | `4dfe28b` | 前端整体构建（契约来源，2026.9.4） |
| git | `d7d1f0d` | 后端本批实现（问题引入） |
