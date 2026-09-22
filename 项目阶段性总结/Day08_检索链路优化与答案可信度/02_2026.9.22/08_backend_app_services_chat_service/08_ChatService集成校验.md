# 08_ChatService集成校验

> 期：**Day08 · 检索链路优化与答案可信度**
> 章：**教程第 8 章 · `ChatService` 集成 verify**
> 覆盖：
> - `backend/app/services/chat_service.py`（**改动**：本期改动最大的地方）
> - `backend/app/api/schemas/chat.py`（**改动**：两处 schema —— ⚠️ 教程截图未体现，见 §3）
> 记录日期：2026.09.22

---

## 一、本章定位

**教程第 8 章包含四个子项**：

```
8、ChatService 集成 verify
    ├ 把 rerank_score 加进检索元数据      ← _build_retrieval_meta
    ├ verify_result 载荷构造              ← 新增 _build_verify_payload
    ├ stream_answer 主流程                ← 本期改动最大
    └ _persist_assistant_message 写…      ← 落库 verify 结果
```

**教程原话**：

> `AnswerVerifier` 作为独立模块写好了，下面把它接进 `ChatService` 的 `stream_answer` 主流程。
> **这是本期改动最大的一个地方**，要在**流式 token 跑完后执行校验**，
> 校验失败就**替换成拒答文案**，最后把结果随 SSE 推给前端、**随消息落库**。

| 文件 | 改动 | 行数 |
| --- | --- | --- |
| `app/services/chat_service.py` | 4 处改动 + 1 个新函数 | 486 → **576** |
| `app/api/schemas/chat.py` | 2 处 schema | 377 → **426** |

---

## 二、⭐ 本章改动的三个来源（先说清"哪些是教程的、哪些是我加的"）

| # | 改动 | 来源 |
| --- | --- | --- |
| ① | `chat_service.py` 加 3 个导入 | ✅ **教程截图** |
| ② | `_build_retrieval_meta` 加 `rerank_score` 键 | ✅ **教程截图** |
| ③ | 新增 `_build_verify_payload` | ✅ **教程截图** |
| ④ | `stream_answer` 插入校验 + 调整落库顺序 | ✅ **教程截图** |
| ⑤ | `_persist_assistant_message` 收 `verify_result` | ✅ **教程截图** |
| ⑥ | `stream_answer` docstring 的事件协议 | 🟡 我补（教程只提了"事件协议"概念） |
| **⑦** | **`RetrievalMeta` 加 `rerank_score`** | ⚠️ **我加（教程未体现）** |
| **⑧** | **新增 `VerifyResultRead`** | ⚠️ **我加（教程未体现）** |
| **⑨** | **新增 `_parse_verify_result`** | ⚠️ **我加（教程未体现）** |
| **⑩** | **`MessageRead` 加 `verify_result`** | ⚠️ **我加（教程未体现）** |

### 2.1 ⚠️ 为什么补 ⑦~⑩：三条证据

**证据 1：前端类型里早就有这两个字段**（前端是 2026.9.4 预置的最终契约）

```
frontend/src/client/types.gen.ts:
    RetrievalMeta.rerank_score   ✓ L1100
    MessageRead.verify_result    ✓ L1016（类型 VerifyResultRead | null）
    VerifyResultRead             ✓ 已定义
```

**证据 2：前端真的在读它们**（决定性）

```
rerank_score 的消费者：
    CitationList.tsx:60    const rerankScore = c.retrieval_meta?.rerank_score

verify_result 的消费者：
    chatStream.ts:140-145  case 'verify_result': ... replacementAnswer: data.replacement_answer
    ChatPage.tsx:73        verifyResult: m.verify_result ?? null      ← 历史回看
    ChatPage.tsx:244-250   case 'verify_result': ... 【整段替换】
    ChatPage.tsx:499       message.verifyResult?.verified === false   ← 【隐藏引用】
    ChatPage.tsx:516-518   message.verifyResult?.verified === true    ← 显示绿色"已校验"标签
```

**不补的后果**：

```
❌ CitationList 的 rerankScore 永远 undefined → 精排分数在界面上【永远不显示】
❌ ChatPage:73 读到 null → 刷新历史后，校验标签与拒答替换态【全部消失】
   （实测过：补 schema 之前历史回看 verify_result = None）
```

**证据 3：第 9-11 章不涉及这两块**

```
第 9 章  ConversationRepository 列表与删除（消息计数 / 分页 / 删除 / 首问自动改标题）
第 10 章 ChatService 串接会话列表 / 删除
第 11 章 API 层补会话列表 / 删除（Schemas / 路由）
→ 全部围绕【会话 CRUD】，与 rerank_score / verify_result 无关
```

**⭐ 判定判据（本章固化，后续沿用）**：

> **本章教程正在做功能 F，而前端契约里有 F 的字段 → 补 schema（功能的必要组成）**
> **本章教程没做功能 G，而前端契约里有 G 的字段 → 不补（属于别的范围）**

### 2.2 ⭐ 前后端字段对齐表（本次核对结果）

**【RetrievalMeta】7 个字段全部对齐**

| 字段 | 前端 | 后端 |
| --- | --- | --- |
| `sources` | 有 | 有 |
| `vector_rank` | 有 | 有 |
| `vector_score` | 有 | 有 |
| `keyword_rank` | 有 | 有 |
| `keyword_score` | 有 | 有 |
| `rrf_score` | 有 | 有 |
| **`rerank_score`** | 有 | **有（本次补）** |

**【MessageRead】8 个字段全部对齐**

| 字段 | 前端 | 后端 |
| --- | --- | --- |
| `id` / `role` / `content` / `created_at` | 有 | 有 |
| `citations` | 有 | 有 |
| `query_route` | 有 | 有 |
| `agent_steps` | 有 | 有 |
| **`verify_result`** | 有 | **有（本次补）** |

**【⚠️ 仍超前、但不在本期范围的 3 个字段】**

| 字段 | 前端 | 后端 | 属于 |
| --- | --- | --- | --- |
| `trace_id` | 有 | **无** | `message_start` 事件（`chatStream.ts` 读 `data.trace_id`） |
| `trace_url` | 有 | **无** | 同上 |
| `cache_hit` | 有 | **无** | 同上 |

**⭐ 为什么不补这三个**：

```
本章教程【根本没做】链路追踪 / 缓存命中这件事
补它们等于自己发明功能，超出范围
→ 前端读到 undefined 是它自己在第 7 期就写好的容错分支（`?? null`）
```

**⚠️ 它们的性质与前两个不同**：

```
rerank_score / verify_result：本章教程正在做这件事（精排 → 校验），字段是它的产物
trace_id / cache_hit：本章教程没做这件事 → 待你后续决定是否实现
```

---

## 三、改动详解

### 3.1 `chat_service.py` 导入（L39 / L46 / L47）

```python
from app.core.config import settings                                    # L39
from app.llm.answer_verifier import VerifyResult, get_answer_verifier   # L46
from app.llm.prompts import REFUSAL_ANSWER                              # L47
```

### 3.2 `_build_retrieval_meta` 加 `rerank_score`（L133）

```python
        # RRF 融合分：两位小数不够用，故保留 6 位
        "rrf_score": (
            round(chunk.rrf_score, 6) if chunk.rrf_score is not None else None
        ),
        # 精排成对相关性分（第 8 期新增）：保留 4 位。
        #   为什么不用 6 位：它的值域是 [0,1]（不是 RRF 那种 0.0x 量级），
        #   4 位已能区分 0.3508 与 0.3509 这类相邻结果，再多位只是浮点尾数抖动。
        "rerank_score": (                                                # L133
            round(chunk.rerank_score, 4) if chunk.rerank_score is not None else None
        ),
```

**⭐ 与 `rrf_score` 的精度取舍形成对照**：

| 字段 | 值域 | 保留位数 | 为什么 |
| --- | --- | --- | --- |
| `rrf_score` | `0.0x` 量级（上限 0.0328） | **6 位** | 4 位会把 `0.0163` 与 `0.0164` 抹平 |
| `rerank_score` | `[0,1]` | **4 位** | 4 位已够区分相邻结果 |

### 3.3 新增 `_build_verify_payload`（L67-88）

```python
def _build_verify_payload(
    result: VerifyResult, *, replacement_answer: str | None
) -> dict:                                                              # L67
    """verify_result SSE / metadata 共用载荷。

    replacement_answer 仅在 verified=False 时携带：前端按它整段替换流式出来的答案，
    与 PRD"verify 失败 → 拒答替换"语义对齐。

    【为什么用关键字参数（`*`）】：
    replacement_answer 是可选且语义敏感的参数（它决定前端是否整段改文案），
    强制关键字传参能杜绝"位置传错把 reason 当 replacement"这类事故。
    """
    payload: dict = {                                                   # L79
        "verified": result.verified,
        # verified=True 时 reason 通常是空串，统一转成 None 下发：
        # 空串在 JSON 里没有信息量，null 更利于前端判断"没有理由可展示"。
        "reason": result.reason or None,
    }
    # 只有"校验不通过"才带替换文案 —— 通过时带一个 null 字段反而让前端多一次判断
    if not result.verified and replacement_answer is not None:           # L86
        payload["replacement_answer"] = replacement_answer
    return payload
```

**⭐ 三个设计点**：

| # | 设计 | 为什么 |
| --- | --- | --- |
| ① | **`*` 强制关键字传参** | `replacement_answer` 语义敏感（决定前端是否整段改文案），防位置传错 |
| ② | `reason or None` | 空串在 JSON 里没信息量，`null` 更利于前端判断"没理由可展示" |
| ③ | **只在失败时带 `replacement_answer`** | 通过时带个 `null` 字段反而让前端多一次判断 |

### 3.4 `stream_answer` 主流程（L271-450）

#### 事件协议更新（docstring L284）

```python
        【事件协议（与前端约定）】：
        message_start → query_route → agent_steps → citations → token…
            → [verify_result] → message_end
        - **拒答路径不发 verify_result**（拒答本身已经是终态，再校验一次毫无意义）；
        - `verify_result.verified=False` 时携带 `replacement_answer`，前端按它**整段替换**
          流式出来的答案，与 PRD"校验失败 → 拒答替换"对齐；
        - 任何阶段出错则 `yield error` 并提前结束。
```

**SSE 协议升级（第 8 期累计）**：

```
第 7 期：message_start → query_route → citations → token(N) → message_end
第 8 期：message_start → query_route → agent_steps → citations → token(N)
                     → [verify_result] → message_end
                     ↑ 第 7 期加的          ↑ 本期加的
```

#### 核心插入（L389-430）

```python
                # 9. 生成分支：拒答时直接把预置文案作为唯一 token 下发，完全不调用大模型
                verify_result: VerifyResult | None = None                # L389
                if state.get("refused"):                                 # L390
                    yield {"event": "token", "data": {"delta": state["answer"]}}
                else:
                    answer_parts: list[str] = []
                    async for delta in stream_generate(state):
                        answer_parts.append(delta)
                        yield {"event": "token", "data": {"delta": delta}}
                    state["answer"] = "".join(answer_parts)

                    # 10. 答案校验（第 8 期新增）：必须等 token 流跑完、拿到【完整答案】才能校验，
                    #     所以放在这里而不是放进 LangGraph 图。
                    #     【为什么只在非拒答路径执行】：拒答路径的 answer 本来就是标准拒答文案，
                    #     再拿它去校验毫无意义（还会白花一次模型调用）。
                    if settings.verify_answer_enabled:
                        verify_result = await get_answer_verifier().verify(   # L408
                            # 用 query 而不是 question：校验的是"这段回答有没有答到
                            # 实际检索/生成所依据的那个问题上"，与生成阶段的口径一致。
                            question=state["query"],
                            answer=state["answer"],
                            chunks=list(state.get("retrieved_chunks", [])),
                        )
                        replacement = (                                   # L418
                            REFUSAL_ANSWER if not verify_result.verified else None
                        )
                        if not verify_result.verified:
                            # 严格按 PRD：替换成统一拒答文案 + 标 refused
                            state["answer"] = REFUSAL_ANSWER              # L424
                            state["refused"] = True                       # L425
                        yield {
                            "event": "verify_result",                     # L427
                            "data": _build_verify_payload(
                                verify_result, replacement_answer=replacement
                            ),
                        }

                # 11. 落库助手回复 + 引用记录
                #     注意必须在【校验与替换之后】调用，否则落库的是替换前的旧答案。
                await self._persist_assistant_message(                    # L435
                    state, session, verify_result=verify_result
                )

                # 12. 收尾事件
                yield {
                    "event": "message_end",                               # L441
                    "data": {
                        "message_id": str(state["assistant_message_id"]),
                        # 这里用替换后的真实值：verify 失败会把 refused 置 True
                        "refused": bool(state.get("refused")),
                    },
                }
```

**⭐ 四处关键点**：

| # | 关键点 | 为什么 |
| --- | --- | --- |
| ① | **`verify_result` 在分支外初始化** | 拒答路径也要把它传给落库函数（值为 `None`） |
| ② | **校验只在非拒答路径执行** | 拒答的 answer 本来就是标准文案，校验毫无意义还白花调用 |
| ③ | **落库放在替换之后** | 否则落库的是**替换前的旧答案** |
| ④ | **`question=state["query"]`** | 校验的是"回答有没有答到**实际检索/生成依据的那个问题**"，与生成阶段口径一致 |

#### 落库顺序调整（对比教程 diff）

```diff
-                # 10. 落库助手回复 + 引用记录
-                await self._persist_assistant_message(state, session)
+                # 11. 落库助手回复 + 引用记录（在【校验与替换之后】）
+                await self._persist_assistant_message(
+                    state, session, verify_result=verify_result
+                )
```

**⭐ 这是"顺序即正确性"的一处**：

```
若落库仍在校验之前：
    校验失败替换了 state["answer"]
    但数据库里已经写入了【替换前的旧答案】
    → 落库内容与 state 不一致 → 前端拿到拒答文案、历史回看却是幻觉答案
```

### 3.5 `_persist_assistant_message` 收 `verify_result`（L497-540）

```python
    async def _persist_assistant_message(
        self,
        state: RAGState,
        session: AsyncSession,
        *,
        verify_result: VerifyResult | None = None,               # L502
    ) -> None:
        """...
        【verify_result 为什么默认 None 且用关键字传参】：
        拒答路径根本不会做校验（answer 本就是拒答文案），因此允许不传；
        用 `*` 强制关键字传参，避免与 RAGState / session 位置参数混淆。
        """
        conv_repo = ConversationRepository(session)
        citation_repo = AnswerCitationRepository(session)

        # 元数据先攒成变量，最后统一传给 make_assistant_message ——
        # 这样"要落哪些键"一眼可见（比在构造函数里内联一个多层 dict 更好审）
        extra_metadata: dict = {                                 # L524
            "refused": bool(state.get("refused")),
            "query_route": _build_query_route_payload(state),
            "agent_steps": _serialize_agent_steps(state),
        }
        if verify_result is not None:
            # verify_result 复用 SSE 的载荷格式，但 metadata【不需要】replacement_answer：
            # 它是"流式 UI 的特殊需求"（前端要拿它整段改文案），
            # 而落库的 answer 已经是替换后的最终文本，历史回看时再带一份重复文案没有意义。
            extra_metadata["verify_result"] = _build_verify_payload(   # L536
                verify_result, replacement_answer=None
            )

        assistant_msg = ConversationRepository.make_assistant_message(  # L540
            state["conversation_id"],
            content=state["answer"],
            extra_metadata=extra_metadata,
        )
```

**⭐ 为什么落库时 `replacement_answer=None`**：

```
它是"流式 UI 的特殊需求"（前端拿它整段改文案）
而落库的 content 已经是替换后的最终文本
    → 历史回看时再带一份重复文案没有意义
```

### 3.6 `api/schemas/chat.py` 的两处（⑦~⑩）

**① `RetrievalMeta.rerank_score`（L104）**

```python
    # 精排成对相关性分（第 8 期新增）：[0,1]，绝对值有意义，judge_context 判拒答的依据
    #   rerank 被短路或降级时为 None（此时 judge_context 回退比 vector_score）
    rerank_score: float | None = None
```

**并在类 docstring 里补了"量纲不同"的说明**：

```
    - rerank_score: 精排模型输出的成对相关性分（第 8 期新增），值域 [0,1]，
      **绝对值有意义**，是 judge_context 判定拒答的依据
      （注意它与 vector_score 虽同为 [0,1]，但【量纲不同、不可比】：
       0.35 的精排分已经算达标，而 0.35 的余弦相似度远不达标）
```

**② `VerifyResultRead`（L295-309）**

```python
class VerifyResultRead(BaseModel):                              # L295
    """答案校验结果快照（历史回放用）。

    与 SSE 的 verify_result 载荷【同一形状】，但 replacement_answer 在落库时是 None：
    它是"流式 UI 的特殊需求"（前端拿它整段改文案），而落库的 content 已经是替换后的最终文本，
    历史回看时再带一份重复文案没有意义。
    """

    verified: bool                                              # L306
    reason: str | None = None                                   # L307
    # 仅 SSE 场景携带；历史回看恒为 None（保留字段是为了与 SSE 载荷共用同一个模型）
    replacement_answer: str | None = None                       # L309
```

**③ `_parse_verify_result`（L312-332）**

```python
def _parse_verify_result(metadata: dict | None) -> VerifyResultRead | None:   # L312
    """从 messages.metadata 解析 verify_result：缺失 / 非法静默返回 None。

    与 `_parse_query_route` 同样的三层防御：
    ① 元数据本身缺失（老数据 / user 消息 / 拒答路径不做校验）
    ② 键不存在或不是 dict
    ③ 结构与契约不符 → 交给 Pydantic 拦截
    任何一种情况都静默返回 None，绝不因为一段可选元数据让整个历史接口报错。
    """
    if not metadata:
        return None
    raw = metadata.get("verify_result")
    if not isinstance(raw, dict):
        return None
    try:
        return VerifyResultRead.model_validate(raw)
    except Exception:
        return None
```

**④ `MessageRead.verify_result`（L356）+ `from_orm` 调用（L403）**

```python
    # 答案可信度校验快照（第 8 期）：拒答路径不校验，因此该字段为 None。
    # 【为什么必须是顶层字段】与 query_route / agent_steps 同一原因：
    # 前端读的是 m.verify_result；只存在 metadata 里而不在此暴露，刷新历史后校验结果就消失了
    verify_result: VerifyResultRead | None = None               # L356
```

```python
            # 答案校验快照同理：拒答路径不写该键 → 解析返回 None
            verify_result=(                                     # L403
                _parse_verify_result(message.extra_metadata) if is_assistant else None
            ),
```

**⚠️ 这一处与第 7 期 `agent_steps` 那次是同一个坑**：

```
数据库 metadata 里有 → 但响应模型不声明 → Pydantic 静默丢弃 → 前端拿不到
```

---

## 四、验证结果

### 4.1 schema 与 OpenAPI

```
RetrievalMeta 字段: [sources, vector_rank, vector_score, keyword_rank,
                     keyword_score, rrf_score, rerank_score]   ✓
MessageRead 字段:   [id, role, content, created_at, citations,
                     query_route, agent_steps, verify_result]   ✓
OpenAPI.RetrievalMeta 含 rerank_score: True     ✓
OpenAPI.MessageRead   含 verify_result: True    ✓
```

### 4.2 `_build_verify_payload` 四种组合

| 用例 | 结果 |
| --- | --- |
| 通过 + 无替换 | `{'verified': True, 'reason': None}` ✓ |
| 通过 + 有替换 | `{'verified': True, 'reason': None}` ✓（**不带替换**） |
| **不通过 + 有替换** | `{'verified': False, 'reason': '数字不符', 'replacement_answer': '抱歉…'}` ✓ |
| 不通过 + 无替换 | `{'verified': False, 'reason': '数字不符'}` ✓（**不带替换**） |

### 4.3 `_parse_verify_result` 防御：7/7

| 用例 | 结果 |
| --- | --- |
| `None` / 空 dict / 无该键 / 键非 dict / 缺 `verified` | `None` ✓ |
| 正常通过 | `verified=True, reason=None, replacement_answer=None` ✓ |
| 正常不通过 | `verified=False, reason='数字不符'` ✓ |

### 4.4 ⭐ 端到端：正常路径

```
'接口怎么认证'
  事件序列: message_start → query_route → agent_steps → citations
            → token → verify_result → message_end
  verify_result: {"verified": true,
                  "reason": "答案中所有关键事实（Bearer Token 认证方式、Authorization 头格式、
                             /api/v2/users/token 获取路径、access_token 2 小时有效期、
                             Refresh Token 30 天有效期）均直接来自片段 1 的原文，且引用 [1] 准确对应"}
  message_end: {"refused": false}
  引用 5 条；retrieval_meta 含 rerank_score = 0.3516 ✓
```

**⭐ 校验理由非常详细** —— 逐条列举了核对过的事实，**说明四条判定标准真的在被执行**。

### 4.5 ⭐ 端到端：校验失败替换路径（用桩逼出）

```
verify_result: {"verified": false, "reason": "桩：模拟数字幻觉",
                "replacement_answer": "抱歉，知识库中没有找到与该问题相关的可靠依据。"}
message_end: {"refused": true}          ← 已被置为 true ✓

流式 token 拼出的答案: '所有接口（除 `/api/v2/users/token` 外）须在 HTTP Header…'  ← 模型原答案
历史回看 content:      '抱歉，知识库中没有找到与该问题相关的可靠依据。'              ← 已替换 ✓
历史回看 verify_result: {'verified': False, 'reason': '桩：模拟数字幻觉', 'replacement_answer': None}
引用条数: 0    ← 因为 refused=True 跳过了引用落库 ✓
```

**⭐ 替换链完整**：`answer` 被覆盖 → `refused` 置 `True` → **落库的是替换后文本** → `message_end` 报 `refused=true`。

### 4.6 历史回看（补 schema 前后对比）

| | 补 schema 前 | 补 schema 后 |
| --- | --- | --- |
| `verify_result` | **`None`**（被 Pydantic 丢弃） | `{'verified': True, 'reason': '答案中所有关键事实…', 'replacement_answer': None}` ✓ |
| `retrieval_meta.rerank_score` | **缺失** | `0.3516` ✓ |

---

## 五、⚠️ 两处判断修正（记录，避免以后重犯）

### 5.1 我一度误判的"设计不一致"（**说早了**）

**我的原判断**：

```
校验失败被替换为拒答后：
    SSE 事件里 citations 已经发出去了（5 条）    ← 因为它在 token 之前
    但落库时 refused=True → 跳过引用落库 → 历史回看 0 条
    → 实时视图有引用、历史视图没有引用 → "设计不一致，需决策 A/B"
```

**实际情况**：**前端已经处理了这件事**：

```
frontend/src/pages/ChatPage.tsx:499
    message.verifyResult && message.verifyResult.verified === false
        → 【隐藏引用】

⭐ 所以后端【不需要】补发 citations: [] 事件
```

**⭐ 教训**：**判断"是不是问题"时，必须先查消费者，而不是只看后端一处。**

**我当时的失误**：看到"SSE 发了、落库没发"就下结论，**没先查前端怎么处理**。

### 5.2 我之前把 `citations 0 条` 当成异常

```
实测：verify 失败时"引用条数 0"  ← 这是【正确行为】
原因：refused=True → _persist_assistant_message 跳过引用落库
     （拒答消息本来就不该有引用 —— 这是第 7 期就定的约定）
```

---

## 六、本章结论

| # | 结论 |
| --- | --- |
| **①** | **校验必须等 token 流跑完**（要完整答案）→ 因此**不能进图**，只能留在服务层 |
| **②** | **校验只在非拒答路径执行** —— 拒答的 answer 本就是标准文案，校验毫无意义还白花调用 |
| **③** | **落库必须在校验与替换之后** —— 否则数据库里是替换前的旧答案，与前端展示不一致（"顺序即正确性"） |
| **④** | **`question=state["query"]`** —— 校验的是"回答有没有答到实际检索依据的那个问题" |
| **⑤** | ⚠️ **schema 必须跟着功能的产物走** —— 本章教程漏了 `RetrievalMeta.rerank_score` 与 `MessageRead.verify_result`，但**后端产出了却没有声明 → Pydantic 静默丢弃 → 前端拿不到** |
| **⑥** | **本章固化了一条判据**：本章教程正在做的功能，其字段要补；本章没做的功能（`trace_id`/`cache_hit`），不补 |

---

## 七、当前状态 —— **第 8 期全部完成**

```
✅ 教程第 1-8 章  全部完成
   第 1 章  需求分析与方案设计
   第 2 章  Reranker 客户端 + rerank 节点
   第 3 章  judge_context / refuse / 节点导出
   第 4 章  retrieve / plan_retrieval 删除拒答逻辑
   第 5 章  normalize_query 多轮上下文化
   第 6 章  重连工作流图
   第 7 章  AnswerVerifier
   第 8 章  ChatService 集成 verify（本章）
────────────────────────────────────────────────────────────
⬜ 教程第 9 章   ConversationRepository 列表与删除
                （消息计数 / 分页列表 / 删除 / 首次提问自动改标题）
⬜ 教程第 10 章  ChatService 串接会话列表 / 删除
⬜ 教程第 11 章  API 层补会话列表 / 删除（Schemas / 路由）
```

**教程本章末原话**：

> 后端链路从**召回 → 精排 → 拒答判定 → 生成 → 校验**的完整流程到这里就跑通了。
> 接下来去处理**会话列表、删除、首次提问自动改标题**。

**⚠️ 待办：3 个超前字段**（`trace_id` / `trace_url` / `cache_hit`）

```
前端 chatStream.ts 在 message_start 事件里读它们
后端目前只发 user_message_id
→ 属于"整个 Day08 都不做"的部分，待你决定是否实现
```

---

## 八、一句话总结

> 本章把 `AnswerVerifier` **接进服务层**，是本期改动最大的地方 ——
> 核心是在**流式 token 跑完之后、落库之前**插入校验：**必须等完整答案**（所以要留服务层不进图）、
> **只在非拒答路径执行**（拒答的 answer 本就是标准文案，校验毫无意义）、
> **校验失败就整段替换为拒答文案并把 `refused` 置 True**、
> **落库必须放在替换之后**（否则数据库里是替换前的旧答案 —— "顺序即正确性"）；
> 并新增 `_build_verify_payload`（SSE 与 metadata 共用载荷，用 `*` 强制关键字传参防位置传错，
> 只在失败时带 `replacement_answer`）、给 `_build_retrieval_meta` 补 `rerank_score`（保留 4 位，
> 与 `rrf_score` 保留 6 位形成对照）；
> **⭐ 本章还独立发现了教程遗漏的两处 schema**：`RetrievalMeta.rerank_score` 与 `MessageRead.verify_result` ——
> 教程截图完全没提，但**前端类型里早有定义、前端代码真的在读**（`CitationList:60` / `ChatPage:73,499,516`），
> 不补则**精排分在界面上永远不显示、刷新历史后校验状态全部消失**（实测：补之前历史回看 `verify_result=None`）；
> 并据此固化了一条判据：**本章教程正在做的功能，其字段要补；本章没做的功能（`trace_id`/`cache_hit`）不补**；
> **⚠️ 同时记录一处我自己的判断偏差**：我曾把"citations 已发出但落库 0 条"当成设计不一致要你决策，
> 实际上**前端已经按 `verifyResult.verified === false` 隐藏了引用** —— 教训是
> **判断"是不是问题"必须先查消费者，不能只看后端一处**；
> **实测验证**：`_build_verify_payload` 四种组合全对、`_parse_verify_result` 7/7、
> **端到端两条路径全通** —— 正常路径校验理由精确列举报核对过的事实，失败路径完整走通
> "覆盖 answer → 置 refused → 落库替换后文本 → message_end 报 true"。

---

## 九、关联文件索引

| 文件 | 关键位置 | 说明 |
| --- | --- | --- |
| `app/services/chat_service.py` | L39 | `from app.core.config import settings` |
| `app/services/chat_service.py` | L46 / L47 | `answer_verifier` 导入 / `REFUSAL_ANSWER` 导入 |
| `app/services/chat_service.py` | L57 | `_serialize_agent_steps`（第 7 期） |
| **`app/services/chat_service.py`** | **L67-88** | **`_build_verify_payload`（L79 payload / L86 替换条件）** |
| `app/services/chat_service.py` | L92 | `_build_retrieval_meta` |
| **`app/services/chat_service.py`** | **L133** | **`rerank_score` 键（保留 4 位）** |
| **`app/services/chat_service.py`** | **L271-450** | **`stream_answer`** |
| `app/services/chat_service.py` | **L284** | **事件协议 docstring（含 `[verify_result]`）** |
| `app/services/chat_service.py` | **L389-390** | **`verify_result` 初始化 / refused 分支** |
| **`app/services/chat_service.py`** | **L408-430** | **校验插入（L408 调用 / L418 replacement / L424-425 替换 / L427 事件）** |
| `app/services/chat_service.py` | **L435** | **落库调用（在替换之后）** |
| `app/services/chat_service.py` | L441 | `message_end` |
| **`app/services/chat_service.py`** | **L497-548** | **`_persist_assistant_message`（L502 签名 / L524 metadata / L536 写 verify_result）** |
| **`app/api/schemas/chat.py`** | **L68 / L104** | **`RetrievalMeta` / `rerank_score` 字段（带量纲说明）** |
| **`app/api/schemas/chat.py`** | **L295-309** | **`VerifyResultRead`（L306 verified / L307 reason / L309 replacement_answer）** |
| **`app/api/schemas/chat.py`** | **L312-332** | **`_parse_verify_result`（三层防御）** |
| **`app/api/schemas/chat.py`** | **L336 / L356 / L403** | **`MessageRead` / `verify_result` 字段 / `from_orm` 调用** |
| `app/llm/answer_verifier.py` | L63 / L130 | `verify` / `_parse_result`（第 7 章） |
| `app/core/config.py` | L208 | `verify_answer_enabled` |
| **前端（消费者，用于判定"字段是否真需要"）** | — | `CitationList.tsx:60` / `ChatPage.tsx:73,244,499,516` / `chatStream.ts:140-145` |
| **前端超前字段（未实现，待定）** | — | `trace_id` / `trace_url` / `cache_hit`（`chatStream.ts` 在 `message_start` 里读） |
