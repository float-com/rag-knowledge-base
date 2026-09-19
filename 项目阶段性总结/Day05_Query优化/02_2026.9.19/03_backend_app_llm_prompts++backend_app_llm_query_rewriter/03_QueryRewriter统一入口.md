# 03_QueryRewriter 统一入口

> 章节：Day05_Query 优化 · 第 3 章 后端实现 · 第 3 节
> 对应文件：`backend/app/llm/query_rewriter.py`（新增）、`backend/app/llm/prompts.py`（同步修正路由提示词）
> 记录日期：2026.09.19

---

## 一、本节要解决什么

前两节备好了**素材**（state 契约 + 配置 + 4 组 prompt），本节把素材**用起来**。重点不在"调通 LLM"，而在于封装：

```
上层（route_query 节点）只想知道一件事："这段提问该用什么检索词"
它不想知道：怎么调 LLM、怎么解析返回值、失败了怎么办
```

**QueryRewriter = 四种策略 + 全部失败处理，封成一个黑盒。**

---

## 二、`QueryRouteResult`：一个 frozen dataclass 承载四种策略

```python
@dataclass(frozen=True)
class QueryRouteResult:
    route: QueryRoute
    query: str
    rewritten_query: str | None = None
    hyde_answer: str | None = None
    multi_queries: list[str] | None = None
```

### 为什么 frozen

与 `RetrievedChunk` 同一理由（第 9 章）：

```
结果一旦产出就不该被下游随手改
→ 检索用的 query 若被某处意外覆盖，问题极难排查
→ frozen 让"意外修改"直接抛 FrozenInstanceError（实测已确认）
```

### 为什么返回 dataclass 而不是直接返回 state 增量 dict

```
dataclass  → 字段有类型、有 IDE 补全、可独立测试
dict       → 键名靠字符串约定，拼错不报错

职责分工：QueryRewriter 产出「强类型结果」
          route_query 节点负责翻译成「state 增量字典」（下一节的事）
```

### `route` 与 `query` 的关系（第 1 节"覆盖 query"设计的落地）

| route | `query` 字段的值 | 明细字段 |
| --- | --- | --- |
| `original` | 原问题 | 全 None |
| `rewrite` | **改写后的问句** | `rewritten_query` = 同一个值 |
| `hyde` | **假设答案** | `hyde_answer` = 同一个值 |
| `multi_query` | **原问题** | `multi_queries` = N 条子查询 |

**关键**：`query` 在四种策略下语义完全一致（"拿去检索的词"），明细字段只是补充说明。**这就是下游 `retrieve` 不需要懂策略的原因。**

---

## 三、四个策略方法 + 共用的 `_extract_text`

```python
async def decide_route(self, question: str) -> QueryRoute      # L88
async def rewrite(self, question: str) -> str                  # L111
async def hyde(self, question: str) -> str                     # L131
async def multi_query(self, question: str, n: int) -> list[str] # L141
```

**四者骨架完全一致**：

```
build_xxx_messages(question)  →  ainvoke(messages)  →  _extract_text(content)
```

### `_extract_text` 为什么必须存在（L66）

```python
def _extract_text(content: str | list[str | dict]) -> str:
    if isinstance(content, str):
        return content
    return "".join(part.get("text", "") for part in content if isinstance(part, dict))
```

**原因**：`AIMessage.content` 的类型是 `str | list[str | dict]`（为多模态设计）。本项目用纯文本模型，实际永远走 `str` 分支，**但类型检查器不认**。

> **这与 `generate.py` 里那段 `isinstance(text, str)` 守卫是同一个坑的两次出现**（第 9 章见过）——属于 LangChain 生态的通用问题，不是偶然。

### `decide_route` 的三层防御（L88-109）

```python
raw = _extract_text(response.content).strip().lower()
route = raw.strip('"').strip("'").strip()

if route not in VALID_ROUTES:
    logger.warning("query_route 模型返回非法结果，降级 original: raw=%r", raw)
    return "original"
return route
```

| 层 | 防什么 | 目标 |
| --- | --- | --- |
| ① `.strip().lower()` | `"Rewrite"` / `"rewrite\n"` / 首尾空白 | 提高成功率 |
| ② `.strip('"').strip("'")` | 引号包裹，如 `'"rewrite"'` | 提高成功率 |
| ③ `in VALID_ROUTES` 白名单 | 完全不相关的输出（`"the route is rewrite"` / `"1"` / `"multi"`） | **保证正确性** |

**为什么不能只留白名单**：`'"rewrite"'` 会被判非法 → 明明判对了却降级 → 白费一次 LLM 调用。**①② 与 ③ 的目标不同，都需要。**

> 对应第 2 章的结论：**提示词约束只是降低概率，代码校验才是确定性保证。**

---

## 四、`optimize()` —— 统一入口（L164）

```python
try:
    route = await self.decide_route(question)
    if route == "rewrite":     ...
    if route == "hyde":        ...
    if route == "multi_query": ...
    return QueryRouteResult(route="original", query=question)
except Exception:
    logger.exception("query_optimize 失败，降级 original: question=%r", question)
    return QueryRouteResult(route="original", query=question)
```

### 为什么需要 `optimize()`，而不是让节点自己分发

```
节点自己分发：
    route = await rw.decide_route(q)
    if route == 'rewrite': res = await rw.rewrite(q)
    elif ...
  代价：
    ① 分发逻辑散落在节点里 → 节点职责被污染
       （节点本该只管"往 state 里写什么"）
    ② 空结果校验也要在节点里写一遍 → 降级规则与策略实现被拆散
    ③ 新增第 5 种策略时，节点要加分支（route_query 本该零改动）

有 optimize()：
    节点只写一行：
        result = await get_query_rewriter().optimize(q, settings.multi_query_count)
    → 分发、校验、降级全在 QueryRewriter 内部闭环
    → 新增策略时节点零改动（正是第 1 节"覆盖 query"想要的效果）
```

> 与第 2 节 `build_*_messages` 同一思想：**把复杂性往上收，让调用方保持无脑。**

---

## 五、降级设计：五种情况、两种层级

```
业务级降级（optimize() 里显式判断）—— 依据是「结果内容」
  ① rewrite 返回空字符串     → 空 query 不能进 embedding
  ② hyde 返回空字符串        → 同上
  ③ multi_query 子查询 < 2   → 多路退化成单路，没有意义

技术级降级（校验与 catch 层面）—— 依据是「调用状态」
  ④ decide_route 返回非法值  → 白名单校验失败
  ⑤ 任何 Exception          → 捕获 + logger.exception + 降级
```

> **口诀：业务级看「值」，技术级看「调用」。**

### 一个容易看漏的细节：`multi_query` 的空结果

```python
queries = await self.multi_query(question, multi_query_count)
if len(queries) < 2:          # ← 同时覆盖"空列表"与"只有 1 条"
```

**没有单独写"queries 为空"的判断**——`len([]) < 2` 已为真。**一个条件覆盖两种退化。**

配合 `multi_query()` 内部先过滤空行：

```python
return [q for q in _extract_text(response.content).splitlines() if q.strip()]
```

**所以"模型返回 3 行但 2 行是空的"会被过滤成 1 条，进而触发降级** —— 第 2 章那条"空子查询不能进 embedding"约定的落地。

### `except Exception` 的两个细节

```
① logger.exception 而非 logger.error
   → 前者自动带完整堆栈；捕获的是未知异常，没堆栈就不知道为什么降级

② 没有 raise
   → decide_route 自己消化非法值，optimize 兜住漏网异常
   → 两层防线，任何一层漏掉的由另一层接住
```

---

## 六、⚠️ 实测发现的两种失败模式（本节最有价值的实证）

### 模式 A：技术性失败 —— 被降级机制正确接住 ✅

**首次运行时四种策略全部降级**：

```
query_route 模型返回非法结果，降级 original: raw='1'      ← 期望 original
query_route 模型返回非法结果，降级 original: raw='2'      ← 期望 rewrite
query_route 模型返回非法结果，降级 original: raw='3'      ← 期望 hyde
query_route 模型返回非法结果，降级 original: raw='multi'  ← 期望 multi_query
```

**根因**：路由提示词只列了**编号**、没列**策略名**，模型只能输出序号（详细分析见第七节）。

**降级机制在此的价值**：

```
若没有白名单校验 → '1' / '2' 会被当成合法策略名写进 state
                → 下游 retrieve 拿到不存在的策略 → 行为不可预测
有白名单校验     → 退到 original（基线），而不是崩溃或错乱
```

### 模式 B：业务性失败 —— 降级机制接不住 ⚠️

**修复路由提示词后**发现 `rewrite` 输出异常：

```
输入: 它的学分是多少    → 改写: 它的学分是多少        ← 原样返回！
输入: 那考核方式呢      → 改写: 那考核方式是什么呢     ← 补全了 ✓
```

**A/B 对照实验**（同模型、同 prompt，唯一差别是有无历史）：

```
A) 无历史 → 它的学分是多少
B) 带历史 → JavaEE 课程的学分是多少        ← 指代被正确解决
```

**为什么降级接不住**：

```
它不抛异常                      → except 接不住
它不返回空                      → `if not rewritten` 接不住
它返回一个"非空且合法"的字符串   → 被当成成功结果使用
→ 检索用"它的学分是多少"做向量化 → 召回质量差 → 全程零报错
```

> **核心认知**：
> **降级机制覆盖的是「技术性失败」；「业务质量失败」（结果合法但没用）永远不会触发任何降级，只能靠度量。**

**关于模式 B 的定性**：这是**教程已知的阶段性设计取舍**，不是实现缺陷。教程第六节「扩展」已明确记录：

> 当前 rewrite 路径只看本轮问题，没有把聊天历史传给改写 prompt……**在第八期我们会实现类似的策略。**

**代码中的处理**：已在 `rewrite()` 的 docstring 里写明这是"已知缺口 + 计划第八期实现 + 实测数据"，避免后续被误判为 bug。

**实测效果分层**：

| 场景 | 效果 | 原因 |
| --- | --- | --- |
| 补全省略（"那考核方式呢"） | ✅ 有效 | 结构不完整但信息自足，模型能补 |
| 消解指代（"它的学分"） | ❌ 无效 | 需外部上下文，prompt 里没有 |

---

## 七、路由提示词的修正（模式 A 的根因）

### 修正前（教程截图被裁掉导致的缺失）

```
1. 问题清晰、表达完整、用词具体（含专有名词 / 编号 / 实体），直接检索即可。
2. 问题存在指代（"它"、"这个"、"那"）、省略……需要改写。
3. 抽象 / 开放式……关键词稀疏，直接检索容易召回不到。
4. 问题包含多个角度……（如"对比 A 和 B"）。

只输出一个小写的 route 名称，不要加任何解释、引号或标点。
```

**问题**：四条判据**没有名称**，最后一句说"输出 route 名称"——**模型根本不知道 route 名称是什么**，只能按编号选。

### 修正后

```
1. original —— 问题清晰、表达完整、用词具体……
2. rewrite —— 问题存在指代……
3. hyde —— 抽象 / 开放式……
4. multi_query —— 问题包含多个角度……

只输出上述 4 个策略名之一（original / rewrite / hyde / multi_query），小写，不要加任何解释、引号或标点。
```

**修正效果**（真实调用）：

```
original     -> original     (1357ms)
rewrite      -> rewrite      (1362ms)
hyde         -> hyde         (3135ms)   ← 最慢（生成长文本）
multi_query  -> multi_query  (2138ms)
```

> **教训**：把策略名写进提示词，与第 2 节"给判据而非只给标签"是同一回事的延伸——**输出目标必须显式声明，否则模型只能猜**。

---

## 八、自测题与批改记录

| # | 题目 | 作答 | 判定 |
| --- | --- | --- | --- |
| 1 | `optimize()` 比"节点自己分发"好在哪（至少两点）？ | 「把整体操作合成一个接口，调用方不用知道内部实现；以后逻辑修改只需改这一个函数」 | ⚠️ 第②点不是理由 |
| 2 | 五种降级分成哪两类？分类依据？ | 「业务级与技术级；依据是策略成功触发但返回值违法/无用，与其他不知名异常导致的失败」 | ✅ |
| 3 | `rewrite`/`hyde` 返回空字符串为何要降级？ | 「空字符串检索返回无效信息或压根没有信息；不如用原始 question 进行兜底生成」 | ✅ 措辞需调 |
| 4 | `decide_route` 三层处理各防什么？为何不能只留白名单？ | 「提示词约束不代表模型一定不返回，这里是兜底策略」 | ✅ 可更细 |
| 5 | 两种失败模式里降级接住了哪种？为何接不住另一种？ | 「接住了代码硬编写的两类降级，接不住结果质量不达标，因为结果合法不会触发降级，但生成的答案是无效的」 | ✅ |

### 逐题批改

**第 1 题 —— 方向对，但第②点不是理由**

```
"改逻辑只改一处" 与有没有 optimize() 无关：
  即使节点自己分发，修改 rewrite 逻辑仍是只改 QueryRewriter.rewrite 一处
  → 分发逻辑本身没变

optimize() 真正的价值是把「编排」从节点收走：
  ① 分发逻辑不再散落在节点里（节点职责不被污染）
  ② 空结果校验与策略实现放在一起（降级规则不被拆散）
  ③ 新增第 5 种策略时节点零改动
```

**第 2 题 —— 正确**。口诀：业务级看"值"，技术级看"调用"。

**第 3 题 —— 正确，一处措辞调整**

```
❌ "使用原始 question 进行答案的兜底生成"
✓ "使用原始 question 作为检索词"

降级发生在**检索之前**（Query 优化是检索的前置步骤）
→ 降级后走的是"用原问题去检索"，不是"直接生成答案"
→ 没有跳过任何环节，只是退回基线（既不绕过引用，也不触发拒答）
```

**第 4 题 —— 正确**，三层分工详见第三节。

**实测补充**：首次测试时模型输出 `'1'`/`'2'`/`'multi'`，**三层都没拦住**——因为它们不是格式问题，而是提示词缺了策略名。

> **防御代码能兜住"格式不合规"，兜不住"模型根本不知道要输出什么"。**

**第 5 题 —— 正确**

精确表述：结果**合法但无效**——长度 > 0、非 None、在合法策略名下，**任何 `if` 都拦不住**。

---

## 九、本节两条结论

### 结论一：复杂性往上收，调用方保持无脑

```
QueryRewriter.optimize()  收走「分发 + 校验 + 降级」
build_*_messages()        收走「模板 + 插值 + 消息组装」
```

**「契约稳定性」思想在本期的第四次应用**：

| 层次 | 隔离对象 |
| --- | --- |
| 第 9 章 `normalize_query` | 查询怎么来 |
| 第 1 节 覆盖 `query` | 策略有哪些 |
| 第 2 节 `build_*_messages` | 提示词怎么写 |
| 第 3 节 `optimize()` | 策略怎么分发与降级 |

### 结论二：降级机制有明确边界

```
能兜：异常、非法值、空值、数量不足   → 有可判定的失败标志
不兜：结果合法但质量差               → 零报错，只能靠实测度量
```

---

## 十、验证记录

### 静态验证

```
1) py_compile                                  → exit 0 ✅
2) VALID_ROUTES = get_args(QueryRoute)         → ('original','rewrite','hyde','multi_query') tuple ✅
3) 方法签名                                     → 五个方法齐全 ✅
4) _extract_text 两分支                         → str 原样 / list 拼接 ✅
5) get_query_rewriter() 两次返回同一实例          → True ✅
6) QueryRouteResult 不可变                      → FrozenInstanceError ✅
7) 行宽                                         → 223 行，最长 88，超 100 者 0 ✅
```

### 真实 LLM 调用（修复提示词后）

```
original     | JavaEE 课程的课程编号是多少          -> original     (1357ms)
rewrite      | 它的学分是多少                      -> rewrite      (1362ms)
hyde         | 如何理解向量检索                    -> hyde         (3135ms)
multi_query  | 对比一下 HNSW 和 IVFFlat 的优缺点    -> multi_query  (2138ms)
```

**HyDE 最慢**（需生成 80–200 字长文本），multi_query 次之（生成 2 条子查询）。

**multi_query 的子查询质量抽查**：

```
对比一下 HNSW 和 IVFFlat 的优缺点
  sub[1] HNSW 和 IVFFlat 在近似最近邻搜索中的算法原理、时间复杂度与内存占用差异分析
  sub[2] HNSW 与 IVFFlat 在高维向量检索场景下的精度-召回率权衡、可扩展性及实际部署适用条件对比
```

**两条子查询角度确实不同**（一条偏"算法原理"，一条偏"精度-召回权衡与部署"），**没有退化成同义替换**——第 2 节那个"多路召回易失效点"在本例中未触发。

---

## 十一、一句话总结

> 本节实现 Query 优化的统一入口：以 frozen dataclass 承载四种策略的结果契约（`query` 在四种策略下语义一致，故下游无需懂策略）；四个策略方法共用 `_extract_text` 归一化（LangChain content 多模态类型的通用坑）；`decide_route` 用三层防御（大小写 → 引号 → 白名单）处理模型输出；`optimize()` 收走分发、校验与降级，使节点保持零改动；降级分业务级（看"值"）与技术级（看"调用"）两类共五种情况——**实测发现它可靠地兜住了技术性失败（模型输出序号），却完全兜不住业务性失败（rewrite 因缺少历史而无法消解指代，返回非空合法字符串，全程零报错）**。

---

## 十二、关联文件索引

| 文件 | 关键位置 | 说明 |
| --- | --- | --- |
| `app/llm/query_rewriter.py` | L39 | `logger` |
| `app/llm/query_rewriter.py` | L44 | `VALID_ROUTES = get_args(QueryRoute)` |
| `app/llm/query_rewriter.py` | L48-49 | `QueryRouteResult`（frozen dataclass） |
| `app/llm/query_rewriter.py` | L66 | `_extract_text`（content 归一化） |
| `app/llm/query_rewriter.py` | L85 | `QueryRewriter` 类 |
| `app/llm/query_rewriter.py` | L88 | `decide_route`（三层防御） |
| `app/llm/query_rewriter.py` | L111 | `rewrite`（含已知缺口说明） |
| `app/llm/query_rewriter.py` | L131 / L141 | `hyde` / `multi_query` |
| `app/llm/query_rewriter.py` | L164 | `optimize`（统一入口 + 五种降级） |
| `app/llm/query_rewriter.py` | L226 / L229 | `_rewriter` 单例缓存与 `get_query_rewriter` |
| `app/llm/prompts.py` | L179-186 | `_ROUTE_SYSTEM`（本次修正：补上策略名） |
| `app/llm/prompts.py` | L200 | `_REWRITE_SYSTEM`（缺 history 的根因所在） |
| `app/workflows/nodes/generate.py` | L40 | `isinstance(text, str)` 守卫（同款坑） |
