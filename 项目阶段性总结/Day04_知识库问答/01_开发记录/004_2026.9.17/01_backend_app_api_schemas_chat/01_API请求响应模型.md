# 01_API 请求响应模型

> 章节：Day04 知识库问答 · 后端实现 · 第 11 章
> 对应文件：`backend/app/api/schemas/chat.py`
> 记录日期：2026.09.17

---

## 一、本章要解决的问题

Service 层现在能跑了，但它返回的是 **SQLAlchemy 的 ORM 实体**。如果直接把 ORM 对象交给 FastAPI 序列化，会出三个问题：

```
① 全字段泄出     Conversation.messages、Message.conversation 这些关系字段会被一并序列化
                 → 可能递归成环，也可能把内部数据/敏感字段暴露出去
② 字段不可控     ORM 改了字段名，前端接口契约就跟着碎
③ 无文档        OpenAPI 里没有类型信息 → 前端只能靠猜，或手写 TypeScript 类型
```

所以要在 HTTP 边界放一层**契约模型（DTO）**：**进来什么形状、出去什么形状，由这一层说了算**，与内部 ORM 解耦。

教程原文强调的收益：

> 有了这一步，FastAPI 自动生成的 OpenAPI 文档就能让前端的类型生成工具产出精确的 TypeScript 类型。

**这就是为什么字段类型要写得那么细（`Literal`、`UUID | None`、`min_length`）——它们不只是运行时校验，更是给前端生成类型的源头。**

---

## 二、六个模型逐个看

### 2.1 `ConversationCreate` —— 请求体

```python
title: str = Field("新对话", min_length=1, max_length=256)
```

三类校验一次到位：

```
默认值 "新对话"      → 前端不传也能用
min_length=1        → 挡住空字符串标题
max_length=256      → 挡住超长标题（数据库列是 String(256)，超了会插入失败）
```

> **`max_length=256` 与数据库 `String(256)` 是对齐的。** 在 API 层挡住优于让数据库抛 `DataError`——前者返回 422 且信息清晰，后者是 500。

### 2.2 `ConversationRead` —— 响应体（最简单的一个）

```python
model_config = ConfigDict(from_attributes=True)

id: UUID
title: str
created_at: datetime
updated_at: datetime
```

**`from_attributes=True` 是本章的关键开关**：让 Pydantic 按属性名直接从 ORM 实体取值。

```python
ConversationRead.model_validate(conversation_orm)   # 不需要手写转换函数
```

**但它只在"字段名与 ORM 属性名完全一致"时够用**。下面两个模型都不是。

### 2.3 `CitationRead` —— 引用快照（注意可空性）

```python
id: UUID
ordinal: int
document_id: UUID | None = None      # ← 注意 None
chunk_id: UUID | None = None         # ← 注意 None
document_name: str
page_no: int | None = None
quote: str
```

#### 为什么 `document_id` / `chunk_id` 必须是 `UUID | None`

这不是随手写的，是被数据库约束**倒逼**出来的。见 `models.py` 中 `AnswerCitation` 的模型说明：

```
【软关联置空 (ON DELETE SET NULL)】：外键 document_id 与 chunk_id 必须声明为 nullable=True，
一旦关联的文档或分块被删除，数据库仅把外键字段置空，保全本引用历史明细本身不被级联误删。
```

**完整因果链：**

```
数据库约束 ON DELETE SET NULL
  → 外键可为空
  → API 契约必须允许 None
  → 否则读取时校验失败
```

具体失败过程：

```
用户删文档 → 数据库把 answer_citations.document_id 置为 NULL
          → 但引用行本身还在（故意保全历史）
          → 读取会话历史时，引用对象带着 document_id=None 返回
          → Pydantic 校验：声明的是 UUID（非空）→ ValidationError
          → 整个「拉取会话历史」接口 500
```

> **⚠️ 冒烟测试为什么发现不了**：开发阶段数据少、基本不删文档，外键永远有值，声明成非空也能跑通。**上线后用户删了文档，历史接口才崩。**

#### 为什么 `document_name` / `page_no` / `quote` 是"快照冗余"

外键虽然被置空了，引用卡片还得显示来源。所以删文档前就把这些信息抄一份存进引用表——**外键可断，快照永存。**

```python
@classmethod
def from_orm(cls, citation: AnswerCitation) -> "CitationRead":
    return cls(id=..., ordinal=..., document_id=..., ...)
```

### 2.4 `MessageRead` —— 消息（含角色过滤）

```python
@classmethod
def from_orm(cls, message: Message) -> "MessageRead":
    return cls(
        ...
        citations=(
            [CitationRead.from_orm(c) for c in message.citations]
            if message.role == "assistant"
            else []
        ),
    )
```

**为什么按角色过滤**：只有 assistant 消息会产生引用（user / system 不会）。两个作用：

1. **防脏数据**：万一某条 user 消息因异常数据关联了引用，不会透出到前端
2. **语义清晰**：前端拿到 user 消息时不带 `citations` 字段，不用判空

#### ⚠️ 隐藏前置条件

```
调用方查询消息时必须已预加载 citations 关系
（conversation_repo.list_messages 内部已使用 selectinload）
否则在异步环境下访问 message.citations 会触发懒加载并抛出 MissingGreenlet
```

**为什么是 `MissingGreenlet`**：

```
SQLAlchemy 默认 lazy="select"（懒加载）
  → 访问 message.citations 时才去查数据库
  → 但异步 session 里，发 SQL 需要 await
  → 而 `message.citations` 是普通属性访问，没法 await
  → SQLAlchemy 试图在同步上下文里发 SQL
  → 报 MissingGreenlet

关键：这不是"查得慢"，而是"根本查不了"。异步 ORM 下懒加载是硬性禁止的。
```

**这个条件在第 9 章的哪段代码保证**——`conversation_repo.py`：

```python
stmt = (
    select(Message)
    .where(Message.conversation_id == conversation_id)
    .order_by(Message.created_at.asc(), Message.id.asc())
    .options(selectinload(Message.citations))    # ← 就是这一行
)
```

`selectinload` 会在主查询之后补一条 `IN (...)` 查询，把 `citations` 一次性装进内存。之后 `message.citations` 只是读内存，不触发 SQL，因此安全。

> **两处代码强耦合**：`conversation_repo.list_messages` 的 `selectinload` 与 `MessageRead.from_orm` 的 `message.citations` 必须同时成立。**如果哪天有人"优化"掉那句 `selectinload`，本章这行代码立刻炸。**

### 2.5 `ConversationDetail` —— 组合响应

```python
conversation: ConversationRead
messages: list[MessageRead]
```

**这是"会话详情 = 会话主体 + 全部历史消息"的接口形状**，一次请求把前端回放会话所需数据全给出去，避免前端发两次请求。

### 2.6 `ChatRequest` —— 流式问答请求体

```python
question: str = Field(min_length=1, max_length=2000)
```

```
min_length=1    → 拦截空提问，避免白费一次向量检索 + 大模型调用
max_length=2000 → 成本保护：控制上下文预算，
                  防止超长提问挤占「参考资料 + 历史消息」的 Token 空间
```

第二点是"成本保护"：prompt 里要同时装 5 个检索片段 + 5 轮历史，提问本身要是 10000 字，模型上下文直接爆。

---

## 三、两个开发陷阱

### 陷阱 1：`Literal` 不是为了好看

```python
MessageRoleValue = Literal["user", "assistant", "system"]
```

对比实测的 OpenAPI 输出：

```python
role: str               → {"type": "string"}
                          前端生成: role: string
                          → 写 role='asistant'（拼错）编译期不报错，运行时才炸

role: MessageRoleValue  → {"type": "string", "enum": ["user","assistant","system"]}
                          前端生成: role: 'user' | 'assistant' | 'system'
                          → 写错值 TypeScript 直接编译报错
```

**收益的三个层次：**

1. **前端类型安全**：拼错的角色值在编译期就被挡住
2. **Swagger 可读**：`/docs` 里直接显示候选值，联调不用翻代码
3. **文档即契约**：后端加一个角色，前端重新生成类型后立刻能发现哪里需要处理

**为什么用 `Literal` 而非 `Enum`**：注释里写明"用 Literal 而非 Enum，使 OpenAPI 直接输出枚举候选值字符串，前端无需额外映射"。若用 Python `Enum`，OpenAPI 里会变成 `$ref` 引用独立 schema，前端生成器要额外处理映射。

### 陷阱 2：这些模型在写路由之前不会出现在 OpenAPI 里

验证时实测：

```
在真实 app.main 里查 OpenAPI components.schemas
  ConversationRead   *** 缺失 ***
```

**这不是 bug**——FastAPI 只为**被路由引用过的模型**生成 schema。第 12 章的 SSE 路由还没写，所以这六个模型现在"没人用"。等路由接上会自动出现。

> 如果现在去 `/docs` 找它们，找不到是正常的。

---

## 四、自测题与批改记录

### 题目与作答

| # | 题目 | 作答 | 判定 |
| --- | --- | --- | --- |
| 1 | `document_id` 为什么必须 `UUID \| None`？完整因果链，为何开发期难发现？ | "用户删掉之后读取历史会话的时候可能报 ValidationError" | ⚠️ 现象对，漏了"为何难发现" |
| 2 | `ConversationRead` 用 `from_attributes` 就够，为何另两个要手写 `from_orm`？ | "不知" | ❌ |
| 3 | 直接返回 ORM 实体不行吗？DTO 买到了什么（至少两点）？ | "首先防止全字段泄出，其次防止字段不可控" | ✅ 对一半，漏第三点 |
| 4 | `message.citations` 那行依赖什么前置条件？不满足报什么错？谁保证的？ | "不知" | ❌ |
| 5 | `MessageRoleValue` 用 `Literal` 而非 `str` 的实际收益？ | "不知" | ❌ |

### 逐题批改

**第 1 题 —— 现象对，漏了最关键的一半**

题目还问了"为什么开发阶段不容易发现"，这才是重点：

```
开发阶段：数据少、基本不删文档
       → document_id 永远有值 → 声明成非空也能跑通 → 一切正常 ✓

上线之后：用户删了文档
       → ON DELETE SET NULL 生效，外键被置空
       → 引用行仍保留（故意保全历史）
       → 校验失败 → 整个接口 500 ✗
```

**要记住的规律：数据库上的约束，会反向决定 API 契约的类型。** 写 DTO 时不能只看"业务上会不会为空"，还要看**数据库层面允不允许为空**。

**第 2 题 —— 本章核心之一**

分工如下：

**`from_attributes=True` 只在"字段名与 ORM 属性名一一对应、且不需要加工"时够用。**

```
ConversationRead:
  id          ←→  conversation.id           ✓ 同名
  title       ←→  conversation.title        ✓ 同名
  created_at  ←→  conversation.created_at   ✓ 同名
  updated_at  ←→  conversation.updated_at   ✓ 同名
  ⇒ 一个开关搞定
```

**另两个有"转换逻辑"，配置开关表达不了：**

```
CitationRead:
  ✗ 需要显式逐字段映射（未启用 from_attributes）

MessageRead:
  ✗ citations 需要"嵌套转换 + 条件过滤"
    ORM:      message.citations 是 list[AnswerCitation]（ORM 实体列表）
    响应需要:  list[CitationRead]（DTO 列表）      ← 需逐个转换
    并且:     if message.role == "assistant"      ← 还需条件过滤
```

**判断标准：**

| 情况 | 用什么 |
| --- | --- |
| 字段名一一对应、直接取 | `from_attributes=True` |
| 需要嵌套转换 / 条件过滤 / 改名 | **手写 `from_orm`** |

> **⚠️ 手写映射的隐患**：Pydantic 的 `cls(...)` **不会**帮你校验字段名拼写。字段越多越容易漏，所以更稳的是显式列举 + 配套测试。

**第 3 题 —— 对一半，漏了最重要的动机**

作答的两点正确（防全字段泄出、防字段不可控）。漏的第三点是教程原文强调的：

> **契约驱动的前端 TypeScript 类型生成。**

```
后端写 Pydantic 模型
  → FastAPI 生成 OpenAPI JSON
  → openapi-ts.config.ts 跑生成
  → frontend/src/client/types.gen.ts 产出精确 TS 类型
  → 前端调用 API 时字段名/类型错了直接编译报错
```

> **顺带对比**：`documents.py` 的 `DocumentRead` 更进一步——**完全不输出 `cos_object_key` / `cos_bucket` / `embedding`**，这是刻意的安全剥离（注释写着"出参 DTO 严格隐蔽底层 COS 存储桶、物理路径与高维 Embedding 向量"）。所以"防泄出"不只是防字段冗余，还是防敏感信息外露。

**第 4 题 —— 本章核心之二**

**前置条件：`message.citations` 必须已被预加载**（否则 `MissingGreenlet`，见 2.4 节的完整机制）。

**保证它的代码**：`conversation_repo.py` 的 `.options(selectinload(Message.citations))`。

**两处代码强耦合**，改一处要记得另一处。这也是为什么在 `chat.py` 的 docstring 里显式写下了这个前置条件——**让下一个改代码的人知道这里有耦合**。

**第 5 题 —— 见第三节"陷阱 1"**

核心：`Literal` → OpenAPI 输出 `enum` 候选值 → 前端生成**联合类型** → 拼错的值在编译期被挡住。

---

## 五、本章两条规律

### 规律一：数据库约束会反向决定 API 契约

```
ON DELETE SET NULL → 外键可空 → DTO 必须 | None
```

写 DTO 时要同时看"业务上会不会为空"和"数据库层面允不允许为空"。

### 规律二：异步 ORM 下，关系读取必须"预加载在前、访问在后"

```
Repository 层   selectinload(...)   ← 保证已装进内存
      ↕ 强耦合
Schema 层       entity.relation     ← 安全访问
```

第 4 题就是这条规律的具体体现。

---

## 六、验证记录

代码写入后实测 6 项：

```
1) ConversationRead.model_validate(ORM)  → OK, created_at 类型 datetime
2) MessageRead.from_orm                  → assistant citations=1 / user citations=0
                                           ★ 按角色过滤生效
3) 引用快照字段                           → document_id=None chunk_id=None 正常容纳
                                           ★ 模拟原文已删除的置空场景
4) model_dump(mode='json')               → OK, UUID/datetime/None 全部可序列化
5) ChatRequest 边界                       → 空串、2001 字符 均被 ValidationError 拦截
6) 数据库清理                             → conversations=0 messages=0 citations=0
```

**关键一条**：全程**没有触发 Pydantic 弃用告警**（验证时把 `DeprecationWarning` 设为 `error` 来暴露）。因为 Pydantic v2 里 `from_orm` 已是弃用方法，覆盖它理论上存在风险——实测确认此用法安全。

**OpenAPI 生成验证**（用临时 app，因为这些模型尚未被路由引用）：

```
MessageRead.role          → {'enum': ['user','assistant','system']}   ★ Literal 正确导出
CitationRead.document_id  → {'anyOf': [uuid, null]}                    ★ 可空正确表达
ChatRequest.question      → {'minLength': 1, 'maxLength': 2000}
ConversationDetail        → 正确 $ref 嵌套
```

---

## 七、对教程代码的两处改进

**改进 1：去掉 `# type: ignore`，改为真实类型注解**

教程原文：

```python
def from_orm(cls, citation) -> "CitationRead":  # type: ignore[no-untyped-def]
```

参数没标类型，所以要用 `ignore` 压住类型检查器。实际实现改为：

```python
def from_orm(cls, citation: AnswerCitation) -> "CitationRead":
def from_orm(cls, message: Message) -> "MessageRead":
```

并补充 `from app.db.models import AnswerCitation, Message`。

> **用类型注解代替 ignore 更干净**：类型检查器能真正校验转换逻辑（比如 `citation.ordinal` 写错名字会立刻报错，而不是被 ignore 掩盖）。

**改进 2：补充图里没有的说明**

- `CitationRead` 为什么必须是 `UUID | None`（否则原文删除后序列化直接报校验失败）
- `MessageRead.from_orm` 的**前置条件**：调用方必须已预加载 `citations`
- `ChatRequest` 的 `min_length=1` 不只是格式校验——它**拦截空提问，避免白费一次向量检索 + 大模型调用**

---

## 八、一句话总结

> DTO 层在 HTTP 边界把内部 ORM 与对外契约解耦，买到三样东西：**防字段泄出、字段可控、契约驱动前端类型生成**；写法上按"字段是否同名直取"选择 `from_attributes` 或手写 `from_orm`；而 `UUID | None` 与 `Literal` 这些看似琐碎的类型选择，分别由**数据库的 `ON DELETE SET NULL` 约束**与**前端的编译期类型安全**反向决定。

---

## 九、关联文件索引

| 文件 | 关键位置 | 说明 |
| --- | --- | --- |
| `app/api/schemas/chat.py` | L32 | `MessageRoleValue = Literal[...]` |
| `app/api/schemas/chat.py` | L38 / L42 | `ConversationCreate` 与标题约束 |
| `app/api/schemas/chat.py` | L45 / L52 | `ConversationRead` 与 `from_attributes=True` |
| `app/api/schemas/chat.py` | L63 / L81-82 | `CitationRead` 与可空外键 |
| `app/api/schemas/chat.py` | L87-88 | `CitationRead.from_orm` |
| `app/api/schemas/chat.py` | L108 / L118-119 | `MessageRead` 与 `from_orm` |
| `app/api/schemas/chat.py` | L141 | 按角色过滤引用 |
| `app/api/schemas/chat.py` | L150 | `ConversationDetail` |
| `app/api/schemas/chat.py` | L157 / L165 | `ChatRequest` 与 `question` 约束 |
| `app/db/repositories/conversation_repo.py` | L81 | `selectinload(Message.citations)` 预加载 |
| `app/db/repositories/conversation_repo.py` | L79 | `created_at + id` 稳定排序 |
| `app/db/models.py` | L681 | `citations` 关系 `order_by=AnswerCitation.ordinal` |
| `app/db/models.py` | L696 | `ON DELETE SET NULL` 软关联说明 |
| `app/db/models.py` | L734 / L743 | 两个外键的置空策略注释 |
| `app/api/schemas/documents.py` | — | 对比参考：`DocumentRead` 的敏感字段剥离（`cos_object_key` / `cos_bucket` / `embedding`） |
