# 02_ORM契约、关键词检索与两路检索器

> 章节：Day06_全文检索、混合检索与 RRF · 第 3 章 开发实现 · 第 4-7 步
> 覆盖：ORM 模型同步（`models.py`）、仓储层 `keyword_search`（`chunk_repo.py`）、
> 　　`RetrievedChunk` 契约统一（`vector_retriever.py`）、`KeywordRetriever`（新文件）
> 记录日期：2026.09.20

---

## 一、本批次的四个模块与它们的因果链

教程第 4-7 步是**从数据库到应用再到抽象**的一条链，缺任何一环，第 8 步的 `HybridRetriever` 都写不出来。

```
第4步  数据库列已在（上一节迁移建的），但 ORM 不知道
        → 映射进 ORM，并声明"应用不写它"（Computed）
        ↓
第5步  ORM 有了属性，才能在仓储层写出 @@ / ts_rank 查询
        → 产出 (chunk, rank_score) 元组
        ↓
第6步  两路产出的元组"形状相同、语义不同"
        → 统一成一份契约，把差异塞进受控的调试字段
        ↓
第7步  契约统一了，关键词检索器就能与向量检索器逐行对称
        → 为第 8 步"两路并发 + RRF 融合"备好可互换的零件
```

| 教程步骤 | 实际改动 | 文件 | 行数变化 |
| --- | --- | --- | --- |
| 4. ORM 模型同步 | 加 2 列映射 | `app/db/models.py` | 782 → **839** |
| 5. `chunk_repo` 加检索方法 | 加 `keyword_search` | `app/db/repositories/chunk_repo.py` | 247 → **307** |
| 6. 统一 `RetrievedChunk` | 契约加 6 字段 + 填值 | `app/retrieval/vector_retriever.py` | 90 → **116** |
| 7. `KeywordRetriever` | **新建** | `app/retrieval/keyword_retriever.py` | **77** |

---

## 二、第 4 步：ORM 模型同步（`models.py`）

### 2.1 `content_tsv`：`Computed` 是"应用侧承诺不碰这一列"的机器可读声明

```python
content_tsv: Mapped[Any] = mapped_column(
    TSVECTOR,
    Computed("to_tsvector('chinese_zh', content)", persisted=True),
    nullable=False,
    comment="中文全文检索向量（数据库自动维护）"
)
```

`Computed` 产生两个效果：

**效果 A —— DDL 里长出生成列**（实测编译结果）：

```
content_tsv TSVECTOR GENERATED ALWAYS AS (to_tsvector('chinese_zh', content)) STORED NOT NULL
```

与迁移脚本 `8c44f95568ad` 建的列**逐字一致**，因此 `alembic check` 不会报漂移。

**效果 B —— SQLAlchemy 把它从 DML 里剔除**（这是"灌库代码不用改"的全部原因）：

```
INSERT 含 content_tsv ? False
UPDATE 含 content_tsv ? False
```

SQLAlchemy 编译 INSERT / UPDATE 时逐列检查有无 `computed` 属性，有则跳过。
所以原有的 `DocumentChunk(content=..., embedding=..., extra_metadata=...)` 原样可用。

**若只映射列而不写 `Computed`**，ORM 会尝试 `INSERT INTO ... (content_tsv) VALUES (NULL)`，
而 PostgreSQL 的 `GENERATED ALWAYS` 列拒绝任何显式赋值：

```
ERROR: cannot insert a non-DEFAULT value into column "content_tsv"
```

→ 灌库、重建索引、增量更新**全部失败**。

### 2.2 ⚠️ 教程此处（`Mapped[str]`）会静默推错列类型

教程原文第一个位置参数只给 `Computed`，类型靠左边注解 `Mapped[str]` 兜底。
但 `mapped_column` 解析首参的规则是「先当列名 → 再当类型 → 都不是则忽略」，
`Computed` 对象两者都不是，于是**退回注解 `Mapped[str]` → 列类型定成 `VARCHAR`**：

| 写法 | 编译出的 DDL |
| --- | --- |
| 教程版 `Mapped[str]` | `content_tsv VARCHAR GENERATED ALWAYS AS (...) STORED` ❌ |
| 实际采用 | `content_tsv TSVECTOR GENERATED ALWAYS AS (...) STORED NOT NULL` ✅ |

**为什么这个错误比 ImportError 更危险**：它启动不报错、运行不报错（列类型由数据库说了算），
但 `alembic revision --autogenerate` 会拿 `Base.metadata`（以为是 VARCHAR）比对真实库（tsvector），
**生成一条 `ALTER COLUMN content_tsv TYPE VARCHAR`**，把生成列改坏。

**为什么注解写 `Any` 而不是 `str`**：注解的作用是"首参没给类型时的兜底"。
给了 `TSVECTOR` 之后，需要注解能**容纳**它才不冲突；写 `Any` = "放弃 Python 侧类型约束，
以显式给的 `TSVECTOR` 为准"。

### 2.3 `retrieval_meta`：必须 `nullable=True`

```python
retrieval_meta: Mapped[dict | None] = mapped_column(
    JSONB, nullable=True,
    comment="检索调试元数据（召回来源/各路排名与分数/RRF融合分）"
)
```

表内**已有第 4-5 期落库的历史引用**。给有数据的表加 `NOT NULL` 列只能给默认值或迁移失败，
而 `nullable=True` 在语义上恰好正确：

| 记录来源 | `retrieval_meta` | 含义 |
| --- | --- | --- |
| 第 4-5 期写入的历史引用 | `NULL` | 当时系统还没有这个能力 |
| 本批次之后的新引用 | `{...}` | 已采集 |

`NULL` 表示"信息不存在"，而非"检索来源为空"。

---

## 三、第 5 步：`chunk_repo.keyword_search`

### 3.1 三段结构

```python
async def keyword_search(self, query: str, top_k: int) -> list[tuple[DocumentChunk, float]]:
    tsquery   = func.plainto_tsquery("chinese_zh", query)              # ① 查询串 → tsquery
    rank_expr = func.ts_rank(DocumentChunk.content_tsv, tsquery)       # ② 打分表达式
    stmt = (
        select(DocumentChunk, rank_expr.label("rank"))
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(
            Document.status == "ready",                                 # ③ 状态守卫（与向量路一致）
            DocumentChunk.content_tsv.op("@@")(tsquery),                # ④ 命中过滤
        )
        .order_by(rank_expr.desc())
        .limit(top_k)
        .options(selectinload(DocumentChunk.document))                  # ⑤ 预加载防 N+1
    )
    rows = (await self.session.execute(stmt)).all()
    return [(chunk, float(rank)) for chunk, rank in rows]
```

编译后的 SQL（注意 regconfig 走 bindparam，不是硬编码字符串）：

```sql
SELECT document_chunks.id, ..., document_chunks.content_tsv, document_chunks.created_at,
       ts_rank(document_chunks.content_tsv,
               plainto_tsquery(%(plainto_tsquery_1)s, %(plainto_tsquery_2)s)) AS rank
FROM document_chunks JOIN documents ON documents.id = document_chunks.document_id
WHERE documents.status = %(status_1)s
  AND (document_chunks.content_tsv @@ plainto_tsquery(%(...)s, %(...)s))
ORDER BY ts_rank(...) DESC LIMIT %(param_1)s
```

`plainto_tsquery` 在 SQL 里出现 3 次（SELECT / WHERE / ORDER BY），但**绑定参数只传 2 个**
（`chinese_zh`、查询串）——SQLAlchemy 复用了同一个表达式对象，PostgreSQL 侧也识别为稳定表达式，
保证"匹配条件"与"打分依据"永远是同一套词。

### 3.2 ⚠️ 教程此处 `from sqlalchemy import ts_rank` 会直接 ImportError

实测：

```
>>> [n for n in dir(sqlalchemy) if n.startswith("ts_")]
[]                                     ← 顶层没有任何 ts_ 开头的函数
>>> [n for n in dir(sqlalchemy.dialects.postgresql) if n.startswith("ts_")]
['ts_headline']                        ← 方言层只额外提供了 ts_headline
```

`ts_rank` 必须走通用 `func.ts_rank(...)` 动态构造。
**通用规则**：SQLAlchemy 不可能为 PostgreSQL 每个函数建类，`func.任意函数名()` 是逃生舱
（`ts_rewrite`、`setweight` 同理）；`plainto_tsquery` 则在方言层里有，教程那半句是对的。

### 3.3 `@@` 运算符用 `.op()` 手工装

```python
DocumentChunk.content_tsv.op("@@")(tsquery)
```

SQLAlchemy 未为 `@@` 提供专用方法（不像 `.contains()` / `.like()`）。
**不改成 Python 侧 `if 关键词 in content` 的原因**：那会把全表拉进内存再过滤；
`@@` 下推给 PostgreSQL，走 `ix_document_chunks_content_tsv`（GIN 倒排索引，312 kB）。

### 3.4 `plainto_tsquery` 的收益与代价

| 函数 | 对 `a & b` | 对 `!!` |
| --- | --- | --- |
| `to_tsquery` | 把 `&` 当**布尔运算符**解析 | **直接抛语法错误** |
| `plainto_tsquery` ✅ | 当**纯文本**，切词后用 `&` 拼接 | 当普通字符 |

**收益（稳定性）** —— 9 组畸形输入全部无异常：

| 输入 | 结果 |
| --- | --- |
| `a & b` | OK，2 条 |
| `!!` | OK，0 条 |
| `x:*` | OK，3 条 |
| `2 + 3 = ?` | OK，3 条 |
| `' OR 1=1 --` | OK，0 条 |
| `C++` / `(待定` | OK，0 条 |
| `  ` / `%` | OK，0 条 |

**代价（AND 语义）** —— 多词查询会瞬间归零：

```
'接口'          ->  16 条
'上传 接口'      ->   0 条      ← 没有任何切片同时含这两个词
'上传接口报错'    ->   0 条
```

`plainto_tsquery('chinese_zh', '上传 接口')` 生成 `'上传' & '接口'`，必须**同时命中**。

> **这会成为第 8 步的真问题**：用户问"上传接口报错怎么办"，关键词路返回 0 条，
> 关键词腿整体废掉，混合检索退化成纯向量检索。留待后续处理。

### 3.5 返回值契约与向量路**形状相同、方向相反**

| | `vector_search` | `keyword_search` |
| --- | --- | --- |
| 返回 | `list[tuple[DocumentChunk, float]]` | `list[tuple[DocumentChunk, float]]` |
| 第二个元素含义 | 余弦**距离**（越小越近） | ts_rank（越大越相关） |
| 排序 | `.order_by(distance.asc())` | `.order_by(rank_expr.desc())` |

仓储层不做归一化是对的（不替上层决定语义），但上层必须自己记住方向相反 ——
`VectorRetriever` 里那句 `score=1.0 - distance` 就是在还这笔债。

---

## 四、第 6 步：统一 `RetrievedChunk` 契约

### 4.1 字段清单（13 个：7 必填 + 6 带默认值）

```python
@dataclass(frozen=True)
class RetrievedChunk:
    # ---- 业务字段：7 个必填（第 5 期就有）----
    chunk_id / document_id / document_name / content /
    page_no / section_path / score

    # ---- 调试字段：6 个带默认值（本批次新增）----
    sources: tuple[str, ...] = field(default_factory=tuple)
    vector_rank: int | None = None
    vector_score: float | None = None
    keyword_rank: int | None = None
    keyword_score: float | None = None
    rrf_score: float | None = None
```

### 4.2 为什么 6 个新字段"全都必须带默认值"（Python 硬语法）

```python
@dataclass
class A:
    a: int = 1
    b: int          # ✗ TypeError: non-default argument 'b' follows default argument
```

规则：**一旦有字段带默认值，它后面所有字段都必须带默认值**。
所以新增字段只能整体追加在末尾且全带默认值，这样才能做到**零破坏性**：
`KeywordRetriever` 只填自己那 3 个字段，不必写一堆 `=None` 占位；
若当初插在 `score` 前面，**所有已有调用点全部报错**。

### 4.3 `score` 是"排序位"，`*_score` 是"原始分"

| 字段 | 语义 | 谁用 |
| --- | --- | --- |
| `vector_score` / `keyword_score` / `rrf_score` | **各路的原始分**，量纲互不可比 | 调试面板、`retrieval_meta` 落库 |
| `score` | **本次排序依据的统一别名** | `_merge_chunks` 排序、prompt、前端引用卡片 |

于是**同一份下游代码在三种模式下都成立**：

| 模式 | `score` 等于 | 值域 |
| --- | --- | --- |
| 向量单路 | `vector_score`（余弦相似度） | [0, 1] |
| 关键词单路 | `keyword_score`（ts_rank） | 无上界 |
| 混合检索 | `rrf_score`（融合分） | ≈ [0, 0.033]（k=60） |

**硬约束**：向量单路时 `score` 必须**严格等于** `vector_score`。
因为 `retrieve` 节点的拒答阈值 `settings.retrieval_min_score = 0.6` 是按**余弦相似度**标定的。
故 `vector_retriever.py` 把两个字段写成**同一个表达式**：

```python
                score=1.0 - distance,
                vector_score=1.0 - distance,
```

防止有人日后单独改其中一个，让阈值判定与新字段**悄悄脱钩**。已列为回归项。

### 4.4 `sources` 为什么用 `tuple` 不用 `list`

`frozen=True` 只拦截**属性重新赋值**（`__setattr__`），**不拦截容器内部修改**：

```python
@dataclass(frozen=True)
class X:
    tags: list = field(default_factory=list)

x = X()
x.tags.append("keyword")     # ✓ 成功了！frozen 管不住
x.tags = ["a"]               # ✗ FrozenInstanceError（只有这个被拦）
```

而且 `list` 不可 hash，`RetrievedChunk` 就进不了 `set`。
`tuple[str, ...]` 三个好处：真正不可变、可 hash、成员判断精确
（不会出现 `"vect" in "vector,keyword"` 这种子串误判）。

---

## 五、第 7 步：`KeywordRetriever`（新文件 77 行）

### 5.1 与 `VectorRetriever.search` 只有三处不同

| | `VectorRetriever` | `KeywordRetriever` |
| --- | --- | --- |
| 检索前 | `await get_embeddings().aembed_query(query)` | **无**（不调模型） |
| 调的方法 | `chunk_repo.vector_search(embedding, top_k)` | `chunk_repo.keyword_search(query, top_k)` |
| 填的调试字段 | `sources=("vector",)` + `vector_rank/vector_score` | `sources=("keyword",)` + `keyword_rank/keyword_score` |

**对称性本身就是设计目标**，不是巧合 —— 教程第 7 节末尾点明了目的：

> 这样 `HybridRetriever` 里就能用 `retriever_cls(session).search(query, top_k)` 这种统一调用方式，
> 两路走完全一致的代码路径。

也就是说第 8 步的 `HybridRetriever` 能写成两路共用同一段代码、无需 `if/else` 分支。
**两路返回结构若不同，融合逻辑就得写两套。**

### 5.2 ⚠️ `rank` 与 `rank_score` 是两个不同的东西

实测输出抽成表：

| chunk | `rank`（enumerate 序号） | `rank_score`（ts_rank） |
| --- | --- | --- |
| 第 1 条 | 1 | 0.086545 |
| 第 2 条 | 2 | 0.082746 |
| 第 3 条 | 3 | 0.082746 |
| 第 4 条 | 4 | 0.082746 |
| 第 5 条 | 5 | 0.082746 |

**注意第 2-5 条的 ts_rank 完全相同**，所以：

- 拿 `rank_score` 排序 → 第 2-5 名**无法区分**（并列），先后取决于数据库的偶然返回顺序
- 拿 `rank` 排序 → 严格 1, 2, 3, 4, 5

**这正是 RRF 只吃"排名"不吃"分数"的原因之一**：排名天然消解并列。
RRF 公式 `1/(k + rank)` 里的 `rank` 指的是 **`enumerate` 出来的序号**，绝不是 `rank_score`。

> 教程里该循环变量名为 `ts_rank`，实现时改名为 `rank_score`：
> 因为第 8 步 `1.0 / (k + rank)` 会与分数同屏出现，改名可让"排名"与"分数"一眼可分。
> **纯可读性改动，不影响行为。**

### 5.3 两者没有共享基类（实测）

```
VectorRetriever 的基类 : (<class 'object'>,)
KeywordRetriever 的基类: (<class 'object'>,)
是否互为子类          : False False
```

**现在不抽 `BaseRetriever` 是对的**：

1. 两路实质差异远大于共性（一路有网络 I/O 与模型失败重试，一路纯数据库），抽象类只能约束方法签名；
2. 抽象类会引入"必须实现"的耦合，将来加第三路（图谱检索 / Rerank 后处理）时改基类要动所有子类；
3. Python 是鸭子类型 —— 下一个消费者只需对象**有 `search` 方法**，不需要共享祖先，也因此极好打桩测试。

---

## 六、验证结果（实测，全部通过）

### 6.1 第 4 步 ORM

| # | 检查项 | 结果 |
| --- | --- | --- |
| 1 | `content_tsv` 推断类型 | `TSVECTOR()` ✓ |
| 2 | `computed` 属性 | `persisted=True` ✓ |
| 3 | 编译 DDL | `content_tsv TSVECTOR GENERATED ALWAYS AS (to_tsvector('chinese_zh', content)) STORED NOT NULL` ✓ |
| 4 | **INSERT 是否含 content_tsv** | **False** ✓ |
| 5 | **UPDATE 是否含 content_tsv** | **False** ✓ |
| 6 | `retrieval_meta` | `JSONB` / `nullable=True` ✓ |
| 7 | ORM 属性存在性 | `DocumentChunk.content_tsv` ✓ / `AnswerCitation.retrieval_meta` ✓ |
| 8 | 数据库实际列 | `tsvector` / `is_generated=ALWAYS` ✓ |
| 9 | ORM 实际读一行 | 成功 ✓ |
| 10 | 生成列回填 | `total=259 generated=259` ✓ |

### 6.2 第 5 步 `keyword_search` 真实语料表现

| 关键词 | tsquery 召回 | 最高 ts_rank |
| --- | --- | --- |
| 接口 | 16 | 0.086545 |
| 权限 | 19 | 0.082746 |
| A100 | 9 | 0.082746 |
| B200 | 7 | 0.082746 |
| 上传 | 3 | 0.075991 |
| 考勤 | 3 | 0.090666 |
| 差旅 | 3 | 0.075991 |
| 报错 | 1 | 0.099103 |
| DDR5-4800 | 1 | **0.188390** |
| HNSW | 0 | — |

> **召回数与上一批迁移记录完全一致**（B200 7 / A100 9 / 差旅 3），
> 说明 `func.plainto_tsquery` 与迁移阶段裸 SQL 的 `to_tsquery` 结果口径相同。
>
> **`DDR5-4800` 只命中 1 篇却是最高分**，说明 ts_rank 受**词频**主导、
> 与 IDF（词在多少篇里出现）无关 —— 故 ts_rank **不可跨查询比较**，
> 这正是必须用 RRF（只吃排名）的根据。

其他检查：

```
rank 降序             : True
selectinload 生效     : API接口文档-用户服务.md（可直接读 document.name）
特殊输入（9 组）       : 全部 OK，无异常
AND 语义              : {'接口': 16, '上传 接口': 0, '上传接口报错': 0}
```

### 6.3 第 6-7 步 契约与两路检索器

```
字段数                : 13
前 7 个必填           : True
后 6 个有默认值        : True
```

关键词路（纯数据库，无模型调用）：

```
  rank=1 score=0.086545 sources=('keyword',) vector_score=None rrf=None
  rank=2 score=0.082746 sources=('keyword',) vector_score=None rrf=None
  rank=3 score=0.082746 sources=('keyword',) vector_score=None rrf=None
  rank=4 score=0.082746 sources=('keyword',) vector_score=None rrf=None
  rank=5 score=0.082746 sources=('keyword',) vector_score=None rrf=None

  keyword_rank 连续 1..N : True
  score == keyword_score : True
  score 降序             : True
  frozen 不可变性        : OK（FrozenInstanceError）
  旧 7 字段构造兼容       : True / sources=() / vector_rank=None
```

向量路（**真实 Embedding，dim=1024**）：

```
  rank=1 score=0.658601 vs=0.658601 src=('vector',) kw_rank=None
  rank=2 score=0.652175 vs=0.652175 src=('vector',) kw_rank=None
  rank=3 score=0.636190 vs=0.636190 src=('vector',) kw_rank=None
  rank=4 score=0.634667 vs=0.634667 src=('vector',) kw_rank=None
  rank=5 score=0.632068 vs=0.632068 src=('vector',) kw_rank=None
  耗时 = 1.30s

  score == vector_score  : True（关键回归项）
  score 在 [0, 1]        : True
```

### 6.4 回归（已写好的链路未被破坏）

```
format_context 消费新契约            : OK
全量 import（10 个模块含 app.main）   : 全 OK
retrieve 单路路径                    : 可运行
retrieve 多路路径（multi_query）      : 可运行
_merge_chunks 去重取最高分            : 3 条(含重复) -> 2 条，chunk_id 唯一
```

---

## 七、⚠️ 过程中发现的三个真问题

### 7.1 `import app.workflows.nodes.retrieve as retrieve_mod` 拿到的是**函数**不是模块

`app/workflows/nodes/__init__.py` L16 写了：

```python
from app.workflows.nodes.retrieve import retrieve      # 同名函数覆盖了同名子模块
```

后果：给 `retrieve` 节点打桩做单测时，`retrieve_mod.VectorRetriever = Stub` 实际是
**给函数对象挂属性**，静默无效，测试会假装通过（本批次验证过程中确曾因此误判一次）。
正确姿势：`sys.modules["app.workflows.nodes.retrieve"]`。

### 7.2 `rank` 与 `rank_score` 混用会**静默倒置排序**

若误把 `rank_score` 当排名代入 RRF 分母 `1/(k + rank_score)`：

| chunk | 正确 `1/(60+rank)` | 错误 `1/(60+rank_score)` |
| --- | --- | --- |
| 第 1 条（rs=0.086545） | **0.0163934426** | 0.0166426610 |
| 第 2 条（rs=0.082746） | **0.0161290323** | 0.0166437133 |

```
正确算法得到的顺序:  [1, 2, 3, 4, 5]
错误算法得到的顺序:  [2, 3, 4, 5, 1]      ← 第 1 名被踢到最后
```

原因：**ts_rank 越小 → 分母越小 → 商越大**，是**负相关**，符号反了。

**不报错**：类型合法、分母非零、不越界、`sorted` 照常工作 —— 只是结果错。
这是本批次最需要警惕的一类 bug：**不报错但结果错**，
且因为最终输出是自然语言，人很难一眼看出排序错了。

### 7.3 从外部粘贴代码会带进垃圾标记

`models.py` 的 `Computed` 注释行末尾曾残留 `[cite: 9]`（网页/AI 生成内容的引用角标），
已在归档时清除。**建议**：从外部复制代码后全局搜一次 `[cite:` 与全角引号 `“ ”`，
避免这类标记进入代码库。

---

## 八、当前状态与剩余待实现

```
✅ 迁移（镜像 + content_tsv + GIN + retrieval_meta）  ← 上一批
✅ ORM 模型同步（第 4 步）
✅ chunk_repo.keyword_search（第 5 步）
✅ RetrievedChunk 契约统一（第 6 步）
✅ KeywordRetriever（第 7 步）
⬜ HybridRetriever（第 8 步：asyncio.gather 双路并发 + 各自独立 AsyncSession）
⬜ RRF 融合（k=60）
⬜ retrieve 节点改造接入
⬜ Top-1 拒答口径调整（仅关键词命中要放行）
⬜ retrieval_meta 落库（随 AnswerCitation 写入）
```

---

## 九、一句话总结

> 本批次把"数据库里已存在的检索能力"接进了应用：第 4 步用 `Computed` 声明生成列的维护权归数据库，
> **因此灌库代码一行未改**（实测 INSERT / UPDATE 均不含该列）——但教程的 `Mapped[str]` 写法会让
> ORM 把列类型推成 `VARCHAR`，**不报错却会让后续 autogenerate 生成改坏列类型的迁移**，故显式标注 `TSVECTOR`；
> 第 5 步的 `keyword_search` 用 `plainto_tsquery`（用户输入安全，9 组畸形输入全部无异常）+
> `@@` + `func.ts_rank`，**注意教程写的 `from sqlalchemy import ts_rank` 会直接 ImportError**；
> 代价是 `plainto_tsquery` 的 **AND 语义**会让"上传 接口"这类多词查询归零，是第 8 步要面对的真问题；
> 第 6 步把两路召回统一成一份契约（7 必填 + 6 带默认值的调试字段），
> 其中 `score` 是"排序位"、`*_score` 是"各路原始分"，**向量单路时二者必须严格相等**，
> 否则拒答阈值 0.6 会与新字段悄悄脱钩；
> 第 7 步的 `KeywordRetriever` 与 `VectorRetriever` 逐行对称且**不共享基类**（鸭子类型），
> **关键陷阱是 `rank`（序号）与 `rank_score`（ts_rank）混用**——实测会让 RRF 排序
> 从 `[1,2,3,4,5]` 变成 `[2,3,4,5,1]`，且完全不报错。

---

## 十、关联文件索引

| 文件 | 关键位置 | 说明 |
| --- | --- | --- |
| `app/db/models.py` | L19 / L28 / L38 | `Any` / `Computed` / `TSVECTOR` 导入 |
| `app/db/models.py` | L514-521 | `content_tsv` 生成列映射（`Mapped[Any]` + `TSVECTOR` + `Computed`） |
| `app/db/models.py` | L518 | `Computed("to_tsvector('chinese_zh', content)", persisted=True)` |
| `app/db/models.py` | L827-831 | `retrieval_meta`（JSONB / nullable） |
| `app/db/repositories/chunk_repo.py` | L208-246 | `vector_search`（余弦距离升序，与关键词路方向相反） |
| `app/db/repositories/chunk_repo.py` | L248-307 | `keyword_search` 方法 |
| `app/db/repositories/chunk_repo.py` | L277 / L282 / L293 | `plainto_tsquery` / `func.ts_rank` / `@@` 过滤 |
| `app/retrieval/vector_retriever.py` | L17 | `from dataclasses import dataclass, field` |
| `app/retrieval/vector_retriever.py` | L31-61 | `RetrievedChunk` 契约（13 字段） |
| `app/retrieval/vector_retriever.py` | L52-61 | 本批次新增的 6 个调试字段 |
| `app/retrieval/vector_retriever.py` | L75-116 | `VectorRetriever.search` |
| `app/retrieval/vector_retriever.py` | L108 / L113 | `score` 与 `vector_score` 同一表达式（硬约束） |
| `app/retrieval/vector_retriever.py` | L111 | `sources=("vector",)` |
| `app/retrieval/vector_retriever.py` | L115 | `enumerate(rows, start=1)` 产出 rank |
| `app/retrieval/keyword_retriever.py` | L28-77 | `KeywordRetriever` 全文 |
| `app/retrieval/keyword_retriever.py` | L42-53 | `search` 签名与仓储调用（与向量路对称） |
| `app/retrieval/keyword_retriever.py` | L69 / L72-74 / L76 | `score=rank_score` / 关键词路调试字段 / `enumerate` |
| `app/services/chat_service.py` | L59-87 | `_serialize_citation`（消费 `score`） |
| `app/services/chat_service.py` | L380-394 | `AnswerCitation(...)` 落库处（下一步要加 `retrieval_meta`） |
| `app/workflows/nodes/retrieve.py` | L19-99 | 下一步要改造为双路 + RRF |
| `app/workflows/nodes/retrieve.py` | L55 | 当前拒答判定（下一步要改口径） |
| `app/workflows/nodes/__init__.py` | L16 | `retrieve` 函数遮蔽同名子模块（见 7.1） |
