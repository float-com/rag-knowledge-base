# 05_Schema扩展与前端来源Tag

> 章节：Day06_全文检索、混合检索与 RRF · 第 3 章 开发实现 · **第 11-13 步**
> 覆盖：
> - 第 11 章 配置项（`backend/app/core/config.py`，对齐教程措辞）
> - 第 12 章 Schema 扩展（`backend/app/api/schemas/chat.py`，新增 `RetrievalMeta`）
> - 第 13 章 前端来源 Tag（`frontend/src/components/CitationList.tsx`，**仅校验未改动**）
> 记录日期：2026.09.20

---

## 一、三章的关系

```
第 11 章  配置项             retrieval_recall_top_k / rrf_k
              ↓  被谁用
第 8 章   HybridRetriever.search(recall_top_k=..., k=settings.rrf_k)
              ↓  产出
第 9-10 章 retrieve 落库 retrieval_meta（JSONB）
              ↓  暴露给前端
第 12 章  RetrievalMeta 模型 + CitationRead 字段   ← OpenAPI schema
              ↓  前端类型生成
第 13 章  formatSourceTag 渲染「混合 / 向量 / 关键词」Tag
```

**一句话**：第 11 章是**参数**，第 12 章是**契约**，第 13 章是**呈现**。三章都很轻，但缺了第 12 章前端就**拿不到类型**、`retrieval_meta` 也就白落库了。

| 文件 | 改动 | 行数变化 |
| --- | --- | --- |
| `backend/app/core/config.py` | 注释措辞对齐教程（字段本身上一批已加） | 180（不变） |
| `backend/app/api/schemas/chat.py` | 新增 `RetrievalMeta` + `_parse_retrieval_meta`，`CitationRead` 加字段 | 240 → **295** |
| `frontend/src/components/CitationList.tsx` | **未改动**（已预置） | 118（不变） |

---

## 二、第 11 章：配置项

```python
    # ===== 混合检索配置（第 6 期） =====
    # 每路（向量 / 关键词）召回数量；设计文档建议候选 20-50。
    # 取 20 兼顾召回率与 RRF 融合开销：
    #   recall_top_k 是「召回率 vs 融合开销」的取舍——召回数量越多，融合越能纠正
    #   单路的不足，但 SQL 排序、网络 I/O、Python 侧 dict 迭代都会变慢。
    #   且它必须【大于】retrieval_top_k，否则两路没有足够候选参与融合，
    #   RRF 会退化成"对同一批结果重排"。
    retrieval_recall_top_k: int = 20        # L163
    # RRF 平滑常数，业界一般用 60；越小越偏向高排名条目
    #   （公式 score(d) = Σ 1/(k + rank_i(d))；k 越大越平滑、越依赖"两路都命中"）
    rrf_k: int = 60                          # L166
```

### 2.1 本批次只改了注释措辞

**这两个字段在实现第 8 章时就已经加进去了** —— 因为教程当时给的 `search` 代码引用了 `settings.rrf_k`，而 `config.py` 里没有该字段，不加会直接 `AttributeError`。

本批次把注释**对齐教程原话**，并补上教程那句"取舍"说明（召回数量与融合开销的权衡）。

### 2.2 `recall_top_k = 20` 这个数字的来历

教程写"设计文档建议候选 20-50"，取 20 是**下限**。为什么是 20 而不是 50：

| 取值 | 收益 | 代价 |
| --- | --- | --- |
| 太小（如 5） | 省开销 | **两路各只有 5 条，融合池最多 10 条 → RRF 退化成"对同一批结果重排"** |
| **20** ✅ | 实测关键词腿对 `接口` 能召回 16 条，20 够覆盖 | SQL 排序 / 网络 I/O / Python dict 迭代的开销可接受 |
| 50 | 召回更全 | 每条腿多召回 30 条，且 `vector_search` 的 HNSW 扫描成本上升 |

**关键约束是"必须大于 `retrieval_top_k`(5)** —— 否则融合根本没素材。20 = 5 的 4 倍，留出了足够余量。

---

## 三、第 12 章：`RetrievalMeta` 模型（L68-98）

```python
class RetrievalMeta(BaseModel):
    """混合检索调试元数据。

    - sources: 该 chunk 命中的检索路（vector / keyword）；两路都命中即"混合"
    - *_rank: 在该路召回结果中的名次（从 1 开始），用于复盘排序
    - vector_score: cosine similarity，**绝对值有意义**，做拒答阈值用
    - keyword_score: ts_rank，**相对值**，跨 query 不可比
    - rrf_score: 两路融合分，**仅在同一次检索内可比**
    """
    sources: list[str] = Field(default_factory=list)     # L87
    vector_rank: int | None = None                        # L89
    vector_score: float | None = None                     # L91
    keyword_rank: int | None = None                       # L93
    keyword_score: float | None = None                    # L95
    rrf_score: float | None = None                        # L97
```

### 3.1 六个字段全都必须有默认值

**和 `RetrievedChunk` 是同一个约束**（见第 6 章归档）：来源不同的 chunk 只填自己那一路的字段 —— 仅向量命中时 `keyword_*` 为 `None`，反之亦然。所以**每个字段都必须可缺省**。

**好处有两层**：

1. 让"只填一部分"的构造方式成立；
2. **前端类型不必声明为可选键** —— 键一定在，值可能为 `None`。这正是第 10 章"刻意保留 `None` 键"的完整闭环：后端保留键 → schema 声明为 `X | None` → 前端 `meta.keyword_rank !== null` 一个判断就够。

### 3.2 docstring 里那句"能不能横向比较"是本模块最值得记的部分

| 字段 | 能否跨查询比较 | 原因 |
| --- | --- | --- |
| `vector_score` | ✅ **可以** | 余弦相似度是绝对量，值域 [0,1]，语义固定 |
| `keyword_score` | ❌ **不可以** | `ts_rank` 受词频与文档长度影响，换查询尺度就变 |
| `rrf_score` | ❌ **只限同一次检索内** | 上限是 `2/(k+1)`，且名次是相对的 |

**实测证据（第 5 章留下）**：

```
'DDR5-4800' 命中 1 篇 → ts_rank = 0.188390
'接口'      命中 16 篇 → ts_rank = 0.086545
```

同一个 `ts_rank` 数值在不同查询下含义完全不同 → 所以 `vector_score` 才是**唯一能拿去和阈值比**的字段。这就是第 9 章 `_should_refuse` 必须用它、而不能用 `score` 的根本原因。

---

## 四、第 12 章：`CitationRead` 与解析函数

### 4.1 `CitationRead` 加字段（L125）

```python
    # 混合检索调试元数据；历史消息（第 6 期之前写入的）没有这个字段，
    # 解析失败时静默为 None，前端按缺失隐藏 Tag
    retrieval_meta: RetrievalMeta | None = None          # L125
```

**为什么是 `| None`**：`answer_citations.retrieval_meta` 这一列是 `nullable=True`（第 4 章迁移时定的），历史记录读出来是 `NULL`。类型必须是 `RetrievalMeta | None`，否则**校验会直接失败** —— 这与 `document_id` / `chunk_id` 的可空理由是同一类。

### 4.2 `from_orm` 里调用解析函数（L142）

```python
        return cls(
            id=citation.id,
            ...
            quote=citation.quote,
            retrieval_meta=_parse_retrieval_meta(citation.retrieval_meta),   # L142
        )
```

**注意这里不是直接传 `citation.retrieval_meta`**：ORM 里它是**裸 dict**（JSONB 反序列化的结果），直接传给 Pydantic 虽然也能自动校验，但**没有防御** —— 一条脏数据会让整个会话详情接口 500。所以走解析函数。

### 4.3 `_parse_retrieval_meta` 的两层防御（L146-161）

```python
def _parse_retrieval_meta(raw: dict | None) -> RetrievalMeta | None:
    """历史消息没有 retrieval_meta，非法/缺失静默返回 None。"""
    # 第一层：None（历史消息）与非 dict（脏数据）直接放行
    if not isinstance(raw, dict):            # L155
        return None
    try:
        # 第二层：字段类型不符时静默降级，不阻断整个响应
        return RetrievalMeta.model_validate(raw)   # L159
    except Exception:                        # L160
        return None
```

**为什么必须两层**：

| 层 | 挡住什么 | 若不挡 |
| --- | --- | --- |
| `isinstance` 检查 | `None`（历史消息）、非 dict（脏数据） | `model_validate(None)` 抛 `ValidationError` |
| `try/except` | 字段类型不符（如 `vector_rank: "abc"`） | 一条脏数据 → **整个会话详情 500** |

**这与上一期 `_parse_query_route` 是同一个兜底风格**，也是**同一个原则**：

> **反序列化路径上的解析函数绝不能向上抛异常。** 一条历史脏数据不应让整个接口挂掉 —— 宁可让那一块元信息显示为空，也不能让用户看不到整个会话。

**实测 8/8 全过**：

| 输入 | 结果 | 期望 |
| --- | --- | --- |
| `None`（历史消息） | `None` | ✓ |
| `{}` | `RetrievalMeta` | ✓ |
| `"not-a-dict"` | `None` | ✓ |
| `[1,2,3]` | `None` | ✓ |
| 正常 dict | `RetrievalMeta` | ✓ |
| `{"vector_rank": "abc"}` | **`None`** | ✓ |
| 多余字段 | `RetrievalMeta` | ✓ |
| 仅向量命中（其余 `None`） | `RetrievalMeta` | ✓ |

---

## 五、第 12 章：OpenAPI 暴露验证

```
components.schemas 含 RetrievalMeta                  ✓
RetrievalMeta 属性:
  ['sources','vector_rank','vector_score','keyword_rank','keyword_score','rrf_score']
RetrievalMeta 必填: （无）                            ← 全部有默认值，前端类型全是可选键
CitationRead.retrieval_meta 类型:
  {"anyOf":[{"$ref":"#/components/schemas/RetrievalMeta"},{"type":"null"}]}
                                                      ✓ 正确表达"可为 null"
```

**这一步是本节的目的**：教程说"后端要把 `retrieval_meta` 暴露到 OpenAPI schema，前端才能拿到对应类型并渲染 Tag"。验证方式就是**在真实的 `/openapi.json` 里能查到它**。

**真实接口回归**：

```
新消息: 5/5 条引用带 retrieval_meta
  ordinal=1 {"sources":["vector"],"vector_rank":1,"vector_score":0.732,...}
历史消息: 25 条引用全部 retrieval_meta=None，GET 接口 200（未 500）
```

---

## 六、第 13 章：前端来源 Tag（**仅校验，未改动**）

### 6.1 代码已与教程一致

```typescript
type SourceTagMeta = { color: string; label: string }              // L19

function formatSourceTag(sources?: string[]): SourceTagMeta | null {   // L21
  if (!sources || sources.length === 0) return null                    // L22
  const hasVector = sources.includes('vector')                         // L23
  const hasKeyword = sources.includes('keyword')
  if (hasVector && hasKeyword) return { color: 'purple', label: '混合' }   // L25
  if (hasVector) return { color: 'blue', label: '向量' }                  // L26
  if (hasKeyword) return { color: 'orange', label: '关键词' }             // L27
  return null
}
```

**三个分支的判定顺序有个细节**：`存在` 之后，先判 `hasVector && hasKeyword`（混合），再判 `hasVector`、`hasKeyword`。**顺序不能颠倒** —— 若先判 `hasVector`，两路都命中的会被误标为"向量"。

### 6.2 三支实测（用真实 API 数据喂给等价的 Python 实现）

| 提问 | `sources`（后端真实返回） | 渲染 Tag | `rerank` Tag |
| --- | --- | --- | --- |
| `接口` | `['vector','keyword']` | **混合**（purple） | （不渲染） |
| `接口怎么认证` | `['vector']` | **向量**（blue） | （不渲染） |

**第三个分支（仅关键词 → 关键词/orange）在当前语料下没触发** —— 因为实测所有查询的首位切片都被向量路召回了。

### 6.3 ⚠️ 前端有一段"超前代码"，运行时安全

`CitationList.tsx` L60、L71-75：

```typescript
const rerankScore = c.retrieval_meta?.rerank_score        // L60
...
{rerankScore != null ? <Tag color="gold">{`rerank ${rerankScore.toFixed(2)}`}</Tag> : null}
```

**后端从不发送 `rerank_score`** → `undefined` → `!= null` 为 false → **Tag 不渲染**。生成类型的注释里写明这是「**第 8 章** reranker 精排分」，属**前瞻预留**。

**生产构建通过**（`✓ built in 1.56s`），`tsc -b` 与 `eslint` 均 exit=0。

---

## 七、⚠️ 两条必须记住的教训

### 7.1 本项目 `tsc --noEmit` 是**假信号**，必须用 `tsc -b`

`frontend/tsconfig.json` 是**项目引用**结构：

```json
{
  "files": [],                              // ← 空！什么都没包含
  "references": [
    { "path": "./tsconfig.app.json" },
    { "path": "./tsconfig.node.json" }
  ]
}
```

所以 `npx tsc --noEmit` **什么都不检查**就返回 0。**本归档初稿曾据此得出"前端类型干净"的结论，那是错的。**

**正确命令**（也是 `package.json` 里 `build` 用的）：

```json
"build": "tsc -b && vite build"
```

**实测对比**：

```
npx tsc --noEmit   ->  exit=0（什么都不检查）
npx tsc -b         ->  真正按 references 检查
```

**验证方法**：删掉一个类型字段后，只有 `tsc -b` 会报 `error TS2339`（`exit=2`），`tsc --noEmit` 仍然 exit=0。

### 7.2 ⚠️ `npm run gen:api` 现在**不能跑**（实测证据）

教程说"后端 schema 改完后，前端 OpenAPI 类型重新拉一遍接口"（`cd frontend && npm run gen:api`）。**在教程语境下这是对的**，但**本项目的前端比教程超前了几期**，所以现在跑会**反向丢字段**。

它的配置是**全量覆盖**：

```typescript
{
  input: 'http://localhost:8000/openapi.json',
  output: 'src/client',          // ← 直接覆盖 src/client
}
```

**预演实测**（把 spec 抓到本地生成到临时目录做对比，未覆盖真文件）：

```
现有 types.gen.ts: 2588 行  →  重生成后 1115 行
类型声明: 142 个消失，其中 17 个被前端真正引用（25 个文件级引用）
```

**17 个被引用的消失类型**：

```
AgentStep(4处) / UserRead / EvaluationItemRead / UserCreate / VerifyResultRead
RoleRead / LoginResponse / MeResponse / EvaluationItemUpdate / EvaluationRunRead
RoleCreate / RoleUpdate / AssignRolesRequest / UserUpdate
ConversationListItem / IngestionTaskRead / EvaluationRunListItem
```

**它们分属后端尚未实现的整块功能**：后端当前只有 15 条路由、4 个 tag（`health` / `documents` / `document-uploads` / `chat`），**没有**认证、用户、角色、评测、摄取任务、会话列表。

**实测报错**（删掉 `rerank_score` 后跑真实构建链）：

```
src/components/CitationList.tsx(60,45): error TS2339:
  Property 'rerank_score' does not exist on type 'RetrievalMeta'.
tsc -b exit=2
```

**`npm run build` 直接失败。**

### 7.3 那些超前字段分别属于哪一期（按课程大纲推算）

| 字段 | 引入期 | 依据 |
| --- | --- | --- |
| `agent_steps` | **第 7 期** Agentic RAG | 多步推理的步骤记录 |
| `rerank_score` | **第 8 期** 检索链路优化与答案可信度 | 前端类型注释写明"第 8 章 reranker 精排分"；大纲第 8 期正好是"检索链路优化" |
| `verify_result` | **第 8 期** | 同上，"答案可信度"即 verifier |
| `trace_id` / `trace_url` | **第 9 期** 可观测性 | 追踪链路 |
| `cache_hit` | **第 12 期** 缓存、限流… | 缓存命中标志 |

**⚠️ 关键结论比"第几期"更麻烦**：这 6 个字段**分散在第 7、8、9、12 期**，**任何单次 `gen:api` 都只能让"已实现的那部分"对齐，同时必然删掉"还没实现的那部分"**。

### 7.4 本批次选定的方案：**保守方案（不跑 `gen:api`）**

| 方案 | 做法 | 评估 |
| --- | --- | --- |
| A. 每次生成后删/注释超前 UI | 每跑一次 `gen:api` 就注释掉引用未来字段的 UI | 反复改代码，易漏 |
| **B. 保守：不跑，类型保持超集** ✅ **本批次采用** | 前端类型是后端的**超集**，超前字段运行时为 `undefined` → 条件渲染不显示 | **运行时行为完全正确**（已实测 Tag 渲染正常） |
| C. 建"未实现功能别名层" | 把 17 个消失的类型搬到不被生成覆盖的文件 | 约 15 个文件、25 处引用要改，成本高 |

**选 B 的理由**：

```
前瞻字段是"超集" → 运行时永远安全（后端不发 = undefined → 条件渲染不显示）
唯一的风险是 gen:api 把超集砍成"=" → 超前 UI 编译失败
→ 所以只要【不跑 gen:api】，就不会有任何问题
```

**时机**：等**第 12 期之后**（6 个字段后端全部具备）再跑一次 `gen:api`，届时**完全对齐**。

---

## 八、当前状态

```
✅ 第 11 章 配置项（retrieval_recall_top_k=20 / rrf_k=60，注释对齐教程）
✅ 第 12 章 RetrievalMeta + CitationRead.retrieval_meta + _parse_retrieval_meta（8/8 实测）
✅ 第 13 章 前端 formatSourceTag（已预置，三支实测，构建通过）
────────────────────────────────────────────────────────────
⬜ npm run gen:api（推迟到第 12 期之后，见 §7.2）
⬜ 前端未实现页面的 404（登录/用户/角色/评测 5 个页面，后端无对应路由）
⬜ 关键词腿 AND 语义导致多词查询召回失效（建议先用 retrieval_meta 统计占比）
⬜ 宽兜底的告警层
```

---

## 九、自测题（5 道）

**Q1（设计题）**　`RetrievalMeta` 的六个字段**全部**有默认值（`sources` 是 `default_factory=list`，其余是 `= None`）。

请回答：为什么必须这样？**并追问**：这个设计与第 10 章"刻意保留 `None` 键"是同一个目的的两个环节 —— 请说明它们如何串起来让前端只用一个判断就能处理。

**Q2（量纲题）**　`RetrievalMeta` 的 docstring 里写了一句"`vector_score` 绝对值有意义，`keyword_score` 相对值跨 query 不可比"。

请用实测数据说明这句话的依据：

```
'DDR5-4800' 命中 1 篇 → ts_rank = 0.188390
'接口'      命中 16 篇 → ts_rank = 0.086545
```

**并回答**：为什么这个区别直接决定了第 9 章 `_should_refuse` 必须用 `vector_score` 而不能用 `score`？

**Q3（防御题）**　`_parse_retrieval_meta` 写了两层防御：

```python
    if not isinstance(raw, dict):
        return None
    try:
        return RetrievalMeta.model_validate(raw)
    except Exception:
        return None
```

请回答：

1. **第一层**（`isinstance`）挡住的是什么？如果去掉它会怎样？
2. **第二层**（`try`）挡住的是什么？如果去掉它会怎样？
3. 由此得出一条**关于反序列化路径的通用原则**。

**Q4（工具题）**　本项目里：

```
npx tsc --noEmit   ->  exit=0
npx tsc -b         ->  真正检查
```

1. 为什么 `tsc --noEmit` 什么都没检查？（看 `tsconfig.json` 的结构）
2. 如果你删掉一个类型字段，哪个命令会报 `error TS2339`？另一个会怎样？
3. 由这个教训能总结出什么**验证纪律**？

**Q5（判断题）**　教程说"后端 schema 改完后，前端 `npm run gen:api` 重新拉一遍类型"。

现在跑这条命令会发生什么？请回答：

1. 它会**丢掉什么**？（给出实测数字）
2. 那些丢掉的东西为什么前端**还有代码在用**？
3. 它们分别属于未来的**第几期**？
4. 为什么"6 个字段分散在多期"这件事让问题**比想象中麻烦**？
5. 本批次选了哪个方案，理由是什么？

---

## 十、批改记录（待补）

> 本节待作答后填写。

---

## 十一、一句话总结

> 第 11-13 章把混合检索的成果**从后端推到了前端**：配置项给出 `recall_top_k=20`（下限，且必须大于 `retrieval_top_k`，
> 否则融合没素材）与 `rrf_k=60`；`RetrievalMeta` 把六个调试字段暴露到 OpenAPI，
> 其 docstring 点明**只有 `vector_score` 是绝对值、可跨查询比较**，这正是第 9 章拒答口径必须用它、
> 不能用 `score`（RRF 分，上限仅 `2/(k+1)`）的根本原因；`_parse_retrieval_meta` 用**两层防御**
> （`isinstance` 挡 `None`/脏数据、`try` 挡字段类型不符）确保**一条历史脏数据不会让整个会话详情 500**，
> 实测 8/8；前端 `formatSourceTag` 已预置且与教程一致，实测「混合/向量」两支在真实数据下正确渲染。
> 本批次还固化了两条**工具层面的教训**：
> **① 本项目 `tsc --noEmit` 是假信号**（根 `tsconfig.json` 的 `files: []` 导致它什么都不检查），必须用 `tsc -b`；
> **② `npm run gen:api` 现在不能跑** —— 实测它会让 `types.gen.ts` 从 2588 行缩到 1115 行、
> 丢掉 142 个类型（其中 17 个被前端引用、分属第 7/8/9/12 期未实现的功能），
> 并直接导致 `CitationList.tsx` 报 `TS2339`、构建失败。
> **本批次采用保守方案：不跑 `gen:api`，让前端类型保持后端的超集** —— 超前字段运行时是 `undefined`、
> 条件渲染不显示，因此**行为完全正确**；待第 12 期之后所有字段齐备，再跑一次实现完全对齐。

---

## 十二、关联文件索引

| 文件 | 关键位置 | 说明 |
| --- | --- | --- |
| `backend/app/core/config.py` | L156-166 | 混合检索配置（`retrieval_recall_top_k` L163 / `rrf_k` L166） |
| `backend/app/core/config.py` | L139 / L143 | `retrieval_top_k=5` / `retrieval_min_score=0.6` |
| `backend/app/api/schemas/chat.py` | **L68-98** | **`RetrievalMeta` 模型（六字段 L87-97）** |
| `backend/app/api/schemas/chat.py` | L100-125 | `CitationRead`（L125 加 `retrieval_meta`） |
| `backend/app/api/schemas/chat.py` | L128-143 | `from_orm`（L142 调解析函数） |
| `backend/app/api/schemas/chat.py` | **L146-161** | **`_parse_retrieval_meta`（L155 第一层 / L159 校验 / L160 第二层）** |
| `frontend/src/components/CitationList.tsx` | L19-29 | `SourceTagMeta` + `formatSourceTag`（三分支 L25-27） |
| `frontend/src/components/CitationList.tsx` | L59-75 | 调用与 Tag 渲染（L60 读 `rerank_score`，L66-70 渲染来源 Tag） |
| `frontend/src/client/types.gen.ts` | L1069-1100 | 前端 `RetrievalMeta`（含前瞻字段 `rerank_score` L1100） |
| `frontend/src/client/types.gen.ts` | L15 / L1301 | `AgentStep` / `VerifyResultRead`（前瞻类型） |
| `frontend/tsconfig.json` | 全文 | 项目引用结构（`files: []` → `tsc --noEmit` 失效的根因） |
| `frontend/package.json` | L11 | `"gen:api": "openapi-ts"` |
| `frontend/openapi-ts.config.ts` | 全文 | `input: localhost:8000/openapi.json` / `output: src/client`（全量覆盖） |
| `backend/app/services/chat_service.py` | L59-98 | `_build_retrieval_meta`（与 schema 字段一一对应） |
| `backend/app/workflows/nodes/retrieve.py` | L86-115 | `_should_refuse`（用 `vector_score` 的依据） |
| `backend/app/db/models.py` | L827-831 | `AnswerCitation.retrieval_meta`（JSONB / nullable） |
