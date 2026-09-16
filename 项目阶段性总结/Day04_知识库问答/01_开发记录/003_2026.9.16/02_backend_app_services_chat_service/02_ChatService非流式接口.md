# 02_ChatService 非流式接口

> 章节：Day04 知识库问答 · 后端实现 · 第 10 章（非流式部分）
> 对应文件：`backend/app/services/chat_service.py`
> 记录日期：2026.09.16

---

## 一、本章要解决的问题

教程原文的定位说明：

> 节点函数、Prompt、Retriever、Repository 这些部分都实现了。接下来我们用一个 `ChatService` 按顺序驱动它们，把完整的 SSE 流式问答流程串起来。

**注意"驱动"这个措辞。** 前面几章产出了四类互相独立的零件：

```
Repository    读写会话与消息     （conversation_repo / citation_repo）
Nodes         四个流水线节点     （workflows/nodes）
Prompt        组装提示词         （llm/prompts.py）
Retriever     向量检索           （retrieval/vector_retriever.py）
```

它们**各自只干一小块，彼此不知道对方存在**：`retrieve` 节点不知道答案最后要落库，`conversation_repo` 不知道消息从哪来。

ChatService 的职责就是：**按正确顺序把它们串起来，并决定"什么时候调用谁"。**

**判断自己写对没写对的标准**：ChatService 里**不应出现 SQL、不应出现 prompt 字符串、不应出现检索算法**。一旦出现，说明职责漏出去了。

---

## 二、非流式三件套

### 2.1 `create_conversation` —— 创建会话

```python
repo = ConversationRepository(self.session)
conversation = await repo.create(title)
await self.session.commit()                 # 事务在这里提交
await self.session.refresh(conversation)    # 不能省
```

#### 关键点 1：`commit` 为什么在 Service，而不是 Repository

这是项目一贯约定（每个 repo 的 docstring 都写了"仓储层严禁自主调用 commit"）：

```
Repository  →  add + flush   （只把 SQL 推到事务缓冲区，拿到主键 / 触发约束检查）
Service     →  commit        （决定事务边界：什么时候真正落盘）
```

**为什么必须这样分？** 因为真实业务里一个操作常常要跨多个仓储。流式问答就是典型：

```
一次问答要落库三样东西
  1. user 消息        ConversationRepository.add_messages
  2. assistant 消息   ConversationRepository.add_messages
  3. 引用记录 N 条     AnswerCitationRepository.bulk_add
```

若让仓储自己 commit：

```python
await conv_repo.add_messages([user_msg, assistant_msg])   # ← 内部 commit 了
await citation_repo.bulk_add(citations)                   # ← 这里炸了
# 结果：消息已永久落库，引用一条都没有 → 数据半成品，且无法自动修复
```

正确做法是三个仓储都只 `flush`，最后 Service 一次性 `commit`：

```python
await conv_repo.add_messages([user_msg, assistant_msg])   # flush only
await citation_repo.bulk_add(citations)                   # flush only
await self.session.commit()                               # 一起成功，或一起回滚
```

这就是 **Unit of Work（工作单元）模式**。

> **核心原理一句话：事务边界必须由"知道业务整体是什么"的那一层决定。** 仓储只知道"我要写一条消息"，不知道这次业务还要写引用，所以它没资格 commit。

#### 关键点 2：`refresh` 为什么不能省

`Conversation.created_at` / `updated_at` 的定义：

```python
created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

`server_default` 表示**值由数据库生成**。于是：

```
Python 实例化 Conversation()   →  created_at = None（Python 不知道数据库会填什么）
INSERT 执行                     → 数据库写入真实时间 ✓ 数据库侧是对的
commit                         → 但 session 里的 Python 实体对象仍是 None
refresh(conversation)          → 从数据库 SELECT 回来，真值才填回 Python 对象
```

**⚠️ 常见误解纠正**：不是"数据库没刷新"，而是**"内存对象与数据库不同步"**。

SQLAlchemy 的实体是**数据库行的内存副本**，两者可能不一致。凡是"数据库侧生成"的值（`server_default`、数据库触发器、`onupdate=func.now()`），Python 端一律不知道，必须 `refresh` 才能拿到。

**表现形式**：创建接口返回的响应里 `created_at` 为 `null`，但**刷新页面重新拉列表时时间又正常了**（因为那是新 session 从数据库读的）。这种 bug 极隐蔽，容易被误判为时区或序列化问题。

### 2.2 `get_conversation` —— 查询会话

```python
conversation = await repo.get(conversation_id)
if conversation is None:
    raise NotFoundError("会话不存在")
return conversation
```

**考点是"异常契约"**：把"查不到"统一翻译成项目自定义的 `NotFoundError`，交给全局异常处理器转成 404。

**为什么不直接 `raise HTTPException(404)`？** 因为 **Service 层不该知道 HTTP 协议**。`NotFoundError` 继承自 `AppException`，自带 `code` / `message` / `http_status`，由路由层的处理器翻译。这样 Service 可以被 HTTP 接口、后台任务、单元测试共同复用，不被 HTTP 状态码绑住。

### 2.3 `list_messages` —— 拉取历史消息

```python
await self.get_conversation(conversation_id)   # 先校验会话存在
repo = ConversationRepository(self.session)
messages = await repo.list_messages(conversation_id)
return messages
```

**为什么先查一次会话？本章最值得品的设计。**

对比不校验的写法：

```python
messages = await repo.list_messages(conversation_id)
return messages          # 会话不存在时 → 200 []
```

调用方拿到 `[]` 时**无法区分**两种情况：

| 真实情况 | 不校验时 | 应该给 |
| --- | --- | --- |
| 会话根本不存在（ID 写错 / 已删除） | `200 []` | **404** |
| 会话存在，但还没聊过 | `200 []` | `200 []` |

**多一次轻量主键查询（走索引，微秒级），买来接口语义不含糊。** 前端才能做正确跳转——打开一个已删除的会话链接，应提示"会话不存在"，而不是给一个空白聊天界面。

---

## 三、本章核心架构点：双会话策略

文件注释（`chat_service.py` 类 docstring）中最重要的一段：

```
- 非流式接口（创建会话 / 历史）使用 FastAPI 注入的请求级 session
- 流式问答使用独立 session（与请求生命周期解耦），由 stream_answer 内部管理
```

**为什么两种接口要用两种 session？**

```
请求级 session 生命周期 = 一次 HTTP 请求时长
  → 普通 CRUD 几十毫秒结束，用完即还，没问题

SSE 流式问答生命周期 = 用户看完整段回答的时长（几十秒～几分钟）
  → 若复用请求级 session，这条数据库连接会被独占几十秒不释放
  → 并发几个用户问答，连接池即被占满，其他请求全部排队等连接
```

**具体感受**：若连接池 `pool_size=5`，流式复用请求级 session，则**只需 5 个用户同时提问，第 6 个用户的所有请求（包括刷新会话列表）就会全部排队**，直到前面某人的回答生成完。

所以流式问答必须**自己开独立 session，用完即关**。

> 当前实现是**非流式**部分，所以直接用 `self.session` 是正确的。
> 等实现 `stream_answer` 时这是必踩的分水岭——**不要复用 `self.session`**。

---

## 四、引用序号契约（`_serialize_citation`）

函数本身平平无奇，但 `ordinal` **为什么是显式入参**而非内部用下标现算，是本章最有含量的决策。

### 三方必须对齐同一个编号

```
prompt 侧   enumerate(chunks, start=1) 给片段编号
              → 模型看到「片段 1」「片段 2」，据此在回答里写 [1][2]
前端侧      按 ordinal 渲染 [N] 角标 → 用户点 [1] 跳转到来源
落库侧      AnswerCitation.ordinal → 历史回看时溯源
```

### 图省事的写法（现在能跑，以后会错）

```python
for i, chunk in enumerate(chunks):
    payload = _serialize_citation(chunk, ordinal=i + 1)
```

一旦引入 **rerank（重排序）** 或 **过滤低分片段**，`chunks` 的顺序/内容就变了，但模型回答里的 `[1][2]` 是**基于当时的顺序**写的：

```
检索     → chunks = [A, B, C] → prompt 给模型看 A=[1], B=[2], C=[3]
模型据此回答"……[1]"（指的是 A）
  ↓ 落库前做了 rerank
chunks   → [C, A, B]
下标现算 → C 的 ordinal 变成 1
落库记录 → ordinal=1 指向 C
用户点 [1] → 跳到 C  ❌ 实际应为 A
```

**显式传入 ordinal，等于把"模型当时看到的编号"这个信息固化下来，不被后续顺序变化污染。**

---

## 五、自测题与批改记录

### 题目与作答

| # | 题目 | 作答 | 判定 |
| --- | --- | --- | --- |
| 1 | `create_conversation` 里 `refresh` 能否省掉？省掉后接口响应有何具体表现？ | "回填数据库生成的时间戳，省掉之后数据库中的数据相当于没有刷新，下次会显示 None" | ⚠️ 结论对，事实说反 |
| 2 | 为什么 `commit()` 写在 Service 而不让 Repository 自己提交？说出一个具体场景 | "防止事务出现大的问题" | ⚠️ 方向对，太笼统 |
| 3 | `list_messages` 去掉开头的会话校验，会对前端造成什么困扰？ | "去掉会有两种语义的 200" | ✅ |
| 4 | 非流式用请求级 session、流式用独立 session，根本原因？ | "并发几个用户问答，连接池就被占满了，其他请求全部排队等连接" | ✅ |
| 5 | `ordinal` 为什么不让函数自己 `enumerate` 现算？举一个"现在能跑、以后会错"的场景 | "显式传入避免了以后的重排序导致序号乱引用的问题" | ✅ |

### 逐题批改

**第 1 题 —— 结论对，但因果说反了**

作答"省掉之后数据库中的数据相当于没有刷新"是错的：**数据库里的数据一直是对的，是 Python 内存对象没刷新。**

两者不一致，表现在响应上。且这个不一致会自然消失——下次重新查这条记录（新 session 从数据库读）字段就对了。所以症状是"**创建接口返回 created_at 为 null，但刷新页面重新拉列表又有时间**"。

→ 已写入 2.1 关键点 2。

**第 2 题 —— 方向对但必须补具体场景**

"防止事务出现大的问题"说不出场景等于没掌握。正确答法是举出**"消息落库了但引用没落库"**这类半成品状态：数据永久处于不一致，且无法自动修复（不知道哪些消息缺引用）。

→ 已写入 2.1 关键点 1。

**第 3 题 —— 正确**

"两种语义的 200"抓住了要点：`200 []` 本身表示"成功但无数据"，若同时能表示"会话不存在"，前端就无法做正确跳转。

**第 4 题 —— 正确**

抓住了连接池。补充了具体数字感受（`pool_size=5` 时第 6 个用户全排队）。

→ 已写入第三节。

**第 5 题 —— 正确**

抓住了"顺序被污染"。因果链已写得更精确：模型看到的编号 vs 落库时重新推导的编号，必须指向同一 chunk。

→ 已写入第四节。

---

## 六、本节两个必须掌握的概念

### 概念 1：Session 与实体的同步关系

实体是数据库行的**内存副本**。凡是数据库侧生成的值（`server_default` / 触发器 / `onupdate=func.now()`），Python 端都不知道，必须 `refresh`。

**排查这类 bug 的思路**：接口返回 `null` 但数据库有值 → 大概率是缺 `refresh`。

### 概念 2：事务边界的归属（Unit of Work）

```
Repository  只做 add / flush，永不 commit
Service     决定业务整体的原子边界，统一 commit / rollback
```

**判断标准**：仓储不知道"这次业务还要写什么"，所以它没资格决定何时提交。

---

## 七、下一部分伏笔（流式问答主链路）

`ChatService` 剩下的是 `answer` / `stream_answer`。写时有两个坑，本小节**故意未展开**：

1. **编排器必须先判 `state.get("refused")` 再决定是否调用 `stream_generate`。**
   否则 `retrieve` 写在 `state["answer"]` 里的拒答文案会被模型输出**直接覆盖**，熔断白做。
   （此为第 9 章"第 4 题"埋下的伏笔）

2. **流式方法里不能复用 `self.session`。**
   必须自己 `async with AsyncSessionLocal()` 开独立会话。
   （此为本章第 4 题的延伸）

---

## 八、一句话总结

> ChatService **不实现任何算法，只决定"什么时候调用谁"**；它掌握两件事——**事务边界**（Repository 只 flush，Service 统一 commit）与**会话生命周期**（非流式用请求级 session，流式用独立 session）；并把**引用序号作为显式入参**固化下来，使溯源不被后续顺序变化污染。

---

## 九、关联文件索引

| 文件 | 关键位置 | 说明 |
| --- | --- | --- |
| `app/services/chat_service.py` | L40 | `_serialize_citation` 引用序列化 |
| `app/services/chat_service.py` | L71 | `ChatService` 类定义（双会话策略 docstring） |
| `app/services/chat_service.py` | L82 | `__init__` 注入请求级 session |
| `app/services/chat_service.py` | L93 | `create_conversation`（commit + refresh） |
| `app/services/chat_service.py` | L110 | `get_conversation`（404 异常契约） |
| `app/services/chat_service.py` | L123 | `list_messages`（先校验会话再查消息） |
| `app/db/repositories/conversation_repo.py` | L85 | `recent_messages` 倒序截断 + 反转正序 |
| `app/db/repositories/citation_repo.py` | L36 | `bulk_add` 只 flush 不 commit |
| `app/core/exceptions.py` | L20 | `AppException` 体系（`NotFoundError` 为其子类） |

---

## 十、验证记录

代码写入后实测 6 项行为，全部通过：

```
1) create_conversation        -> id 正常, title='新对话', created_at/updated_at 均已回填 ✓
2) get_conversation           -> 命中 ✓
3) list_messages（空会话）     -> 返回 [] ✓
4) get_conversation（不存在）  -> 正确抛出 NotFoundError ✓
5) list_messages（不存在）     -> 正确抛出 NotFoundError ✓
6) _serialize_citation        -> 8 个字段齐备, score=0.7174(四舍五入), chunk_id 已转 str ✓
```
