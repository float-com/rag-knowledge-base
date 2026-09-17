# 02_API 路由与 SSE

> 章节：Day04 知识库问答 · 后端实现 · 第 12 章（后端实现最后一节）
> 对应文件：`backend/app/api/routes/chat.py`、`backend/app/main.py`
> 记录日期：2026.09.17

---

## 一、本章要解决的问题

前面 11 章把东西都造好了：契约模型有了（第 11 章）、编排服务有了（第 10 章）、检索与生成都有了。但它们**没有一个能被外部调用的入口**。

本章就是**把能力挂到 HTTP 上**，共三个端点：

```
POST /api/conversations                 创建会话
GET  /api/conversations/{id}            会话详情（含历史消息与引用）
POST /api/conversations/{id}/chat       SSE 流式问答   ← 重点
```

**路由层的职责边界**：只做四件事——**参数校验、依赖注入、调用服务、协议转换**。

> **判断标准**：如果路由函数里出现业务 `if` 判断，或出现 `db.execute(...)`，说明职责漏出去了，该下沉到 Service 层。

---

## 二、三个端点逐个看

### 2.1 `create_conversation` —— 最简单的一个

```python
@router.post("", response_model=ConversationRead, status_code=201, operation_id="createConversation")
async def create_conversation(payload: ConversationCreate, session: DbSession) -> ConversationRead:
    service = ChatService(session)
    conversation = await service.create_conversation(title=payload.title)
    return ConversationRead.model_validate(conversation)
```

**四个装饰器参数各有分工：**

```
response_model=ConversationRead   → 出参强制符合契约，多余字段自动剥离
status_code=201                   → 新建资源的语义（不是 200）
operation_id="createConversation" → 前端 SDK 生成的函数名
path=""                           → 空串配合 prefix="/conversations"
                                    最终路径是 /api/conversations（无尾斜杠）
```

**为什么不直接 `return conversation`？**

`response_model` 确实会过滤字段，但走 `model_validate` 是**显式转换**，明确表达"我在把 ORM 实体翻译成响应契约"，而不依赖框架的隐式行为。全项目统一此写法（`documents.py` 也是 `DocumentRead.model_validate(document)`），**一致性本身就是价值**。

更重要的收益：`documents.py` 的 DTO 会剥掉 `cos_object_key` / `cos_bucket` / `embedding` 等字段。**统一走 DTO 能防止将来加 ORM 字段时意外泄漏**——否则你不会注意到某个新字段悄悄出现在响应里。

### 2.2 `get_conversation` —— 组合响应

```python
service = ChatService(session)
conversation = await service.get_conversation(conversation_id)
repo = ConversationRepository(session)
messages = await repo.list_messages(conversation_id)
return ConversationDetail(
    conversation=ConversationRead.model_validate(conversation),
    messages=[MessageRead.from_orm(m) for m in messages],
)
```

`ConversationDetail` 需要 `conversation` 和 `messages` **两部分**，而 `ChatService.list_messages` 只返回消息列表（见第四节）。

**404 从哪来**：`service.get_conversation` 发现会话不存在会抛 `NotFoundError`，由全局异常处理器转成 404。**路由层一句 `if` 都没写**（见第三节 Q5）。

### 2.3 `stream_chat` —— SSE 流式问答（本章重点）

```python
@router.post("/{conversation_id}/chat", operation_id="streamChat", response_class=EventSourceResponse)
async def stream_chat(conversation_id: UUID, payload: ChatRequest, session: DbSession) -> AsyncIterable[ServerSentEvent]:
    service = ChatService(session)
    async for sse_event in service.stream_answer(conversation_id, payload.question):
        yield ServerSentEvent(event=sse_event["event"], data=sse_event["data"])
```

**有效代码只有三行。** 它做的事叫**协议适配**：

```
服务层产出:   {"event": "token", "data": {"delta": "这"}}      ← 业务中立的字典
路由层翻译:   ServerSentEvent(event="token", data={"delta": "这"})
框架编码为:   event: token\ndata: {"delta": "这"}\n\n           ← SSE 线格式
```

---

## 三、本章两个坑 + 一处契约修正

### 坑 1：`EventSourceResponse` 从哪导入（**版本差异**）

图里写的是：

```python
from fastapi.responses import EventSourceResponse    # ❌ 不存在
```

实测四个候选位置：

```
fastapi.responses.EventSourceResponse   -> 不存在
fastapi.EventSourceResponse             -> 不存在
starlette.responses.EventSourceResponse -> 不存在
fastapi.sse.EventSourceResponse         -> 存在  ✓
```

**原因是 FastAPI 版本代差：**

```
本项目: FastAPI 0.141.1 + Starlette 1.6.0
        → 已内置完整 SSE 支持（fastapi.sse 模块），无需第三方库

教程写作时（推测）:
        → 内置 SSE 之前，只能用第三方库
          pip install sse-starlette
          from sse_starlette.sse import EventSourceResponse
```

**注意**：图末尾那句注释"FastAPI 的 `ServerSentEvent` 内部序列化时已经设置了 `ensure_ascii=False`"——**说的就是内置的那个类**，说明教程作者清楚这是内置能力，只是 `EventSourceResponse` 那一行写错了模块。

**结论**：改用 `from fastapi.sse import EventSourceResponse, ServerSentEvent`，**零新增依赖**（`sse-starlette` 环境中未安装、`pyproject.toml` 也未声明）。

### 坑 2：`data` 千万不要预序列化（**双重编码**）

图里的写法：

```python
data=json.dumps(sse_event["data"])     # ❌
```

`ServerSentEvent` 的文档字符串原文：

> `data`: The event payload. Can be any JSON-serializable value. It is **always** serialized.
> All `data` values **including plain strings** are JSON-serialized.
> For example, `data="hello"` produces `data: "hello"` on the wire (with quotes).

**线上形态对比：**

```
❌ json.dumps 后传入 → 框架再编码一次
   data: "{\"delta\": \"这\"}"
   前端 JSON.parse → 得到字符串 '{"delta": "这"}'（string 类型）
   → data.delta → undefined

✓ 直接传 dict
   data: {"delta": "这"}
   前端 JSON.parse → {delta: "这"}
   → data.delta = "这"
```

**⚠️ 最关键的一点：这个 bug 不会在后端报错。** 接口返回 200、事件照发，只是**前端解析全失效**。前端 `chatStream.ts` 的 `onmessage` 里是 `JSON.parse(msg.data)` 后直接读 `data.delta`，所以表现为"**连接成功、事件收到、但一个字都不显示**"。

### 契约修正：`list_messages` 的返回类型

图里写的是：

```python
conversation, messages = await service.list_messages(conversation_id)   # ❌ 解包会报错
```

**为什么错**：第 10 章定义的 `ChatService.list_messages` 签名是 `-> list[Message]`，**只返回消息列表**。回顾其 docstring：

```
【为什么先校验会话再查消息】：
先执行一次会话存在性校验，是为了让「会话不存在（404）」与
「会话存在但还没有任何消息（200 + 空列表）」两种情况能在 API 层被明确区分。
```

它承诺"校验 + 返回消息"，**不承诺返回会话实体**。而 `ConversationDetail` 需要两部分，所以路由得自己再拿一次 conversation。

**修正方式**：`service.get_conversation` + `ConversationRepository.list_messages`。语义等价（同样先校验、同样 404），且复用了 `selectinload(Message.citations)` 的预加载保证。

---

## 四、自测题与批改记录

### 题目与作答

| # | 题目 | 作答 | 判定 |
| --- | --- | --- | --- |
| 1 | 为什么 `ChatService` 产出 `{event, data}` 字典而非直接产出 SSE 字符串？ | "可以更灵活" | ⚠️ 太笼统 |
| 2 | `data=json.dumps(...)` 会造成什么后果？线上 `data:` 行长什么样？ | "会造成序列化的严重问题" | ⚠️ 太笼统 |
| 3 | `response_class=EventSourceResponse` 帮我们做了哪些事？ | "不知" | ❌ |
| 4 | `create_conversation` 为什么不直接 `return conversation`？ | "服务层返回 ORM 实体，这里通过属性映射（from_attributes）转换为脱敏的响应模型，本质还是协议转换" | ✅ |
| 5 | 路由里没有 `if`，那"会话不存在"的 404 怎么产生的？ | "不知" | ❌ |

### 逐题批改

**第 1 题 —— "灵活"太笼统，真正的收益是协议中立**

两层分离：

```
ChatService 负责:  "发生了什么业务事件"     → {"event": "token", "data": {...}}
路由层负责:        "这件事怎么变成线格式"   → ServerSentEvent(event=..., data=...)
框架负责:          "怎么编码成字节"         → event: token\ndata: {...}\n\n
```

**若服务层直接产出 SSE 字符串的三个后果：**

```
① 服务层被 SSE 协议绑死   → 业务代码里散落 "event:" / "data:" 传输层细节
② 换传输方式要改服务层     → 想换 WebSocket 做双向通信？整个 stream_answer 重写
③ 无法独立测试业务逻辑     → 必须模拟一个 SSE 消费者才能测
```

**实证**：验证 `stream_answer` 时是**直接迭代它**的，完全没碰 HTTP：

```python
async for ev in svc.stream_answer(conv.id, '课程目标是什么'):
    print(ev['event'], type(ev['data']))
```

> 因为它是"协议中立的字典生成器"，所以能这样单独验证——**这就是分层的价值**。

**第 2 题 —— 必须能说出线上形态**

见第三节"坑 2"。关键是记住：**这个 bug 不报错**，只表现为"连接成功但无内容"。

**第 3 题 —— 三件事**

```
① 设置响应头
   Content-Type: text/event-stream; charset=utf-8
   （实测确认）

② 把每个 ServerSentEvent 编码成 SSE 线格式
   注意：我们的函数 yield 的是**对象**，不是 bytes！
   必须有人负责格式化——就是 EventSourceResponse

③ 心跳保活
   定期发送注释行（如 ": keepalive"）防止中间代理/负载均衡
   因长时间无数据而断开空闲连接——LLM 生成时可能有几百毫秒不出 token
```

> **关键理解**：`yield ServerSentEvent(...)` 出来的是对象，一个 yield 对象的异步生成器怎么变成"一行行 SSE 文本"？**中间那个转换器就是 `response_class` 指定的 `EventSourceResponse`。** 若不声明，FastAPI 按默认 JSON 序列化，整个流就废了。

**第 4 题 —— 正确**

作答抓住了"协议转换"这个本质。两点补充见 2.1 节（显式转换的一致性 + 防将来字段泄漏）。

**第 5 题 —— 完整产生路径**

```
① ChatService.get_conversation 发现查不到
   → raise NotFoundError("会话不存在")

② NotFoundError 继承自 AppException（core/exceptions.py）

③ main.py 注册的全局处理器捕获它
   app.add_exception_handler(AppException, _app_exception_handler)

④ 处理器用异常自带的 http_status 作为状态码
   JSONResponse(status_code=exc.http_status, content={"code":..., "message":...})
   NotFoundError.http_status == 404
```

**实测证据**：

```
GET /api/conversations/00000000-...  → 404
body: {'code': 'not_found', 'message': '会话不存在'}
```

**为什么路由层一句 `if` 都不用写**：这就是第 10 章"异常契约"的兑现——服务层用异常表达业务结果，全局处理器统一翻译成 HTTP，路由层完全不参与。

> **收益**：404 的映射规则**只存在一个地方**（异常类里的 `http_status`）。将来要把"会话不存在"改成 410 Gone，只改异常类一行，所有用到处同步生效。

---

## 五、本章两条规律

### 规律一：框架会替你做编码，所以别重复做

`ServerSentEvent.data` "**always serialized**"——**传对象，别传字符串**。

> **凡"框架负责 X"的地方，手动做 X 都会出问题**（双重编码、双重转义、双重转码都属此类）。

### 规律二：路由层应该"薄"到没有 if

```
参数校验     → Pydantic 模型
业务结果     → 异常（NotFoundError）
错误映射     → 全局处理器
```

路由函数只剩"调用服务 + 转换契约"。**如果在路由里写了业务判断，说明有东西该下沉到 Service 层。**

---

## 六、教程与实现的三处不一致（重点留档）

| # | 教程写法 | 实际问题 | 根因 | 修正 |
| --- | --- | --- | --- | --- |
| 1 | `from fastapi.responses import EventSourceResponse` | 模块不存在，直接 ImportError | **FastAPI 版本代差**（0.141.1 已内置 SSE） | 改为 `from fastapi.sse import ...` |
| 2 | `data=json.dumps(sse_event["data"])` | 双重编码，前端解析失效且不报错 | **SSE 协议 + 框架约定** | 直接传 dict |
| 3 | `conversation, messages = await service.list_messages(...)` | 解包报错 | **教程内部服务层契约不对齐** | `get_conversation` + 仓储取消息 |

**三处都不是本项目引入的问题**，而且都与上传链路/直传改造**无关**——它们的根因分别是依赖版本、协议约定、教程笔误。

> **可复用的教训**：照着任何教程写代码时，**必须先验证依赖版本与框架约定，而不是照抄**。第 1 处尤其典型——教程写作时的"标准做法"（第三方库）在新的框架版本里可能已被内置能力取代。

---

## 七、验证记录

### 7.1 路由注册与 OpenAPI

```
新增端点：
  POST /api/conversations                            operationId=createConversation
  GET  /api/conversations/{conversation_id}           operationId=getConversation
  POST /api/conversations/{conversation_id}/chat      operationId=streamChat
```

**第 11 章预告的现象在此兑现**：那六个契约模型此前因"无路由引用"而不出现在 OpenAPI，接入路由后全部自动出现：

```
ConversationCreate / ConversationRead / CitationRead
MessageRead / ConversationDetail / ChatRequest      → 全部 OK
```

### 7.2 端到端实测（真实 HTTP + 真实 SSE）

用 `httpx.ASGITransport` 在**同一事件循环**内跑（见第 8 节踩坑记录）：

```
1) POST /api/conversations                    -> 201 | title = __sse_test__

2) POST /api/conversations/{id}/chat  (SSE)
   status       = 200
   content-type = text/event-stream; charset=utf-8        ★ SSE 头正确
   message_start -> {'user_message_id': '73473078-…'}
   citations     -> 5 条, [1] score=0.6883
   message_end   -> {'message_id': 'b8402384-…', 'refused': False}
   事件序列: message_start -> citations -> token -> message_end
   token 次数: 42
   回答前 110 字: 这门课的课程目标有以下几个方面：**课程目标 1（专业知识面）**：…[1]

3) GET /api/conversations/{id}                -> 200
   conversation.title = __sse_test__
     role=user      len=  11 citations=0
     role=assistant len= 424 citations=5      ★ 历史回放带引用

4) GET 不存在的会话                            -> 404 | {'code':'not_found','message':'会话不存在'}
```

**关键验证**：每个 `data` 都成功 `json.loads` 成 **dict**（脚本内含 `isinstance(obj, dict)` 断言，双重编码会被打印出来）——**线格式与前端契约一致**。

---

## 八、一个测试踩坑记录（供后续参考）

首次用 **`TestClient`** 测试时报大量 asyncpg 异常：

```
AttributeError: 'NoneType' object has no attribute 'send'
RuntimeWarning: coroutine 'Connection._cancel' was never awaited
```

**原因**：`TestClient` 在独立线程里跑事件循环，与 `pool_pre_ping=True` 的连接池冲突——asyncpg 连接绑定在原事件循环上，跨循环复用时底层 transport 已失效。

**这不是代码问题**，而是测试方式问题。改用 `httpx.ASGITransport` 让应用与测试跑在**同一事件循环**内即恢复正常。

> 后续为异步应用写接口测试时，优先用 `httpx.AsyncClient(transport=ASGITransport(app=app))`。

---

## 九、一句话总结

> 路由层只做"参数校验 → 依赖注入 → 调用服务 → 协议转换"四件事；SSE 端点的三行代码体现了**协议中立**的分层价值（服务层产出业务事件，路由层翻译成传输格式，框架负责编码）；而本章三处"教程与实现不一致"分别源于**依赖版本代差、框架编码约定、教程内部契约不对齐**——都不是本项目引入的问题，但都提醒我们：**照抄教程前先验证版本与约定**。

---

## 十、关联文件索引

| 文件 | 关键位置 | 说明 |
| --- | --- | --- |
| `app/api/routes/chat.py` | L29 | `from fastapi.sse import EventSourceResponse, ServerSentEvent` |
| `app/api/routes/chat.py` | L45 | `router = APIRouter(prefix="/conversations", tags=["chat"])` |
| `app/api/routes/chat.py` | L55-75 | `create_conversation`（201 + response_model） |
| `app/api/routes/chat.py` | L82-118 | `get_conversation`（组合响应） |
| `app/api/routes/chat.py` | L109 / L112 | `get_conversation` + `repo.list_messages` 组合 |
| `app/api/routes/chat.py` | L126-162 | `stream_chat`（SSE） |
| `app/api/routes/chat.py` | L162 | `yield ServerSentEvent(event=..., data=...)` |
| `app/main.py` | L34 / L108 | 导入与挂载 `chat.router` |
| `app/api/error_handlers.py` | L34 | `status_code=exc.http_status`（404 的来源） |
| `app/api/error_handlers.py` | L64 | `add_exception_handler(AppException, ...)` |
| `app/core/exceptions.py` | — | `NotFoundError.http_status == 404` |
| `app/db/repositories/conversation_repo.py` | L81 | `selectinload(Message.citations)` 预加载 |
| `frontend/src/api/chatStream.ts` | L116-159 | 前端 SSE 事件分派（按 `msg.event`） |
| `frontend/src/api/chatStream.ts` | L118 | `JSON.parse(msg.data)`——双重编码会在此拿到字符串 |
