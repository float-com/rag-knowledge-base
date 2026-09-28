# 03_入参出参 Schema

> 期：**Day13 · MCP Server 集成**
> 章：**第 3 章 定义工具的入参 / 出参 schema**
> 模块：`backend/app/mcp_server/schemas.py`（新增）
> 记录日期：2026.09.28
> 本篇定位：**复习材料** —— 模型逐个讲解 + 41 个字段的注释口径 + 实测数据 + 排错速查。
> 归档目录：`Day13_MCP Server 集成/02_2026.9.28/02_backend_app_mcp_server_schemas/`

---

## 〇、三分钟速览（先读这一节）

```
本章要解决的一件事：
        工具对外长什么样 —— 字段叫什么、什么类型、哪些必填、能填什么值。

本章的唯一设计决策：
        专门为 MCP 另建一份 schema，不复用 api/schemas/documents.py

本章的规模：
        7 个 Pydantic 模型 / 41 个字段，全部逐字段带注释（脚本审计覆盖率 100%）

本章最该记住的三句：
        ① 同一个业务能力，面向不同消费者应有各自的门面（前端 vs 外部 Agent）
        ② 复用类型别名可以（防状态口径漂移），复用整个模型不可以（会把内部字段带给 Agent）
        ③ ConfigDict(from_attributes=True) 全模块只有 MCPDocumentItem 有
           —— 其余 6 个模型不能直接 model_validate(orm 对象)
```

---

## 一、本章在整条链路里的位置

```
第 2 章（auth）    解决了"现在是谁在调"
第 3 章（本章）    解决"这个工具对外长什么样"
第 4 章及以后      按这份契约把 5 个工具实现出来
```

本章全是**纯 Pydantic 模型**：不碰数据库、不写业务、不引用 Session。
写完就交给框架 —— **FastMCP 会自动把这些模型抽成 JSON Schema 暴露给 Agent**，
模块里不需要写任何 `model_json_schema()` 之类的调用。

---

## 二、★ 本章唯一的设计决策：为什么另建一份 schema

| | 网页侧契约（`app/api/schemas/*`） | 本章（`app/mcp_server/schemas.py`） |
| --- | --- | --- |
| 读者是谁 | 自家前端（`types.gen.ts` 逐字段对齐） | **外部 Agent**（Cursor / Claude Desktop 的模型） |
| 字段来源 | 往往直接从 ORM 列映射过来 | **只保留 Agent 真正需要的字段** |
| 能否带内部字段 | 可以（前端不管这些） | **绝不可以**：`retrieval_meta` / `rerank_score` / `cos_object_key` 等 |
| 随表结构变化 | 加一列就跟着加一个字段 | **不动**：由 Agent 的需求决定，不由 `documents` 表的列决定 |

一句话：**同一个业务能力，面向不同消费者时应该有各自的门面。**

这是第 11 期的镜像 —— 那时 `api/schemas/auth.py` 的 `UserRead` 必须与前端类型逐字段一致；
现在是 MCP 的字段集合由 Agent 的需求决定。

**好处很实在**：第 12 期给 `documents` 加过 `version` 列，今后再加列（比如多一个内部指标），
MCP 的契约不会被动跟着变 —— 否则每次迁移都要回头想"这列要不要给 Agent 看"。

---

## 三、7 个模型与 5 个工具的对应

| 区块 | 模型 | 服务的工具 | 说明 |
| --- | --- | --- | --- |
| 问答 | `MCPCitation` | `ask_knowledge_base`（出参的子结构） | 一条引用快照；**不单独返回** |
| 问答 | `MCPAnswer` | `ask_knowledge_base` | answer / refused / citations / trace_id |
| 文档 | `MCPUploadResult` | `upload_document` | 回执：document_id / name / status / version / file_hash |
| 文档 | `MCPDocumentItem` | `list_documents`（出参的子结构） | 一份文档的画像；**不单独返回** |
| 文档 | `MCPDocumentList` | `list_documents` | 外壳：items / total / page / page_size |
| 文档 | `MCPDocumentStatus` | `get_document_status` | 入库进度看板：status + 6 个 task 字段 |
| 统计 | `MCPStats` | `get_knowledge_base_stats` | document_count / chunk_count / last_indexed_at |

**7 个模型对应 5 个工具**，因为 `MCPCitation` 与 `MCPDocumentItem` 是"被包在别的模型里"的行模型。

### 3.1 命名约定：为什么统一叫 Result / Item / List / Status，而不是 Read

网页侧出参一律叫 `XxxRead`（FastAPI 惯例）；本章没有用 Read，而是 Result / Item / List / Status ——
因为它们**不是 ORM 的"读模型"**，而是"工具调用的返回值"。
**看到名字就知道自己站在哪一侧**，不会误以为可以拿它去 `model_validate(orm_obj)`
（只有 `MCPDocumentItem` 支持，而且那是靠它自己显式声明的 `from_attributes=True`）。

---

## 四、★ 一条贯穿全章的界线：借类型可以，借模型不行

```python
from app.api.schemas.documents import DocumentStatusValue, IngestionTaskStatusValue
```

这两个是 **Literal 类型别名**，复用它们**是有意的**：

```
状态取值只有一份定义 → 网页侧将来加一个状态，MCP 侧自动跟着对上
（否则就会出现两套"什么算 ready"的口径漂移）
```

但 `api/schemas/documents.py` 里的**模型一个都不能复用** ——
复用了就会把网页侧的内部字段一起带给 Agent，正好违背第二章那条设计决策。

> 这条界线写进了 `schemas.py` 的注释（语法点 6），避免后来人"顺手也把模型 import 过来"。

---

## 五、代码逐段讲解

### 5.1 问答相关

```python
class MCPCitation(BaseModel):
    """问答引用快照。与 SSE / 历史接口的 citation 形状对齐，但只保留外部 Agent 真正需要的字段，
    避免把 retrieval_meta / rerank_score 等内部调试元数据塞给 Agent。"""

    ordinal: int = Field(description="prompt 中给 LLM 的「片段 N」编号，从 1 开始")
    document_id: UUID
    document_name: str
    page_no: int | None = None
    section_path: str | None = None
    quote: str = Field(description="该片段在 prompt 里的原文")
```

```python
class MCPAnswer(BaseModel):
    """`ask_knowledge_base` 出参。"""

    answer: str
    refused: bool = Field(description="是否触发拒答（命中阈值不足或答案校验失败）")
    citations: list[MCPCitation] = Field(default_factory=list)
    trace_id: str | None = Field(default=None, description="LangSmith trace_id，未启用观测时为空")
```

**为什么拒答要显式给一个布尔字段**：拒答时 `answer` 里也是一段人话，
模型容易把"知识库里没有"误当成一个正常答案；单独给结构化标志位，
Agent 才能做确定性判断（据此改问法或直接告诉用户查不到）。

### 5.2 文档相关

```python
class MCPUploadResult(BaseModel):        # upload_document 出参
    document_id: UUID
    name: str
    status: DocumentStatusValue
    version: int
    file_hash: str = Field(description="sha256；文件级幂等键，相同 hash 复用现有文档")

class MCPDocumentItem(BaseModel):        # list_documents 列表项
    model_config = ConfigDict(from_attributes=True)      # ★ 全模块唯一
    id: UUID
    name: str
    status: DocumentStatusValue
    mime_type: str
    size: int
    version: int
    permission_tags: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

class MCPDocumentList(BaseModel):        # list_documents 出参（外壳）
    items: list[MCPDocumentItem]
    total: int
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)

class MCPDocumentStatus(BaseModel):      # get_document_status 出参
    document_id: UUID
    name: str
    status: DocumentStatusValue
    version: int
    error_message: str | None = None
    latest_task_type: Literal["ingest", "reindex"] | None = None
    latest_task_status: IngestionTaskStatusValue | None = None
    latest_task_progress_total: int | None = None
    latest_task_progress_done: int | None = None
    latest_task_error_message: str | None = None
```

**为什么把 `file_hash` 这个"内部键"也给 Agent**：它不是调试信息，而是**幂等语义** ——
Agent 重试上传时能靠它判断"这份文件其实已经在库里"。判断标准始终是"Agent 需不需要"。

**为什么 `latest_task_*` 全部可空**：文档可能**从未入过库**（刚上传、任务还没落库），
此时一个任务都没有；也可能上传任务早已被清理。硬要求非空，
Agent 就会在"没有任务"这种正常状态下拿到报错。

**为什么 `document_id` 与 `latest_task_*` 同时给**：后者让外部 Agent 直接观察 ingest / reindex 进度，
**不必再多调一次 `list_documents`**。

### 5.3 统计相关

```python
class MCPStats(BaseModel):               # get_knowledge_base_stats 出参
    """严格按调用者权限范围统计；admin 视角看全量，普通用户只看自己有权访问的。"""

    document_count: int
    chunk_count: int
    last_indexed_at: datetime | None = Field(
        default=None,
        description="最近一次进入 ready 状态的文档时间；库为空时返回 null",
    )
```

> ⚠️ 口径必须写进 description：不写清楚，模型可能把"最后一次**上传**时间"
> 当成"最后一次**可检索**时间"讲给用户听。

---

## 六、★ 41 个字段的注释口径（两类写法）

脚本审计结果（不是肉眼数）：

```
MCPCitation        字段  6      MCPDocumentList    字段  4
MCPAnswer          字段  4      MCPDocumentStatus  字段 10
MCPUploadResult    字段  5      MCPStats           字段  3
MCPDocumentItem    字段  9
字段总数 41 | 缺注释 0 | 类内重复字段 0
```

### 6.1 第一类：字段名能看懂，但不知道"给 Agent 干什么用" → 写消费场景

| 字段 | 注释 |
| --- | --- |
| `MCPCitation.document_id` | 被引用文档的主键：Agent 想进一步查这份文档时，拿它去调 `get_document_status` |
| `MCPCitation.page_no` | 该片段在第几页；解析不出页码（如纯 markdown、txt）时为 null |
| `MCPAnswer.answer` | 最终答案正文；**拒答时这里放的是拒答话术** |
| `MCPDocumentList.total` | 满足条件的总条数：Agent 据此决定"还要不要再翻一页" |
| `MCPDocumentStatus.latest_task_progress_total` | 任务总工作量（切片总数）：与 `progress_done` 组成进度分母/分子 |

### 6.2 第二类：容易和邻近字段混淆 → 明确划界（这类最值得写）

| 字段对 | 划界注释 |
| --- | --- |
| `updated_at` vs 索引时间 | 最后一次变更时间（含改权限、重建索引）：**注意它不等于最后一次索引完成时间** |
| 文档级 `error_message` vs `latest_task_error_message` | 任务级失败原因（**与文档级不是同一件事，别混用**） |
| `created_at` | 创建时间（UTC）：Agent 回答"最近上传了什么"这类问题时用 |

---

## 七、★ 6 处生僻语法注释（教程截图里没有的讲解）

| # | 语法点 | 讲的是什么 |
| --- | --- | --- |
| 1 | `Field(description=...)` | MCP 侧它**不是普通注释**，而是写给模型的字段说明，会随 JSON Schema 一起到客户端 → 按"给模型讲清楚"的标准写 |
| 2 | `default_factory=list` | 与写 `= []` 的区别；每次构造新造一个空列表，语义更准（官方推荐） |
| 3 | `str \| None + default=None` | ★ **只写类型不等于可空**！不写 `default=None`，字段就变成必填 → 直接关联第 1 章 §7.2：语义缓存命中时 `trace_id` 本来就是空的 |
| 4 | `ConfigDict(from_attributes=True)` | Pydantic v2 的 ORM 模式开关（取代 v1 的内部类 `orm_mode`）。⚠️ 本模块**只有 `MCPDocumentItem`** 需要它 |
| 5 | `Field(ge=1, le=100)` | 数值约束会进 JSON Schema，让模型自己把参数收敛在合法区间，而不是等被服务端拒掉再重试 |
| 6 | `Literal[...]` | 把字符串枚举化，同时充当给模型的"可选项清单"；本章直接复用网页侧的两个 Literal 类型别名 |

---

## 八、自测结果（全部真实执行）

```
py_compile                     通过
7 个模型可构造                 必填/可空与教程一致
MCPAnswer 缺 answer/refused    → 报 2 个字段错（证明它们确实是必填）
MCPUploadResult 缺必填          → 报 5 个字段错
MCPStats 缺必填                 → 报 2 个字段错
MCPDocumentStatus.required     ['document_id','name','status','version']（latest_task_* 六个全可空）
默认值                         citations == []、trace_id is None、
                               page_no / section_path is None、last_indexed_at is None
from_attributes                MCPDocumentItem.model_validate(orm 对象) 实测成功
数值约束                       page_size schema = {"minimum": 1, "maximum": 100}
```

### 8.1 ★ 一处只有验了才看得见的细节

```python
latest_task_type: Literal["ingest", "reindex"] | None = None
```

它在 JSON Schema 里**不是一个 `enum`**，而是被包成：

```json
{"anyOf": [{"enum": ["ingest", "reindex"], "type": "string"}, {"type": "null"}], "default": null}
```

以后讲到"Agent 最终看到什么"时，要按这个形状讲，**不能按 `enum` 讲**。

---

## 九、★ 本章过程记录：一次自己造成又自己抓出来的错误

- 给 41 个字段补注释时，有一次按"整个字段块"做替换，漏掉了块中间的
  `file_hash: str = Field(...)` 那三行，导致 `MCPDocumentStatus` 的 6 个字段
  （`error_message` + 5 个 `latest_task_*`）被**复制了两遍**
- **抓到它的办法**：写脚本审计"类内重复字段"，而不是肉眼复查 ——
  `MCPDocumentStatus` 字段数从 10 变成 16 时立刻暴露
- 修复时又发现：被误删的是【语法点 6：Literal】那段注释 → 已原样放回，本章 6 个语法注释一条没少
- **复盘**：这类"整块替换"最危险的地方不是语法（`py_compile` 依然通过、模型依然能构造），
  而是注释被悄悄删掉、字段被悄悄复制 —— **必须用结构化审计兜住**

---

## 十、排错速查表

| 现象 | 最可能的原因 | 先查哪里 |
| --- | --- | --- |
| `model_validate(orm_obj)` 报字段取不到 | 该模型没开 `from_attributes` | 是否只有 `MCPDocumentItem` 该开（其余模型要在工具里手工拼装） |
| 构造模型时报"字段必填" | 可空字段漏写 `default=None` | 语法点 3 |
| Agent 把"上传时间"讲成"可检索时间" | `last_indexed_at` 的口径没写进 description | `MCPStats.last_indexed_at` |
| 模型把 `page_size` 填成 1000 | 约束没进 schema | `Field(ge=1, le=100)` 是否还在 |
| Agent 拿不到 `trace_id` 就报错 | 该字段被误设成必填 | `trace_id` 的 `default=None` |
| 状态取值出现"网页侧有、MCP 侧没有" | 有人新写了第二份 Literal，没复用类型别名 | 导入是否仍指向 `api/schemas/documents.py` |
| 响应里出现 `cos_object_key` / `rerank_score` | 误复用了网页侧的模型（而不是只借类型别名） | 第四节那条界线 |

---

## 十一、本章地图（配图）

```
05_契约映射_7个Schema与5个工具的对应关系.png
        7 个模型 ↔ 5 个工具的映射；标注出 2 个"被包含"的行模型（不单独对应工具）
06_模型组合_外壳与子结构的关系图.png
        MCPAnswer / MCPDocumentList 是外壳，MCPCitation / MCPDocumentItem 是里面的行
```

原本还有一张"6 个语法工具图"，经评估后**未采用**：它是语法清单而非结构关系，
信息密度低于代码注释，且那 6 条已逐条写进 `schemas.py`。

---

## 十二、自查 5 题

1. 为什么不能直接复用 `api/schemas/documents.py` 里的模型？那为什么又复用了它里面的两个 Literal？
2. `MCPCitation` 和 `MCPDocumentItem` 与另外 5 个模型有什么结构性区别？
3. `str | None` 写了为什么还要写 `default=None`？不写会怎样？（顺便说说它和第 1 章 §7.2 的关系）
4. `latest_task_*` 六个字段为什么全部可空？
5. 为什么 `ConfigDict(from_attributes=True)` 只给 `MCPDocumentItem` 开？其余模型怎么办？

---

## 十三、本章实际新增或修改文件

```
后端
└── backend/app/mcp_server/schemas.py         【新增】★ 本章正文，7 个模型 / 41 个字段

项目阶段性总结
└── Day13_MCP Server 集成/02_2026.9.28/02_backend_app_mcp_server_schemas/
    ├── 03_入参出参Schema.md                  【本文件】
    ├── 05_契约映射_7个Schema与5个工具的对应关系.png
    ├── 06_模型组合_外壳与子结构的关系图.png
    └── 上传日志.md

（注：本章没有改动任何现有接口、没有加依赖、没有加迁移；auth.py 与依赖调整属第 2 章）
```
