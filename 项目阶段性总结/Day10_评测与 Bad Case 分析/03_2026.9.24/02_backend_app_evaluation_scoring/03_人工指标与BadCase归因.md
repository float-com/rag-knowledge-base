# 03_人工指标与BadCase归因

> 期：**Day10 · 评测与 Bad Case 分析**
> 章：第 3 章 后端实现 · **第 3 步「人工指标与 Bad Case 归因」**（含 3.1 两个业务指标 / 3.2 十三类归因字典 / 3.3 漏斗归因引擎）
> 记录日期：2026.09.24

---

## 一、本节在整条链路里的位置

> **第 1 节造了"账本"（两张表），第 2 节准备了"考卷"（评测集）。这一节定"判分标准"。**

```
第 1 步  新建评测表      ← 账本（已归档）
第 2 步  构建评测集      ← 考卷 + 读卡机（已归档）
第 3 步  人工指标与归因   ← 你在这里：判分标准 + 病因诊断
第 4 步  RAGAS 自动指标  ← 第三方评估框架封装（产出 4 个 float）
第 5 步  评测入口        ← 让评测与线上走同一条链路（产出 citations / refused / is_error）
第 6 步  评测 Service    ← 执行器主流程：把 1~5 串起来
第 7 步  评测 API        ← 暴露成 8 个接口
```

**用考试打比方，本节是"阅卷标准"**：

| 产物 | 打比方 | 回答什么问题 |
| --- | --- | --- |
| **`compute_citation_hit`** | **答案有没有写出参考出处** | 模型引用的资料，是我期望的那些吗？ |
| **`compute_refusal_correct`** | **该说"不知道"的时候有没有瞎编** | 该拒的拒了吗？不该拒的被误杀了吗？ |
| **`classify_bad_case`** | **错题订正：到底错在哪一步** | 这条为什么差？改检索、改重排，还是磨 Prompt？ |

> ⭐ **本节的真正价值不是"打分"，而是"把分数翻译成动作"** ——
> 只给一个 0.31 的分数，算法工程师不知道该动哪一行代码；
> 给出 `embedding_recall_miss`，他就知道该去查向量索引和切分块。

---

## 二、本节与后续章节的**依赖关系**

```
    第 2 步（已归档）               第 4 步（下一节）            第 5 步
  EvaluationCase 七字段          RAGAS 四项指标             评测入口
  ├─ expected_document_names     ├─ context_recall           ├─ actual_citations
  ├─ expected_keywords           ├─ context_precision        ├─ actual_refused
  └─ should_refuse               ├─ faithfulness             └─ is_error
             │                   └─ answer_relevancy                  │
             │                              │                        │
             └──────────────┬───────────────┴────────────────────────┘
                            ▼
        ┌───────────────────────────────────────────────────────┐
        │   第 3 步（本节）scoring.py                             │
        │   compute_citation_hit  ·  compute_refusal_correct     │
        │   classify_bad_case     ·  _is_low                     │
        └───────────────────────────┬───────────────────────────┘
                                    ▼
                        第 6 步 执行器主流程
                 （把 BadCaseRule 落库进 evaluation_items）
                                    ▼
                        第 7 步 API + 前端看板
                  （人工 PATCH 覆盖机器归因）
```

| 后续章节 | 依赖本节的什么 |
| --- | --- |
| 第 4 节 | **`classify_bad_case` 的后 4 个入参**就是 RAGAS 的四个产出，本节先把"读分数的姿势"定好 |
| 第 5 节 | `actual_citations` / `actual_refused` / `is_error` 由评测入口产出，本节先约定**语义**（尤其是 `citation_hit=None` 的含义） |
| 第 6 节 | **`classify_bad_case(...)` 是执行器每跑完一条 case 必调的一次**；`BadCaseRule` 的两个字段直接落 `evaluation_items` |
| 第 7 节 | `BadCaseCategory` 的 **13 个值**就是前端 `BadCaseCategorySelect` 下拉框的选项，顺序也由它决定 |

### ⭐ 与第 2 节的一处**跨章节对齐**

第 2 节归档里留了一句话：

> 🎯 **第 3 节要与之对齐**：`citation_hit` 对拒答用例**记 `NULL`**、**不参与命中率的分母**。

本节落实了这件事 —— 靠的是 `citation_hit: bool | None` 这个**三态**参数：

| 取值 | 含义 | 从哪来 |
| --- | --- | --- |
| `True` | 引用了，且命中期望 | 路径 A 或路径 B 成立 |
| `False` | 引用了，但完全对不上期望 | 两路都不命中 |
| **`None`** | **不适用** —— 该题本就该拒答，不该有引用 | 第 5 节对 `should_refuse=True` 的用例不调用 `compute_citation_hit` |

**`None` 不是"没命中"，是"这道题不考引用"。** 这个区分是本节第一个关键设计。

---

## 三、3.1 两个业务指标

### 3.1.1 `compute_citation_hit` —— 双重宽松匹配（L67-138）

```
实际引用 citations  →  与期望比对：
    路径 A：实际引用的来源文档名 命中 expected_document_names 中【任意一个】
    路径 B：实际引用片段（quote）拼成的大文本 命中 expected_keywords 中【任意一个】
    任一成立  →  引用命中（True）
    两路皆否，或压根没有引用  →  未命中（False）
```

**四个执行阶段与精确锚点**：

| 阶段 | 位置 | 动作 | 关键防御 |
| --- | --- | --- | --- |
| **前置边界** | **L86-87** | `if not actual_citations: return False` | 模型压根没给引用，**直接判负**，不浪费后面的 set/join 计算 |
| **路径 A** | **L97** | 集合推导式收敛文档名 → `actual_doc_names` | **`c.get("document_name", "")`** 兜底缺字段，避免 `KeyError` |
| **路径 A 判定** | **L111** | `any(name in actual_doc_names for name in expected_document_names if name)` | **`if name`** 过滤空串 |
| **路径 B 判定** | **L119 → L134** | 拼 `quote_blob` 再逐个关键词子串匹配 | **`if kw`** 过滤空串 |

#### 设计要点一：为什么是 `any`（命中一个就够），不是 `all`

源码 **L105-110** 给了三条业务理由，**这是本函数最值得记的一段**：

| # | 理由 | 说人话 |
| --- | --- | --- |
| 1 | **同义知识源容错** | 同一答案可能同时存在于《政策 2024 版》《政策 2025 版》《FAQ》里，模型引用**任意一份**都证明不是凭空捏造；要求全中会**严重误杀** |
| 2 | **避免大模型"刷引用"** | 强制全中会**反向逼模型在答案末尾堆砌所有弱相关文档**，损害用户体验 |
| 3 | **粗筛闸门定位** | 这个指标只是**定性的准入门槛**（回答是否有据可依），"知识点找得全不全"交给第 4 节的 `context_recall` 算精细召回率 |

> ⭐ **这张表把"业务语义"和"指标分工"绑在一起讲** ——
> `citation_hit` 负责**有没有据**，`context_recall` 负责**据够不够**。两者不是重复，是粗筛与精算。

#### 设计要点二：性能上的两个小优化（都写在注释里）

| 优化 | 位置 | 从 | 到 |
| --- | --- | --- | --- |
| 引用文档名装进 **`set`** | **L97** | 列表 `in` 查找 O(n) | 集合哈希查找 **O(1)** |
| 所有 `quote` 拼成 **`quote_blob`** | **L127** | 关键词 × 引用 双重循环 **O(L×K)** | 一次流式扫描 **O(L+K)** |

引用一般只有 1~5 条，**性能其实无所谓** —— 这两个优化的真实收益是**代码更直白**。

#### 🔴 设计要点三：`if name` / `if kw` 不是可有可无的装饰（最容易踩的坑）

```python
"" in "任何文本"   # → 永远是 True！
```

- **`if name`（L111）**：标注里若出现空字符串文档名，`"" in actual_doc_names` 也会被判定命中（只要引用里有一条 `document_name` 缺失，`get` 兜底就产生 `""`）→ **全部用例误判为命中**。
- **`if kw`（L134）**：同理，空关键词会让**所有**引用的文本都被判命中 → 命中率虚高到 100%。

**实测证据**：本节的验证里专门跑了一条 `compute_citation_hit(citations, [""], [""]) → False`，
证明这两个过滤器真的在挡子弹。

### 3.1.2 `compute_refusal_correct` —— 双向对称的严格等号（L141-167）

```python
return actual_refused == should_refuse      # L167
```

**只有 4 种组合，2 对 2 错**：

| 预期 `should_refuse` | 实际 `actual_refused` | 结果 | 业务后果 |
| --- | --- | --- | --- |
| `True` | `True` | ✅ **True** | 正确兜底 |
| `False` | `False` | ✅ **True** | 正常作答 |
| `True` | `False` | ❌ **False** | **漏防**：库里没有还强行编造 → **致命幻觉** |
| `False` | `True` | ❌ **False** | **误杀**：明明有据可查却冷漠推脱 → **损伤可用性** |

> ⭐ **为什么必须用 `==` 而不是只判一个方向？**
> 因为这两种错的**调优手段完全相反**：
> 漏防要**收紧** Prompt / 调高相似度截断阈值；误杀要**放宽**检索过滤阈值。
> **如果指标不区分方向，就永远不知道该往哪个方向拧螺丝。**
> —— 这也正是 Level 1 归因要拆成 `too_loose` / `too_strict` 两个分类的原因。

---

## 四、3.2 十三类归因字典（`BadCaseCategory`，L30-44）

```python
BadCaseCategory = Literal[
    "document_parse_failed", "chunk_split_bad", "embedding_recall_miss",
    "keyword_recall_miss", "rrf_fusion_error", "rerank_order_error",
    "context_judge_too_loose", "context_judge_too_strict", "prompt_constraint_weak",
    "generation_off_context", "citation_parse_failed", "permission_filter_error", "other",
]
```

**按 RAG 链路的层级排列**（源码 L29 的注释）：

```
解析层 → 检索层 → 决策层 → 排序层 → 生成层 → 权限与兜底
```

### ⭐ 最重要的一张表：**13 类里，规则引擎只能自动产出 7 类**

| # | 分类 | 谁能判定 | 判定依据（本节的哪一行） |
| --- | --- | --- | --- |
| 1 | `document_parse_failed` | 🔧 **仅人工** | 需看原始文件（OCR / 乱码），代码层拿不到 |
| 2 | `chunk_split_bad` | 🔧 **仅人工** | 需人看切片边界是否割裂语义 |
| 3 | **`embedding_recall_miss`** | ✅ **自动** | **L262**（L2 引用未命中）/ **L274**（L3.1 召回低） |
| 4 | `keyword_recall_miss` | 🔧 **仅人工** | 关键字腿与向量腿的召回差异，需对比两路结果 |
| 5 | `rrf_fusion_error` | 🔧 **仅人工** | 需人工核查 RRF 融合权重 |
| 6 | **`rerank_order_error`** | ✅ **自动** | **L280**（`context_precision` 低） |
| 7 | **`context_judge_too_loose`** | ✅ **自动** | **L246**（该拒未拒） |
| 8 | **`context_judge_too_strict`** | ✅ **自动** | **L254**（不该拒却拒） |
| 9 | **`prompt_constraint_weak`** | ✅ **自动** | **L292**（`answer_relevancy` 低） |
| 10 | **`generation_off_context`** | ✅ **自动** | **L286**（`faithfulness` 低） |
| 11 | `citation_parse_failed` | 🔧 **仅人工** | 需比对"答案里有角标但 citations 为空" |
| 12 | `permission_filter_error` | 🔧 **仅人工** | 需人工核对权限规则 |
| 13 | **`other`** | ✅ **自动** | **L237**（`is_error=True`） |

> 🎯 **7 自动 / 6 人工** —— 这个比例必须在第 7 节做前端下拉框时说清楚，
> 否则使用者会以为"下拉框里 13 个选项都是机器给的"。

### 契约锁：与前端 `types.gen.ts` 的 1:1

`frontend/src/client/types.gen.ts` **L623 / L646** 里有同一份 13 值联合类型。
本节验证脚本**逐值比对**了两边的**集合与顺序**：

```
backend  13 类 == frontend 13 类   ✅ 完全一致（含顺序）
```

> ⚠️ **顺序不能乱** —— 前端下拉框的排列顺序就是照这个来的。
> 变动顺序 = 改动 UI。

---

## 五、3.3 漏斗归因引擎（`classify_bad_case`，L170-299）

### 5.1 函数签名：强制关键字传参

```python
def classify_bad_case(*, should_refuse, actual_refused, refusal_correct,
                      citation_hit, faithfulness, answer_relevancy,
                      context_precision, context_recall, is_error) -> BadCaseRule:
```

`*` 之后**禁止位置传参**。理由（源码 L184 起）：

> 本函数有 **9 个入参，其中 5 个是 `float | None`，参数类型高度相似**。
> 如果允许位置传参，`classify_bad_case(a, b, c, d, e, f, g, h, i)` 里只要错一位，
> **`context_precision` 和 `context_recall` 互换，归因结论就完全反了** ——
> 而且不会报错，是最难查的那类 bug。

### 5.2 四级漏斗：**越底层，优先级越高**（命中即停）

```
Level 0  is_error ──────────────────────► other                     [系统故障]
Level 1  not refusal_correct ─┬─ 该拒未拒 ► context_judge_too_loose   [安全闸门]
                              └─ 不该拒却拒 ► context_judge_too_strict
Level 2  citation_hit is False ─────────► embedding_recall_miss      [证据链]
Level 3  _is_low(context_recall) ───────► embedding_recall_miss      [召回]
         _is_low(context_precision) ────► rerank_order_error         [排序]
         _is_low(faithfulness) ─────────► generation_off_context     [忠实]
         _is_low(answer_relevancy) ─────► prompt_constraint_weak     [相关]
         （全部通过）───────────────────► BadCaseRule(False, None)   [优质样例]
```

**为什么必须"从最底层开始短路"**（源码 L18-22）：

```
RAG 是顺序依赖流水线：
  [网络/运行] → [意图与边界(拒答)] → [召回/检索] → [重排] → [生成/表述]

如果最底层的检索彻底丢了，此时"模型回答得离题"是【表象】而不是【根因】。
```

> 🎯 **不短路会怎样？** 会得出"**检索完全没召回，却去归因大模型胡说八道**"的**误诊** ——
> 算法工程师拿着错误的病因去改 Prompt，改一个月也没用。

### 🔴 5.3 L2 的"避坑关键"：必须是 `citation_hit is False`

```python
if citation_hit is False:          # ✅ L262 正确
if not citation_hit:               # ❌ 错误写法
```

| 写法 | `citation_hit=None`（正确拒答的用例）时 | 后果 |
| --- | --- | --- |
| `is False` | `None is False` → **False** | ✅ 正常拒答的用例**不会被冤枉** |
| `not citation_hit` | `not None` → **True** | ❌ **正常拒答的用例被误判为 `embedding_recall_miss`** |

**这是一处典型的"三态布尔"陷阱**：`bool | None` 有三个值，
而 `not` 会把 `None` 和 `False` 混成一个。

**实测证据**：验证脚本里跑了一条 `should_refuse=True, actual_refused=True, citation_hit=None`
→ 期望 `(False, None)`，**结果正确判为优质样例**，没有被误诊。

### 5.4 Level 3 的内部顺序 = RAG 流水线的顺序

| 顺序 | 指标 | 归因 | 诊断环节 | 修复动作（源码注释里的建议） |
| --- | --- | --- | --- | --- |
| 3.1 | `context_recall` | `embedding_recall_miss` | **检索** | 换更强 Embedding / 开 BM25 混合检索 / 扩大 Top-K |
| 3.2 | `context_precision` | `rerank_order_error` | **重排** | 上 Cross-Encoder Rerank，核心论据压进前 3 |
| 3.3 | `faithfulness` | `generation_off_context` | **生成·事实性** | System Prompt 加"仅依据材料，严禁常识推演" |
| 3.4 | `answer_relevancy` | `prompt_constraint_weak` | **生成·指令遵循** | 优化 Prompt 结构 + Few-Shot 范式 |

> ⭐ **这张表是本节的核心产出**：每一个 `category` 都对应一个**明确的修改动作**。
> 3.1/3.2 是"**材料没找对**"（改检索、改重排），3.3/3.4 是"**材料对了但话说歪了**"（磨 Prompt）。
> 判断分界线就是前两个指标还是后两个指标低。

### 5.5 `_is_low` 与阈值（L302-311）

```python
_LOW_SCORE_THRESHOLD = 0.5                                        # L49
def _is_low(score: float | None) -> bool:
    return score is not None and score < _LOW_SCORE_THRESHOLD     # L311
```

**三条边界语义**（全部实测）：

| 输入 | 返回 | 说明 |
| --- | --- | --- |
| `None` | **False** | **缺分 ≠ 低分**。评分服务超时导致的 `None` **不能给算法扣帽子** |
| `0.5` | **False** | 用的是 `<` 不是 `<=`，**边界值不算低分** |
| `0.49` | True | 严格小于才判低 |
| `0.0` | True | 最低分也是低分 |

> 🎯 **`None → False` 这个设计必须和第 2 节 `citation_hit=None` 一起理解**：
> 本模块贯彻同一条哲学 —— **"数据不适用/拿不到" 绝不等于 "表现差"**。
> 否则服务一抖动，Bad Case 率就飙升，整个评测体系失去可信度。

### 5.6 `BadCaseRule`：`frozen=True` 的不可变结论（L52-64）

```python
@dataclass(frozen=True)
class BadCaseRule:
    is_bad_case: bool
    category: BadCaseCategory | None
```

**为什么必须冻结**（源码 L56-60）：

| 角度 | 理由 |
| --- | --- |
| **机评侧** | 算法引擎输出的结论**一旦生成就锁定**，禁止任何业务代码在内存里就地篡改 |
| **人评侧** | 前端人工要修正归因，**必须走专用 PATCH 接口**提交人工修正记录 —— 这样**机评与人评的对比轨迹才留得下来** |

> ⭐ 如果允许就地改内存，就分不清"这条是机器判的还是人改的"，
> 也就**没法反过来评估规则引擎的准确率**了。

---

## 六、运行验证（真实执行，非纸面推导）

验证方式：直接 `import app.evaluation.scoring` 后逐项断言，**全部 36 项通过**。

| 检查组 | 项数 | 结果 |
| --- | --- | --- |
| **契约一致性**（13 值集合 + 顺序 vs `types.gen.ts`） | 1 | ✅ **完全一致** |
| **`BadCaseRule` 真不可变** | 1 | ✅ 改字段抛 `FrozenInstanceError` |
| **`compute_citation_hit`** | 8 | ✅ 全通过（空引用 / 路径 A / 路径 A 部分命中 / 路径 B / 两路皆否 / 标注为空 / 空串过滤 / `quote=None` 的 `str(None)` 坑） |
| **`compute_refusal_correct`** | 4 | ✅ 四种组合全对（2 及格 / 2 不及格） |
| **`_is_low` 与阈值** | 6 | ✅ 含 `None→False`、边界 `0.5→False`、`0.0→True` |
| **`classify_bad_case`** | 15 | ✅ 含 **4 组优先级压制** + 拒答样例 `citation_hit=None` + 边界 `context_recall=0.5` |
| **应用回归** | 1 | ✅ 应用可正常导入，`len(app.routes) == 8`（4 个内建文档路由 + 4 个挂载子路由） |
| **合计** | **36** | ✅ **36 / 36 通过** |

**4 组"优先级压制"是本次验证的重点**（证明漏斗真的在短路，不是"逐个 if 并列"）：

| 场景 | 同时满足 | 实际归因 | 是否被压制 |
| --- | --- | --- | --- |
| L0 压全部 | `is_error=True` + `citation_hit=False` + `context_recall=0.0` | `other` | ✅ 压住 |
| L1 压 L2 | 拒答判错 + `citation_hit=False` | `context_judge_too_loose` | ✅ 压住 |
| L2 压 L3 | `citation_hit=False` + `context_precision=0.0` | `embedding_recall_miss` | ✅ 压住 |
| L3.1 压 L3.2 | `context_recall=0.1` + `context_precision=0.0` | `embedding_recall_miss` | ✅ 压住 |

**边界语义实测**：`context_recall=0.5`（**恰好等于阈值**）→ 判为 `(False, None)` 优质样例，
证明 `_is_low` 用的是严格小于。

---

## 七、配图（5 张）

| 图 | 文件 | 讲清什么 |
| --- | --- | --- |
| **图一** | `01_scoring模块函数黑盒总览.png` | 4 个函数 + 1 个类 + 1 个 `Literal` 的**入参 / 出参 / 上下游** |
| **图二** | `02_compute_citation_hit 判定流程.png` | **两路匹配 + 三个出口**（`any` 而非 `all`） |
| **图三** | `03_compute_refusal_correct 判定流程.png` | **双向对称**：4 种组合、2 对 2 错 |
| **图四** | `04_classify_bad_case 漏斗式归因流程.png` | ⭐ **四级漏斗 + 命中即停**（本节设计重点） |
| **图五** | `05__is_low 阈值判定流程.png` | **`None → False` 与边界 0.5** |

**最该先看的两张**：**图四**（漏斗主逻辑）→ **图一**（先把 6 个元素的地图看清）。
图二/图三/图五 是细节补充，可以后看。

**排版建议**：

```
图一        ← 先给地图（六个元素分别是什么）
图四        ← 单独一张，它是本模块的设计核心
图二 + 图三  ← 【并排】形成"两个业务指标"的对照
图五        ← 细节补充
```

### 本节画图沉淀出的 5 条规则（后面几节继续用）

| # | 规则 | 出处 |
| --- | --- | --- |
| 1 | **不要嵌套子图**（子图套子图会框线互压，"二、归因引擎"会盖住"一、业务指标"的边框） | 图一三轮返工 |
| 2 | **子图与外部有连线时，`direction` 不可靠** —— 不要靠它来排版 | 图一 |
| 3 | ⭐ **没有入边的"类型 / 契约"节点会被甩到图顶**，并拖出一条横穿全图的虚线 —— 让它挂在有入边的父节点下 | 图一的 `BadCaseCategory` |
| 4 | **容器底色不能和节点底色相同**（同色会让内外糊成一片） | 图一终版 |
| 5 | **竖排图先按"消费方所在行"给输入排序**，再手动 `<br/>` 控制换行（自动换行阈值默认仅 200px，约 12 个汉字） | 图一 |

> 前 3 条是"**画错了会让读者对代码实际行为产生错误预期**"级别的错误，
> 第 4、5 条才是美化。**优先保证前 3 条。**

---

## 八、遗留与待确认

| # | 项 | 说明 |
| --- | --- | --- |
| 1 | ⚠️ **`_LOW_SCORE_THRESHOLD` 是单一阈值** | 教学版四项指标统一 `0.5`。源码 L47-48 已注明生产建议：`context_recall` 可放宽到 `0.6`，`faithfulness` 通常要求 `>= 0.85`。**改成分项配置会牵动 `_is_low` 签名与 4 处调用**，等第 6 节接执行器时一并收口 |
| 2 | ⚠️ **评测集漏填标注时 `citation_hit` 必然为 `False`** | 若 `expected_document_names` 与 `expected_keywords` **都为空**，只要模型有引用就返回 `False`（实测），于是 L2 直接扣"检索漏召回"。**与 `_is_low(None)=False` 的哲学不一致** —— 建议改为"两路标注都空 → 返回 `None`"（`classify_bad_case` 用的是 `is False`，天然兼容，不用改） |
| 3 | ⚠️ **`refusal_correct` 是派生参数** | 它恒等于 `compute_refusal_correct(actual_refused, should_refuse)`，却是独立入参。调用方若传歪，会出现"该拒也拒了却判 `too_strict`"的矛盾。**建议删掉该形参、函数内部自己算**（可与第 2 条的一同处理） |
| 4 | ⚠️ **6 个分类规则引擎永远产不出** | `document_parse_failed` / `chunk_split_bad` / `keyword_recall_miss` / `rrf_fusion_error` / `citation_parse_failed` / `permission_filter_error`。建议显式声明 `MANUAL_ONLY_CATEGORIES`，前端下拉框据此标注"仅人工" |
| 5 | 🔸 **`str(None)` 的小坑** | **L127** `str(c.get("quote", ""))`：`quote` 键存在但值为 `null` 时得到字符串 `"None"`（实测会被关键词 `"None"` 命中）。稳妥写法 `str(c.get("quote") or "")` |
| 6 | 🔸 **L46 有一处笔误** | 注释写成 `# # 业务及 RAGAS 评估指标阈值线`（双井号） |
| 7 | ⚠️ **契约 SSOT 尚未建立** | `types.gen.ts` L623/L646 的 13 值联合目前是**手写镜像**。第 7 节写 `api/schemas/evaluation.py` 时应使用 `BadCaseCategory` 让 FastAPI 生成 enum。**⚠️ 现在绝不能跑 `npm run gen:api`** —— 路由未补齐，会把 `types.gen.ts` 从 2588 行砍到 1115 行 |
| 8 | ⚠️ **`is_error=True` 记成 `other` 会污染 Bad Case 率** | 系统超时/网络中断不是算法质量问题，却一样进 `is_bad_case=True` 的分母。**统计口径需在第 6 节明确**（排除 `category == "other"`，或落在 `progress_failed` 上不计分） |

---

## 九、关联文件索引

### 9.1 本节新增（1 个文件）

| 文件 | 位置 | 说明 |
| --- | --- | --- |
| **`backend/app/evaluation/scoring.py`** | **全文 311 行**（20821 B） | ⭐ 本模块核心：4 个函数 + 1 个类 + 1 个 `Literal` |

### 9.2 `scoring.py` 精确锚点

| 位置 | 内容 |
| --- | --- |
| L1-23 | 模块 docstring（纯计算 / 两部分职责 / **为什么必须命中即停** L18-22） |
| L25-26 | `from dataclasses import dataclass` / `from typing import Literal` |
| L28-29 | 分类字典的层级注释（解析层 → 检索层 → 决策层 → 排序层 → 生成层 → 权限与兜底） |
| **L30-44** | **`BadCaseCategory = Literal[...]`（13 类，每类带行内中文注释）** |
| L46-49 | `_LOW_SCORE_THRESHOLD = 0.5`（**L46 有 `# #` 笔误**）+ 生产分项阈值建议 |
| **L52-64** | **`BadCaseRule`（`@dataclass(frozen=True)`，两字段）** |
| L63 / L64 | `is_bad_case: bool` / `category: BadCaseCategory \| None` |
| **L67-138** | **`compute_citation_hit`** |
| L86-87 | 前置边界：空引用直接 `return False` |
| L93-96 | 集合推导式的逐行讲解 |
| **L99-110** | ⭐ **`any` 而非 `all` 的三条业务设计考量** |
| L111-112 | 路径 A：`any(name in actual_doc_names ... if name)` |
| L119 | `if expected_keywords:` 短路 |
| L121-127 | `quote_blob` 的拼接与"为什么拼"的说明 |
| **L131** | ⭐ `if kw` —— **空串会让 `"" in 任何文本` 恒为 True** |
| L134-135 | 路径 B 判定 |
| L138 | 两路皆否 → `return False` |
| **L141-167** | **`compute_refusal_correct`**（L144-154 两个参数的三态说明） |
| L159-164 | ⭐ **双向对称性**：漏防风险 vs 误杀风险 |
| **L167** | `return actual_refused == should_refuse` |
| **L170-299** | **`classify_bad_case`** |
| L184-228 | 四组入参的详细拆解（系统运行层 / 安全门控层 / 证据支撑层 / RAGAS 量化层） |
| L205-211 | ⭐ `citation_hit` 的**三态**说明（`None` = 不适用，不可当成未命中） |
| L226 | "缺失不算低分"的防御说明 |
| **L231-238** | Level 0：`is_error` → `other` |
| **L240-254** | Level 1：`refusal_correct` → `too_loose` / `too_strict` |
| **L256-266** | ⭐ Level 2：**必须 `citation_hit is False`，禁止 `not citation_hit`**（L259-261） |
| **L268-294** | Level 3：召回 → 排序 → 忠实 → 相关（四步各自带修复动作） |
| L296-299 | 全部通过 → `BadCaseRule(is_bad_case=False, category=None)` |
| **L302-311** | **`_is_low`**（L311 的 `is not None and score < 阈值`） |

### 9.3 依赖与相关

| 位置 | 说明 |
| --- | --- |
| `frontend/src/client/types.gen.ts` | **L623 / L646** 的 13 值联合类型 —— 与 `BadCaseCategory` **集合与顺序实测一致**；L501-632 `EvaluationItemRead` 承载本节全部产出的落库字段 |
| `backend/app/db/models.py` | **L1208-1261**：`faithfulness` / `answer_relevancy` / `context_precision` / `context_recall` / `citation_hit`（**L1230-1233 注明 `should_refuse=True` 时置 NULL**）/ `refusal_correct` / `is_bad_case` / `bad_case_category` / `bad_case_note` |
| `backend/app/evaluation/dataset.py` | `EvaluationCase`（L36-65）提供本节两个指标的**期望值来源**（`expected_document_names` / `expected_keywords` / `should_refuse`） |
| `backend/app/db/repositories/evaluation_repo.py` | **L204-255** `list_page(bad_case_only=..., category=...)` —— 按本节的 `bad_case_category` 筛选 |
| `backend/app/core/exceptions.py` | 本节不抛异常（纯计算、无 IO），所以**不涉及错误归一化** |

### 9.4 与第 2 节的对照（一句话）

| | 第 2 节 `dataset.py` | 第 3 节 `scoring.py` |
| --- | --- | --- |
| 职责 | 把考卷读进来 | 把分数翻译成病因 |
| IO | 读文件（**有 IO**） | **零 IO** |
| 失败出口 | **3 个异常出口**（错误归一化） | **0 个**（输入不合法也只是算出 `False`） |
| 核心难点 | 定位到"第几行 + 哪个键" | 定位到"哪一层 + 该动哪一步" |
