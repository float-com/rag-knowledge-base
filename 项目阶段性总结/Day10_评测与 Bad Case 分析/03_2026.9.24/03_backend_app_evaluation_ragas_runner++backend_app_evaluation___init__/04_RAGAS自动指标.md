# 04_RAGAS自动指标

> 期：**Day10 · 评测与 Bad Case 分析**
> 章：第 3 章 后端实现 · **第 4 步「RAGAS 自动指标」**（含 4.1 封装 / 4.2 暴露对外接口）
> 记录日期：2026.09.24

---

## 一、本节在整条链路里的位置

> **前三步全是"自己动手"：自己建账本、自己出考卷、自己定判分标准。这一节第一次把"专业评委"请进来。**

```
第 1 步  新建评测表      ← 账本（已归档）
第 2 步  构建评测集      ← 考卷 + 读卡机（已归档）
第 3 步  人工指标与归因   ← 判分标准 + 病因诊断（已归档）
第 4 步  RAGAS 自动指标  ← 你在这里：接入第三方评估框架
第 5 步  评测入口        ← 让评测与线上走同一条链路
第 6 步  评测 Service    ← 执行器主流程：把 1~5 串起来
第 7 步  评测 API        ← 暴露成 8 个接口
```

| 步骤 | 分数从哪来 | 谁打分 |
| --- | --- | --- |
| 第 3 步（人工指标） | **规则算出来的** | 我们自己写的 `scoring.py` |
| **第 4 步（本节）** | **大模型当裁判打出来的** | **RAGAS 框架 + 一个 Judge LLM** |

> ⭐ **本节的真正身份是"适配器（Adapter）"** ——
> 把我们的业务对象翻译成 RAGAS 认识的形状，再把 RAGAS 的产物翻译回我们的类型。
> **它本身不做任何评估**，评估全在框架里。

---

## 二、本节与后续章节的**依赖关系**

```
   第 2 步（已归档）              第 4 步（本节）
   EvaluationCase                ragas_runner.py
   ├─ question ─────────────────► RagasSample.question
   ├─ expected_answer ──────────► RagasSample.reference_answer
   └─ ...                                     ▲
                                              │
   第 5 步（下一节）                           │
   评测入口（非流式跑一遍 RAG 链路）             │
   ├─ 检索到的切片 ─────────────► retrieved_contexts
   └─ 模型最终回答 ─────────────► answer
                                              │
                                              ▼
                              ┌───────────────────────────────┐
                              │  evaluate_batch(samples)      │
                              │      ↓                        │
                              │  RagasMetrics × N（等长）      │
                              └───────────────┬───────────────┘
                                              ▼
                        第 6 步 执行器：把 4 个分数喂给
                        第 3 节的 classify_bad_case(...)
                                              ▼
                        第 7 步 API + 前端看板
```

| 后续章节 | 依赖本节的什么 |
| --- | --- |
| 第 5 节 | 产出 `answer` / `retrieved_contexts` —— **本节的四个输入里，有两个由第 5 节提供** |
| 第 6 节 | **`evaluate_batch(samples)` 是执行器每批必调的一次**；返回的 `list[RagasMetrics]` **按下标**与 `samples` 一一对应，4 个分数直接灌进 `classify_bad_case` 的 4 个入参 |
| 第 7 节 | `types.gen.ts` 里 `EvaluationItemRead` 的 `faithfulness` / `answer_relevancy` / `context_precision` / `context_recall` 四列，就是本节这几个字段 |

### ⭐ 本节是 Day10 "None 哲学"的**第二环**

```
RAGAS 算不出来 → NaN  →  _pick 洗掉  →  None  →  第 3 节 _is_low(None) = False  →  不判坏例
```

| 环节 | 三态 | 出现位置 |
| --- | --- | --- |
| 第 3 节 | `citation_hit: bool \| None` | `None` = 该题本就该拒答，**不考引用** |
| **第 4 节** | **`RagasMetrics` 四个 `float \| None`** | **`None` = 没算出来，不是分数低** |
| 第 6 节 | 落库 `evaluation_items` | 前端按缺失友好呈现（显示 `-`） |

> 🎯 **一句话记住全篇**：**"没算出来 / 不适用" ≠ "表现差"。**

---

## 三、4.1 封装（`ragas_runner.py`）

### 3.1 模块清单

| 文件 | 行数 | 内容 |
| --- | --- | --- |
| **`backend/app/evaluation/ragas_runner.py`** | **329**（19289 B） | ⭐ 核心：2 个 frozen dataclass + 3 个函数 + 1 个指标列表 |
| `backend/app/evaluation/__init__.py` | 22（549 B） | **本节由 0 字节填充**：汇总 10 个出口 + `__all__`（见 §4） |

`ragas_runner.py` 的六个出口：

| 对象 | 位置 | 作用 |
| --- | --- | --- |
| `RagasMetrics` | **L25-54** | `@dataclass(frozen=True)`，**出参契约**（4 个 `float \| None`） |
| `RagasSample` | **L57-78** | `@dataclass(frozen=True)`，**入参契约**（4 个字段） |
| `_METRICS` | **L88-93** | 4 个 RAGAS 指标对象的编排列表 |
| `evaluate_batch(...)` | **L96-173** | 对外唯一入口（`async`） |
| `_extract_metrics(...)` | **L176-248** | 指标提取 + **长度对齐** |
| `_pick(row, key)` | **L251-298** | 单值清洗（**NaN 净化**） |
| `_empty_metrics()` | **L301-329** | 哑对象占位 |

### 3.2 ⭐ 设计要点一：**一进一出两个 dataclass，把 RAGAS 的"怪异形状"挡在边界外**

**`RagasSample`（入参）** —— 一个 RAG 问答生命周期的四个核心要素：**提问 → 检索 → 回答 → 黄金标准**

| 我们的字段 | 映射到 RAGAS 的 | 数据来源 | 生命周期 |
| --- | --- | --- | --- |
| `question` | `user_input` | 评测集 `question` | 提问 |
| `answer` | `response` | 第 5 节评测入口 | 回答 |
| `retrieved_contexts` | `retrieved_contexts` | 第 5 节评测入口 | 检索 |
| `reference_answer` | `reference` | 评测集 `expected_answer` | 黄金标准 |

**`RagasMetrics`（出参）** —— 正好对应第 3 节 `classify_bad_case` 的后 4 个入参：

| 我们的字段 | RAGAS 输出 key | 评估环节 | 第 3 节的归因 |
| --- | --- | --- | --- |
| `faithfulness` | `"faithfulness"` | 生成·事实性 | `generation_off_context` |
| `answer_relevancy` | `"answer_relevancy"` | 生成·指令遵循 | `prompt_constraint_weak` |
| `context_precision` | `"context_precision"` | 检索·排序 | `rerank_order_error` |
| `context_recall` | `"context_recall"` | 检索·召回 | `embedding_recall_miss` |

> ### 🎯 **"封装"两个字在本节的全部含义**
> 外面（第 6 节执行器）**只认这两个 dataclass** ——
> 它永远不需要知道 `SingleTurnSample`、`EvaluationDataset`、`result.scores` 长什么样。
> **RAGAS 的一切都在这个模块的边界内被吸收掉了。**

**⚠️ 一个必须逐字匹配的细节**（**L232-236**）：`_extract_metrics` 里的四个 key 是 **RAGAS 官方的字典协议**，
必须严格照抄。源码注释专门提醒：**别把 `answer_relevancy` 拼成 `answer_relevance`** ——
拼错了不会报错，只会**整列静默变成 `None`**。

### 3.3 ⭐ 设计要点二：`evaluate_batch` 的**三件保险**

```python
async def evaluate_batch(samples: list[RagasSample]) -> list[RagasMetrics]:
```

| # | 保险 | 位置 | 防止什么 |
| --- | --- | --- | --- |
| **1** | **`asyncio.to_thread`** | **L150-158** | RAGAS 的 `evaluate()` 是**同步阻塞**函数。直接在 `async def` 里调它会**卡死整个单线程事件循环**，FastAPI 全站跟着卡住 |
| **2** | **整批 `try/except`** | **L159-164** | API 彻底超时 / 网络断开等**未捕获的硬错误** → 记录堆栈，返回**等长的全 `None` 列表** |
| **3** | **`_extract_metrics` 长度对齐** | **L173** | RAGAS 丢件导致返回行数变少 → **主动补齐**，绝不让外部下标错位 |

**为什么"空引用直接返回"之外还要三个兜底？**（**L113-115**）
`if not samples: return []` 是**零成本短路** —— 免得为一个空列表白起一个线程、白初始化一次 RAGAS。

#### 2.1 `or " "` 三处空格占位（**L126-136**）

| 字段 | 兜底写法 | 为什么 |
| --- | --- | --- |
| `response` | `s.answer or " "` | 模型拒答 / 回答为空串时，RAGAS 会当成异常数据 |
| `retrieved_contexts` | `s.retrieved_contexts or [" "]` | 检索彻底失败（空列表）时，算忠实度/召回率会**除以 0 或越界闪退** |
| `reference` | `s.reference_answer or " "` | 拒答类用例**本来就没有标准答案** |

> 💡 **用"一个空格"而不是空字符串**是个不起眼但关键的选择：
> 空字符串在很多框架里仍然会被判为"空"，而空格是**合法的非空文本**，能让打分流水线走完。

#### 2.2 🔴 `raise_exceptions=False` 是**本节最关键的一个参数**（**L156**）

| 取值 | 单条 case 打分失败时 | 后果 |
| --- | --- | --- |
| `True` | 抛异常 | **整批 50 条全废**，一条坏样本毁掉一整轮评测 |
| **`False`（本项目）** | RAGAS 内部**静默记成 `NaN`** | 只有那一条降级，**其余 49 条照常出分** |

**它和 `_pick` 是一对搭档**：`raise_exceptions=False` 负责"**不让异常逃出来**"，`_pick` 负责"**把留下的 `NaN` 洗成 `None`**"。**少任何一半，这条降级链就断了。**

`show_progress=False`（**L157**）则是关掉 tqdm 进度条，避免刷屏污染日志。

### 3.4 ⭐ 设计要点三：`_extract_metrics` 的**长度对齐契约**（L176-248）

#### 3.1 安全反射取数（**L202**）

```python
rows = getattr(result, "scores", None) or []
```

两步防御：
1. `getattr(..., "scores", None)` —— RAGAS 不同版本的返回结构变过（`.scores` / `.to_pandas()` / 直接是 dict），**用反射避免第三方库升级就 `AttributeError` 当场崩**；
2. 末尾 **`or []`** —— `scores` 存在但值是 `None`（评分全失败）时，`None or []` 收敛成空列表，保证后面 `len(rows)` 和切片绝对安全。

#### 3.2 🔴 架构关键：**以 `expected` 驱动循环，绝不写 `for row in rows`**（**L220-227**）

```python
for i in range(expected):                      # ✅ 以外部预期为准
    row = rows[i] if i < len(rows) else {}     # 越界 → 空字典 → 全 None 的"哑行"
```

| 写法 | 结果列表长度 | 外部按下标对账 |
| --- | --- | --- |
| `for row in rows` | **不确定**（RAGAS 丢几条就短几条） | ❌ 错位 / `zip` 丢数据 |
| **`for i in range(expected)`（本节）** | **恒等于 `expected`** | ✅ 绝对可靠 |

> ⭐ **这一行是整个模块对外的"硬承诺"**：
> **`evaluate_batch` 返回的 list，长度在物理上必然等于 `samples` 的长度。**
> 第 6 节执行器之所以能放心"按下标对账"，全靠它。

#### 3.3 失配只告警、不打断（**L210-213**）

```python
if len(rows) != expected:
    logger.warning("RAGAS 返回行数 %d 与样本数 %d 不一致，按可用值对齐", ...)
```

**记 Warning 是为了可观测**（方便排查线程池吞任务、异步任务静默失败、丢包），
**但不抛异常** —— 因为"少几条"是可以优雅降级的，没必要让整轮评测失败。

### 3.5 ⭐ 设计要点四：`_pick` 的**三关清洗 + NaN 黑洞**（L251-298）

```python
value = row.get(key)                    # 第一关：键缺失 / 显式 None
if value is None: return None
try:    as_float = float(value)         # 第二关：类型强转（挡 "N/A" 之类脏字符串）
except (TypeError, ValueError): return None
if as_float != as_float: return None    # 第三关：NaN 净化
return as_float
```

#### 🔴 **为什么必须把 NaN 洗掉？**（**L286-289**）

> **NaN 是"浮点黑洞"** —— 任何带 NaN 的大小比较**永远返回 `False`**：
> ```python
> nan < 0.5    # False
> nan >= 0.5   # False
> ```
> 如果放它过去，第 3 节 `_is_low` 里那句 `if score < 0.5:` **永远不会成立** ——
> **低分报警直接失效，故障静默漏报。**

**NaN 从哪来**（**L282-284**）：
1. **除以零** —— 算 `context_recall` 时标答关键词数为 0，分母为 0；
2. **无效数据占位** —— NumPy/Pandas 处理缺失值、或大模型打分格式解析失败时的默认填充。

#### 为什么用 `as_float != as_float` 而不是 `math.isnan`（**L291-293**）

在 IEEE 754 规范里，**NaN 是唯一一个"不等于自己"的值**（`NaN != NaN` 恒为 `True`）。
利用这个底层特性判定，**不用额外 `import math`，更快也不会有类型意外**。

### 3.6 设计要点五：`_empty_metrics` —— "哑对象"（L301-329）

整批失败时返回**等长的全 `None`** 而不是空列表：

| 若返回空列表 | 本节做法 |
| --- | --- |
| 第 6 节 `zip(samples, metrics)` **直接丢数据** | 长度对齐，**逐条**落库为"本轮没算出来" |
| 外部按下标取值 **`IndexError`** | 安全取到 `None` |

**和 `RagasMetrics` 的 `frozen=True` 是同一套思路**：机评结果是"事实镜像"，**不允许业务层在内存里就地篡改**（**L35-37**）。

---

## 四、4.2 暴露对外接口（`__init__.py`）

本节把**上一节留空的 `app/evaluation/__init__.py`（0 字节）**填上，成为整个 `evaluation` 包的唯一门面：

```python
from app.evaluation.dataset import EvaluationCase, list_datasets, load_dataset
from app.evaluation.ragas_runner import RagasMetrics, evaluate_batch
from app.evaluation.scoring import (
    BadCaseCategory, BadCaseRule, classify_bad_case,
    compute_citation_hit, compute_refusal_correct,
)

__all__ = [ ... 10 个名字，按字母序 ... ]
```

**10 个出口 = 三步的产物汇总**：

| 来自哪一节 | 出口 |
| --- | --- |
| 第 2 步 `dataset.py` | `EvaluationCase` / `list_datasets` / `load_dataset` |
| 第 3 步 `scoring.py` | `BadCaseCategory` / `BadCaseRule` / `classify_bad_case` / `compute_citation_hit` / `compute_refusal_correct` |
| **第 4 步 本节** | **`RagasMetrics` / `evaluate_batch`** |

> 💡 `__all__` **按字母序排列**（不是按来源分组）—— 排序是为了让后续 diff 干净、避免重复或漏项。

---

## 五、运行验证（真实执行，非纸面推导）

针对**当前 329 行版本**重跑，**21 项全部通过**：

| 检查组 | 项数 | 结果 |
| --- | --- | --- |
| **4.2 `__all__`** 内容 + 顺序 + 每个名字真实可解析 | 2 | ✅ |
| `RagasMetrics` / `RagasSample` 真不可变 | 2 | ✅ 都抛 `FrozenInstanceError` |
| `_METRICS` 个数与**顺序** | 1 | ✅ Faithfulness / AnswerRelevancy / ContextPrecision / ContextRecall |
| **`_pick` 七种边界** | 7 | ✅ 正常值 / `None` / 缺键 / **NaN** / 字符串数字 / 脏字符串 `"N/A"` / 列表 `TypeError` |
| **`_extract_metrics` 四种对齐** | 4 | ✅ 少列补 `None` / 行数不足补齐 / `scores=None` / **行数多余按 `expected` 截断** |
| `_empty_metrics` 四字段全 `None` | 1 | ✅ |
| `evaluate_batch([])` 短路 | 1 | ✅ 返回 `[]`（**不发请求**） |
| **真机单条调用** | 2 | ✅ 长度 == 1，类型全是 `float \| None` |
| 应用回归 | 1 | ✅ `len(app.routes) == 8` |
| **合计** | **21** | ✅ **21 / 21** |

### ⭐ 真机结果（`seed.jsonl` 第 1 条：差旅住宿 600 元）

```
faithfulness        = 1.0
answer_relevancy    = 0.9813404362448346
context_precision   = 0.9999999999
context_recall      = 1.0
```

**四个指标全部出分** —— 说明整条链路是通的：我们的 dataclass → `SingleTurnSample` → RAGAS → Judge LLM（DashScope）→ 分数 → 洗回 `RagasMetrics`。

**并且实测到了降级链真的在工作**：另一次运行时 3 个指标报了 `OpenAIConnectionError`，
RAGAS 把它记成 `NaN` → `_pick` 洗成 `None` → **整批没崩、长度没变**，只把那 3 个降级成"本轮没算出来"。

---

## 六、配图（5 张）

| 图 | 文件 | 讲清什么 |
| --- | --- | --- |
| **图一** | `01_ragas_runner 模块调用全景图.png` | 2 个 dataclass + 3 个函数的**入参 / 出参 / 上下游** |
| **图二** | `02_evaluate_batch 批量评估主流程.png` | ⭐ **空列表短路 → 契约转换 → 线程池 → 整批兜底 → 提取** |
| **图三** | `03__extract_metrics 指标提取与长度对齐流程 .png` | **以 `expected` 驱动循环 + 越界补空字典** |
| **图四** | `04__pick 三关指标清洗流程.png` | **三关清洗，第三关抓 NaN** |
| **图五** | `05__empty_metrics 空指标占位流程.png` | 哑对象为什么必须**等长** |

**最该先看的两张**：**图二**（主流程，三件保险都在里面）→ **图一**（先把六个出口的地图看清）。
**图四最重要** —— 它是"NaN 黑洞"这条设计价值的唯一载体，建议单独放大看。

**排版建议**：

```
图一        ← 先给地图
图二        ← 主流程，本模块的骨架
图四        ← 单独一张，NaN 净化是本模块最值得学的一处
图三 + 图五  ← 【并排】"长度对齐"的两个侧面（不足补齐 / 崩溃占位）
```

---

## 七、遗留与待确认

| # | 项 | 说明 |
| --- | --- | --- |
| 1 | ⚠️ **`ragas` 依赖没有上界** | `pyproject.toml` **L22** 是 `"ragas>=0.4.3"`。**ragas 1.0 一发布，`uv lock` 就会把它拉进来**，下面第 2 条的 5 处警告会同时变成 `ImportError`。对比同文件 **L15** 的 `"langchain-community<0.4.2"` —— 同样的习惯漏了 `ragas`。**建议改成 `"ragas>=0.4.3,<1.0"`，但等 Day10 收尾再动**（上次 `uv add ragas` 重新解析把 `openai 3.8.0→3.3.0`、`rich`、`jiter`、`fsspec` 全降了一版，学到一半不适合动依赖锁） |
| 2 | ⚠️ **5 条 PyCharm 警告都是真的，但都是"v1.0 才会炸"** | ① `evaluate()` 已被官方标弃用 —— `ragas/evaluation.py` **L448** 自己 `warnings.warn("...Use the @experiment decorator instead...")`；② 四个小写指标实例（**L11-16**）**都不在 `ragas.metrics.__all__` 里**（实测 `__all__` 只有 16 条），只靠 `ragas/metrics/__init__.py` **L191** 的 `__getattr__` 垫片提供。**现在都能跑，v1.0 会断** |
| 3 | ✅ **不影响功能**：`warnings.filterwarnings(module="ragas")`（**L7**）实测**拦不住** | 实测三种写法：`module="ragas"` → 2 条照打；**完全不过滤 → 也是 2 条**；`module=""` → 0 条。根因是 ragas 用 `stacklevel=2` 抛，**警告归属模块是我们自己的 `ragas_runner`**，匹配不上 `"ragas"`。**但 Python 默认过滤器本来就 `ignore` 非 `__main__` 的 DeprecationWarning，所以实际无害**（实测 `import app.evaluation.ragas_runner` 打出 **0 条**；只有 `-W default` / pytest 才会冒出来）。要修就是把 `module="ragas"` 改成 `module=""` |
| 4 | ⚠️ **`__init__.py` 一填，`app.evaluation` 就不再轻量** | 实测热缓存下 `import ragas` ≈ **2.5~2.9 s**，`import app.evaluation.scoring` ≈ **2.6 s** 且 `"ragas" in sys.modules` 为 `True`。**第 3 节那个"零 IO、可秒级单测"的纯计算模块被 ragas 拖重了** —— 与第 2 节归档「遗留 #3」预告的是同一件事。想保住纯净可走 PEP 562 模块级 `__getattr__` 懒加载（**属结构改动，未实施**） |
| 5 | 🔸 **`answer_relevancy` 对 DashScope 有兼容性抖动** | 单独跑它会报 `LLMDidNotFinishException(The LLM generation was not completed. Please increase the max_tokens...)`，同时伴随 `LLM returned 1 generations instead of requested 3` —— 该指标需要一次生成 **3 个反问句**（`n=3`），DashScope 的 OpenAI 兼容端点只回 1 条 → 记 `NaN`。**但在 4 指标并发那轮它给出了 0.9813**，属抖动而非稳定失败 |
| 6 | ✅ **PyCharm 提示"异常子句过于宽泛"（L159）是故意的** | `except Exception` 在这里**必须宽**：任何一条外部调用失败都不能让整轮评测崩掉。窄化成 `OpenAIError` 之类反而会漏掉未知异常，**把"降级"变成"崩溃"**。不要改 |

---

## 八、关联文件索引

### 8.1 本节新增 / 修改（2 个文件）

| 文件 | 位置 | 说明 |
| --- | --- | --- |
| **`backend/app/evaluation/ragas_runner.py`** | **全文 329 行**（19289 B） | ⭐ **新增**，本模块核心 |
| **`backend/app/evaluation/__init__.py`** | **全文 22 行**（549 B） | **0 字节 → 填充**，`evaluation` 包的门面 |

### 8.2 `ragas_runner.py` 精确锚点

| 位置 | 内容 |
| --- | --- |
| L1-3 | `import asyncio` / `import warnings` / `from dataclasses import dataclass` |
| L5-6 | 注释：ragas 0.4 把 metrics import 路径迁到 `collections`，小写实例 API 到 v1.0 前可用 |
| **L7** | `warnings.filterwarnings("ignore", ..., module="ragas")` —— **实测不生效**（见遗留 #3） |
| L9 | `from ragas import evaluate`（**官方已标弃用**，见遗留 #2） |
| L10 | `from ragas.dataset_schema import EvaluationDataset, SingleTurnSample` |
| **L11-16** | `from ragas.metrics import (answer_relevancy, context_precision, context_recall, faithfulness)` —— **四个名字都不在 `ragas.metrics.__all__` 里** |
| L18-20 | `get_logger` / `get_embeddings` / `get_chat_model` |
| L22 | `logger = get_logger(__name__)` |
| **L25-54** | **`RagasMetrics`**（`frozen=True`） |
| L27-38 | docstring：取值 `[0.0, 1.0] ∪ {None}` + `frozen` 的工程考虑 |
| L42 / L46 / L50 / L54 | 四个字段（各自带"排查环节"注释） |
| **L57-78** | **`RagasSample`**（`frozen=True`，"提问 → 检索 → 回答 → 黄金标准"适配器） |
| L68 / L71 / L75 / L78 | `question` / `answer` / `retrieved_contexts` / `reference_answer` |
| **L81-93** | **`_METRICS`** 编排列表（**L88-93** 带四个指标的逐行说明） |
| **L96-173** | **`evaluate_batch`** |
| L97-112 | docstring：**三件防坑机制** |
| L113-115 | `if not samples: return []`（零成本短路） |
| L121-140 | `EvaluationDataset(samples=[SingleTurnSample(...)])` 契约转换 |
| **L126-128** | 防坑点 1：`response=s.answer or " "` |
| **L130-132** | 防坑点 2：`retrieved_contexts=s.retrieved_contexts or [" "]` |
| **L134-136** | 防坑点 3：`reference=s.reference_answer or " "` |
| **L150-158** | `await asyncio.to_thread(evaluate, ...)` |
| **L156** | ⭐ **`raise_exceptions=False`**（单条失败不毁整批） |
| L157 | `show_progress=False` |
| **L159-164** | `except Exception` → `logger.exception` + **等长全 `None` 占位** |
| **L173** | `return _extract_metrics(result, expected=len(samples))` |
| **L176-248** | **`_extract_metrics`** |
| L180-188 | docstring：动态兼容 + 严格长度对齐 |
| **L202** | `rows = getattr(result, "scores", None) or []`（两步防御） |
| **L210-213** | 长度失配 → **只告警不打断** |
| **L220-222** | ⭐ **架构关键：以 `expected` 驱动循环，严禁 `for row in rows`** |
| **L227** | `row = rows[i] if i < len(rows) else {}`（越界 → 哑行） |
| L232-237 | 四个 key 是 RAGAS 官方协议，**必须逐字匹配**（`relevancy` 不是 `relevance`） |
| L238-245 | `metrics.append(RagasMetrics(...))` |
| L248 | `return metrics` |
| **L251-298** | **`_pick`**（三关清洗） |
| L263-266 | 第一关：键缺失 / `None` |
| L269-274 | 第二关：`float()` 类型强转 |
| **L276-296** | ⭐ **第三关：NaN 判定**（L282-284 来源 / **L286-289 为什么必须洗掉** / L291-293 `x != x` 原理） |
| L298 | `return as_float` |
| **L301-329** | **`_empty_metrics`**（哑对象；四个字段各带业务隐患说明） |

### 8.3 `__init__.py` 锚点

| 位置 | 内容 |
| --- | --- |
| L1 | `from app.evaluation.dataset import EvaluationCase, list_datasets, load_dataset` |
| L2 | `from app.evaluation.ragas_runner import RagasMetrics, evaluate_batch` |
| L3-9 | `from app.evaluation.scoring import (...)` 5 个名字 |
| **L11-22** | **`__all__`（10 个名字，按字母序）** |

### 8.4 依赖与相关

| 位置 | 说明 |
| --- | --- |
| `backend/pyproject.toml` | **L22** `"ragas>=0.4.3"`（**无上界**，见遗留 #1）；**L15** `"langchain-community<0.4.2"` |
| `backend/app/core/hf_env.py` | L34-73 `setup_hf_environment()` —— **必须在 import ragas 之前执行**；L76-98 `_warn_if_too_late()` 会把"调用太晚"暴露成告警 |
| `backend/app/main.py` | **L24-26**：`setup_hf_environment()` 是**应用启动的第一步**，早于所有业务 import —— 这是 `ragas_runner` 能在模块级 import ragas 的前提 |
| `backend/app/llm/models.py` | L32-64 `get_chat_model()`（模块级**单例**，`ChatOpenAI` + `streaming=True`）→ 作为 RAGAS 的 **Judge LLM** |
| `backend/app/ingestion/embedder.py` | L32-77 `get_embeddings()`（模块级**单例**，`OpenAIEmbeddings`）→ 作为 RAGAS 的 **Embeddings** |
| `backend/app/evaluation/dataset.py` | `EvaluationCase.question` / `expected_answer` 是 `RagasSample` 两个字段的来源 |
| `backend/app/evaluation/scoring.py` | `classify_bad_case` 的后 4 个入参**就是** `RagasMetrics` 的 4 个字段 |
| `backend/app/db/models.py` | L1210-1228 四列 `Float nullable=True` 对应 `RagasMetrics` 的四个字段 |
| `frontend/src/client/types.gen.ts` | L501-632 `EvaluationItemRead` 里同名四列 |
| `backend/app/core/exceptions.py` | **本节不抛业务异常**（所有失败都走"降级成 `None`"这条路），所以**不涉及错误归一化** |

### 8.5 与前三节的对照（一张表看完 Day10 后端四步）

| | 第 1 步 新建评测表 | 第 2 步 构建评测集 | 第 3 步 人工指标与归因 | **第 4 步 RAGAS（本节）** |
| --- | --- | --- | --- | --- |
| 产物 | 2 张表 + ORM + 仓储 | `dataset.py` + `.jsonl` | `scoring.py` | **`ragas_runner.py`** |
| 职责 | 存 | 读考卷 | 定判分标准 | **请专业评委** |
| IO | 数据库 | 读文件 | **零 IO** | **网络（LLM + Embedding）** |
| 失败出口 | 无 | **3 个异常** | **0 个**（算错也只是 `False`） | **0 个**（全部降级成 `None`） |
| 核心难点 | 表结构与索引 | 定位"第几行 + 哪个键" | 定位"哪一层 + 该动哪一步" | **把不确定的第三方 API 变成确定的类型** |
| 分数性质 | — | — | **规则算的（客观）** | **大模型评的（主观、会抖）** |
