"""知识库问答 API 路由层（Transport / Controller Layer）。

【模块职责说明】：
1. 协议入口与能力暴露（RESTful / SSE Endpoint）：
   把问答模块的能力通过 FastAPI 暴露给前端，包含会话创建、会话详情查询
   以及流式问答三个端点。其中流式问答采用 EventSourceResponse 实现 SSE 推送。

2. 依赖注入与协议转换（Dependency Injection & Protocol Mapping）：
   通过 DbSession 注入请求级数据库会话，交给 ChatService 完成业务编排；
   路由层只负责参数校验、调用服务、把结果转换为响应模型，不承载业务规则。

3. 流式协议适配（SSE Adapter）：
   ChatService.stream_answer 产出统一的 {event, data} 事件字典，其中 data 是**未序列化的字典**；
   本层负责把它翻译成 SSE 协议的具名事件，由框架完成 JSON 编码，
   前端 @microsoft/fetch-event-source 据此按 msg.event 分派处理。

4. 会话生命周期约束（Session Scope）：
   三个端点都使用请求级会话。流式端点在服务层内部另开独立会话承载长连接，
   因此请求级会话只覆盖到业务开始之前，不会被 SSE 长时间占用。

【依赖说明】：
   本模块使用 FastAPI 内置的 SSE 能力（fastapi.sse），无需引入 sse-starlette 等第三方库。
"""

from collections.abc import AsyncIterable
from uuid import UUID

from fastapi import APIRouter
from fastapi.sse import EventSourceResponse, ServerSentEvent

from app.api.deps import DbSession
from app.api.schemas.chat import (
    ChatRequest,
    ConversationCreate,
    ConversationDetail,
    ConversationRead,
    MessageRead,
)
from app.db.repositories.conversation_repo import ConversationRepository
from app.services.chat_service import ChatService

# 语法（APIRouter 路由分组）：APIRouter(prefix="/conversations", tags=["chat"])
#   特性：统一声明问答模块的路由前缀与 OpenAPI 聚合标签
#   通俗来讲：给会话与问答相关接口挂上统一门牌号 `/conversations`，归类放置。
router = APIRouter(prefix="/conversations", tags=["chat"])


# =============================================================================
# 1. 创建会话
# =============================================================================
# 语法（路由装饰器配置）：
#   - response_model=ConversationRead: 强制出参符合会话详情契约
#   - status_code=201: 新建资源成功的标准状态码
#   - operation_id="createConversation": 供前端 SDK 生成唯一函数标识
@router.post(
    "",
    response_model=ConversationRead,
    status_code=201,
    operation_id="createConversation",
)
async def create_conversation(
    payload: ConversationCreate,
    session: DbSession,
) -> ConversationRead:
    """创建一个新会话。

    【调用时机】：
    用户点击「新建对话」时，前端先建会话拿到 conversation_id，
    后续该会话下的所有消息与流式问答都挂在这个 id 上。

    【协议转换】：
    服务层返回 ORM 实体，这里通过属性映射（from_attributes）转换为脱敏的响应模型。
    """
    service = ChatService(session)
    conversation = await service.create_conversation(title=payload.title)
    return ConversationRead.model_validate(conversation)


# =============================================================================
# 2. 查询会话详情（会话主体 + 全部历史消息）
# =============================================================================
@router.get(
    "/{conversation_id}",
    response_model=ConversationDetail,
    operation_id="getConversation",
)
async def get_conversation(
    conversation_id: UUID,
    session: DbSession,
) -> ConversationDetail:
    """返回会话主体与全部历史消息（含引用）。

    【为什么用 get_conversation + repo.list_messages 组合】：
    ChatService.list_messages 只返回消息列表，不含会话实体本身，无法直接组装
    ConversationDetail（需要 conversation + messages 两部分）。
    因此这里复用服务层的 get_conversation 做存在性校验与 404 转换，
    再通过仓储取回消息——仓储内部已用 selectinload 预加载 citations。

    【一次请求完成会话回放】：
    把会话元信息与消息列表打包成一个响应，避免前端为「打开历史会话」发两次请求，
    同时保证两者读到的是同一时点的数据。

    【引用读取的前置条件】：
    MessageRead.from_orm 会访问 message.citations，
    该关系由 ConversationRepository.list_messages 内部的 selectinload 预加载，
    否则异步环境下会触发懒加载并抛出 MissingGreenlet。
    """
    service = ChatService(session)
    conversation = await service.get_conversation(conversation_id)

    repo = ConversationRepository(session)
    messages = await repo.list_messages(conversation_id)

    return ConversationDetail(
        conversation=ConversationRead.model_validate(conversation),
        messages=[MessageRead.from_orm(m) for m in messages],
    )


# =============================================================================
# 3. 流式问答（SSE）
# =============================================================================
# 语法（流式响应声明）：response_class=EventSourceResponse
#   特性：声明该端点返回 text/event-stream 流，而非一次性 JSON 响应
#   通俗来讲：告诉 FastAPI 这个窗口是"边生成边推送"的水管，不是打好包再发的快递。
@router.post(
    "/{conversation_id}/chat",
    operation_id="streamChat",
    response_class=EventSourceResponse,
)
async def stream_chat(
    conversation_id: UUID,
    payload: ChatRequest,
    session: DbSession,
) -> AsyncIterable[ServerSentEvent]:
    """SSE 流式问答端点。

    【事件协议（与前端约定）】：
    正常顺序为 message_start -> citations -> token…（多次） -> message_end；
    任何阶段出错则下发 error 事件，前端 @microsoft/fetch-event-source 按 event 名分派。

    【为什么用 EventSourceResponse 而不手写 StreamingResponse】：
    前者内置 SSE 协议格式化与心跳保活，自动把每个事件写成
    `event: xxx\\ndata: {...}\\n\\n` 的线格式；后者需要自己实现这一层。

    【⚠️ data 不要预序列化】：
    ServerSentEvent.data 接收的是**任意可 JSON 序列化对象**，由框架负责编码。
    服务层产出的 data 本身就是字典，直接透传即可；
    若先 json.dumps 成字符串再传入，会被二次编码变成 `data: "{\\"...\\"}"`，
    前端 JSON.parse 后拿到的是字符串而不是对象，事件解析直接出错。

    【会话作用域说明】：
    这里注入的 session 是请求级会话，仅用于服务层开头的会话存在性校验。
    真正的流式主链路在 ChatService.stream_answer 内部另开独立会话承载，
    避免 SSE 长连接长期占用请求级连接（详见该方法的双会话策略说明）。
    """
    service = ChatService(session)
    async for sse_event in service.stream_answer(conversation_id, payload.question):
        # 服务层产出 {event, data} 字典：
        # - event: 事件名，前端按它分派（message_start / citations / token / message_end / error）
        # - data : 原始字典，交给框架统一 JSON 编码
        yield ServerSentEvent(event=sse_event["event"], data=sse_event["data"])
