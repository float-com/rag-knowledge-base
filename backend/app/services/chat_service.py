"""知识库问答编排服务（ChatService）。

【模块职责说明】：
1. 流水线总控与节点编排（Pipeline Orchestration）：
   作为 RAG 问答链路的业务总入口，把前序章节已经实现好的四类「零件」按顺序驱动起来：
   Repository（读写会话与消息）→ Workflow Nodes（装历史 / 改写查询 / 向量检索 / 流式生成）
   → Prompt（组装提示词）→ 引用落库（AnswerCitation）。
   本模块只负责「什么时候调用谁」，不实现检索算法、不拼提示词、不写 SQL。

2. 会话生命周期管理（Conversation Lifecycle）：
   提供非流式的会话与消息基础能力：新建会话、查询会话、拉取会话全部消息。
   这部分属于基础 CRUD，服务于前端会话侧边栏与历史回看场景。

3. 双会话策略（Dual Session Strategy）：
   - 非流式接口（创建会话 / 历史记录）使用 FastAPI 注入的**请求级 session**，
     生命周期与单次 HTTP 请求严格对齐；
   - 流式问答使用**独立 session**（与请求生命周期解耦），由 stream_answer 内部管理，
     因为 SSE 长连接期间不能长期占用请求级连接，避免撑爆连接池。
   本文件当前实现的是第一部分。

4. 引用序号契约（Citation Ordinal Contract）：
   引用编号必须与提示词中给大模型看到的「片段 N」编号严格一致（均从 1 开始），
   否则前端渲染的 [N] 角标会与真实来源错位，溯源功能失效。
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.db.models import Conversation, Message
from app.db.repositories.conversation_repo import ConversationRepository
from app.retrieval.vector_retriever import RetrievedChunk

# 初始化模块级业务日志记录器
logger = get_logger(__name__)


def _serialize_citation(chunk: RetrievedChunk, ordinal: int) -> dict:
    """把检索切片转换为 citations SSE 事件载荷（与前端约定一致）。

    【为什么 ordinal 必须显式传入，而不是用列表下标现算】：
    ordinal 必须与 prompt 中交给大模型看到的「片段 N」编号完全一致（均从 1 开始）。
    prompt 侧是 `enumerate(chunks, start=1)` 生成的编号，模型据此在回答里写 [1][2]；
    前端则按本函数返回的 ordinal 渲染 [N] 角标。
    若这里改用下标自增，一旦后续引入重排序、过滤等会改变顺序的环节，
    引用序号就会与模型回答中的编号错位。因此把编号作为入参显式传递，杜绝顺序丢失。

    :param chunk: 检索召回的不可变切片视图
    :param ordinal: 该切片在本次回答中的引用序号（从 1 开始，与 prompt 编号一致）
    :return: 可直接 JSON 序列化并下发给前端的引用字典
    """
    return {
        # 引用序号：与模型回答中的 [N] 严格对应
        "ordinal": ordinal,
        # 主键统一转字符串，避免 UUID 对象在 JSON 序列化时报错
        "chunk_id": str(chunk.chunk_id),
        "document_id": str(chunk.document_id),
        # 快照冗余：文档名 / 页码 / 章节路径随引用一起下发，前端无需二次请求
        "document_name": chunk.document_name,
        "page_no": chunk.page_no,
        "section_path": chunk.section_path,
        # 相似度分数保留 4 位小数，避免浮点尾数在前端表格里抖动
        "score": round(chunk.score, 4),
        # 引用原文：供前端展开查看「这句话出自哪一段」
        "quote": chunk.content,
    }


class ChatService:
    """知识库问答编排服务。

    【会话（session）使用约定】：
    - 非流式接口（创建会话 / 历史记录）使用 FastAPI 注入的**请求级 session**；
    - 流式问答使用**独立 session**（与请求生命周期解耦），由 stream_answer 内部管理。

    因此构造函数只接收一个请求级 session：非流式方法直接用 self.session；
    后续实现流式方法时，应在该方法内部自行创建独立 session，不要复用 self.session。
    """

    def __init__(self, session: AsyncSession) -> None:
        """注入请求级异步数据库会话。

        :param session: 由 FastAPI 依赖注入提供的请求生命周期 AsyncSession
        """
        self.session = session

    # =========================================================================
    # 非流式接口：会话基础 CRUD
    # =========================================================================

    async def create_conversation(self, title: str = "新对话") -> Conversation:
        """创建一个新会话。

        【事务边界】：
        仓储层只负责 add + flush，提交与刷新由本层控制。
        必须 refresh 一次，才能把数据库生成的 created_at / updated_at
        回填到实体上（这两个字段是 server_default，Python 侧实例化时并不存在），
        否则序列化响应时这两个字段为 None。
        """
        repo = ConversationRepository(self.session)
        conversation = await repo.create(title)
        # 提交事务：仓储层不主动 commit，由服务层统一控制事务边界
        await self.session.commit()
        # 刷新实体：回填数据库生成的时间戳等字段，避免响应里出现 None
        await self.session.refresh(conversation)
        return conversation

    async def get_conversation(self, conversation_id: UUID) -> Conversation:
        """按主键查询会话，不存在时抛出 404 业务异常。

        【异常契约】：
        统一把「查不到」翻译成 NotFoundError，由全局异常处理器转换为标准 404 响应，
        避免各调用方各自写 if None 判断与状态码映射。
        """
        repo = ConversationRepository(self.session)
        conversation = await repo.get(conversation_id)
        if conversation is None:
            raise NotFoundError("会话不存在")
        return conversation

    async def list_messages(self, conversation_id: UUID) -> list[Message]:
        """拉取指定会话的全部历史消息（按时间正序）。

        【为什么先校验会话再查消息】：
        先执行一次会话存在性校验，是为了让「会话不存在（404）」与
        「会话存在但还没有任何消息（200 + 空列表）」两种情况能在 API 层被明确区分。
        若直接查消息表，两种情况的返回都是空列表，调用方无法判断到底是哪一种。
        """
        # 1. 会话存在性校验：不存在会在此处直接抛 404，不会继续向下查消息
        await self.get_conversation(conversation_id)

        # 2. 复用 self.session 再实例化一次仓储（仓储是无状态的，仅持有会话引用）
        repo = ConversationRepository(self.session)

        # 3. 仓储内部按 created_at + id 稳定排序，保证同一毫秒写入的消息顺序不抖动
        messages = await repo.list_messages(conversation_id)

        return messages
