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

3. 流式问答主链路（Streaming Answer Pipeline）：
   以 stream_answer 为入口，按「校验会话 → 开独立 session → 装历史 → 落库用户提问
   → 检索 → 流式生成 → 落库助手回复与引用」的顺序驱动全流程，
   并在每个阶段对应位置向外 yield 标准 SSE 事件，供路由层直接转发给前端。

4. 双会话策略（Dual Session Strategy）：
   - 非流式接口（创建会话 / 历史记录）使用 FastAPI 注入的**请求级 session**，
     生命周期与单次 HTTP 请求严格对齐；
   - 流式问答使用**独立 session**（与请求生命周期解耦），由 stream_answer 内部管理，
     因为 SSE 长连接期间不能长期占用请求级连接，避免撑爆连接池。

5. 引用序号契约（Citation Ordinal Contract）：
   引用编号必须与提示词中给大模型看到的「片段 N」编号严格一致（均从 1 开始），
   否则前端渲染的 [N] 角标会与真实来源错位，溯源功能失效。

6. 异常边界（Error Boundary）：
   流式响应一旦开始发送，HTTP 状态码就已经确定为 200，后续任何异常都无法再改用 4xx/5xx 表达。
   因此流式接口内部必须显式捕获异常并转为 error 事件下发，不能把异常直接抛给上层中间件。
"""

from collections.abc import AsyncIterator
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.db.models import AnswerCitation, Conversation, Message
from app.db.repositories.citation_repo import AnswerCitationRepository
from app.db.repositories.conversation_repo import ConversationRepository
from app.db.session import AsyncSessionLocal
from app.retrieval.vector_retriever import RetrievedChunk
from app.workflows.nodes import (
    load_context,
    normalize_query,
    retrieve,
    stream_generate,
)
from app.workflows.rag_state import RAGState

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

    # =========================================================================
    # 流式问答主链路：按「校验 → 装历史 → 落库提问 → 检索 → 生成 → 落库回复」顺序驱动
    # =========================================================================

    async def stream_answer(
        self, conversation_id: UUID, question: str
    ) -> AsyncIterator[dict]:
        """**这是本模块最核心的一段**：流式问答主链路。

        【执行的六个阶段，每一步对应下方相应位置的 SSE 事件】：
        1. 校验会话存在性（复用非流式的 self.session，本阶段仍是短请求）
        2. 开启独立数据库会话（与请求生命周期解耦，长连接专用）
        3. 装载上下文 → 标准化查询（改写后的检索词）
        4. 落库并提交用户提问（必须在 load_context 之后，避免本轮提问混进历史）
        5. 向量检索 → 回填引用 → 把拒答判定前置到生成之前
        6. 流式生成（或走拒答分支）→ 落库助手回复与引用 → 收尾

        【事件协议（与前端约定）】：
        正常顺序为 message_start → citations → token… → message_end；
        其中 token 事件在拒答分支只有一次，且直接下发预置的拒答文案。

        【异常处理】：
        流式响应一旦开始返回，HTTP 状态码已固定为 200，后续异常无法再用 4xx/5xx 表达，
        因此必须在函数内部兜住异常并转成 error 事件下发，而不是向上抛出。

        :param conversation_id: 目标会话 ID（必须已存在）
        :param question: 用户本轮提问原文
        :return: 逐条 SSE 事件字典（含 event 与 data 两个键）的异步生成器
        """
        # 1. 会话存在性校验：复用请求级 session。
        #    此步骤在流式响应开始之前完成，若会话不存在会直接抛 404，由路由层正常返回 HTTP 错误。
        await self.get_conversation(conversation_id)

        # 2. 开启独立 session：SSE 长连接期间不能占用请求级连接，否则并发时极易耗尽连接池。
        #    async with 保证无论是正常结束还是异常退出，连接都会被归还。
        async with AsyncSessionLocal() as session:
            try:
                state: RAGState = {
                    "conversation_id": conversation_id,
                    "question": question,
                }

                # 3. 装载上下文（取历史消息）与查询标准化。
                #    注意：这一步必须早于用户提问落库，否则 load_context 会把本轮提问也当成历史读回来。
                state.update(await load_context(state, session))
                state.update(await normalize_query(state))

                # 4. 落库用户提问并 commit。
                #    此处单独提交一次，是为了让「用户提问」这条记录先于大模型调用持久化：
                #    即使后续 LLM 调用失败，用户问题也不会丢，便于前端重试与问题排查。
                await self._persist_user_message(state, session)

                # 5. 先把 message_start 发给前端，再进入 RAG 主链路。
                #    这样做让前端尽早拿到 user_message_id 用于消息占位，参考资料的耗时不会阻塞首屏。
                yield {
                    "event": "message_start",
                    "data": {"user_message_id": str(state["user_message_id"])},
                }

                # 6. 向量检索 + 拒答熔断（判定结果由 retrieve 节点写入 state["refused"]）
                state.update(await retrieve(state, session))

                # 7. 引用回填：ordinal 用 enumerate(start=1) 生成，与 prompt 中给模型的「片段 N」编号一致
                citations_payload = [
                    _serialize_citation(chunk, ordinal=i)
                    for i, chunk in enumerate(state.get("retrieved_chunks", []), start=1)
                ]
                yield {
                    "event": "citations",
                    "data": {"citations": citations_payload},
                }

                # 8. 生成分支：拒答时直接把预置文案作为唯一 token 下发，完全不调用大模型
                if state.get("refused"):
                    yield {
                        "event": "token",
                        "data": {"delta": state["answer"]},
                    }
                else:
                    # ★ 流式节点只负责逐块产出，不写 state；答案的累积与回写由本编排层负责
                    answer_parts: list[str] = []
                    async for delta in stream_generate(state):
                        answer_parts.append(delta)
                        yield {"event": "token", "data": {"delta": delta}}
                    state["answer"] = "".join(answer_parts)

                # 9. 落库助手回复 + 引用记录（两者在同一事务内提交，保证原子性）
                await self._persist_assistant_message(state, session)

                # 10. 收尾事件：下发 assistant_message_id 与拒答标志，前端据此结束流式状态并回填正式消息
                yield {
                    "event": "message_end",
                    "data": {
                        "message_id": str(state["assistant_message_id"]),
                        "refused": bool(state.get("refused")),
                    },
                }

            except Exception as exc:
                # 记录完整堆栈供服务端排查（含 conversation_id 便于定位现场）
                logger.exception(
                    "chat stream failed: conversation_id=%s", conversation_id
                )

                # 说明：用户提问已在第 4 步单独提交，回滚不会把它删掉。
                # 这里回滚的是「助手回复 + 引用」这一批尚未提交的写入，
                # 保留用户消息便于前端重试与问题复现（前端可用它做"重发"）。
                await session.rollback()

                # 流式已开始，无法再改用 HTTP 错误码，只能以 error 事件下发
                yield {
                    "event": "error",
                    "data": {
                        "code": "chat_stream_failed",
                        "message": str(exc) or "问答处理失败",
                    },
                }

    # =========================================================================
    # 内部方法：流式链路的两段落库逻辑
    # =========================================================================

    async def _persist_user_message(
        self,
        state: RAGState,
        session: AsyncSession,
    ) -> None:
        """流式开始前先把用户提问落库并提交。

        【为什么必须单独 commit】：
        让用户提问先于大模型调用持久化。若后续检索或 LLM 调用失败，
        用户问题依然留在数据库里，前端可据此做"重发"，也便于排查问题现场。

        【调用时机约束】：
        必须在 load_context 之后 调用——load_context 读到的「历史消息」不应包含本轮提问。
        """
        repo = ConversationRepository(session)
        user_msg = ConversationRepository.make_user_message(
            state["conversation_id"], content=state["question"]
        )
        await repo.add_messages([user_msg])
        await session.commit()
        # 回写主键：供上层组装 message_start 事件下发前端
        state["user_message_id"] = user_msg.id

    async def _persist_assistant_message(
        self,
        state: RAGState,
        session: AsyncSession,
    ) -> None:
        """流式生成结束后落库助手回复消息及其引用，用单事务保证两者原子。

        【为什么两者必须同一事务】：
        助手消息与引用记录是"一次回答"的两个组成部分。
        若分开提交，一旦引用写入失败，就会出现"有回答但点不开引用"的半成品数据，
        且无法自动修复（无法判断哪些回答缺引用）。

        【拒答时不写引用】：
        拒答路径没有可溯源的原文依据，因此跳过 AnswerCitation 写入，
        只落一条带 refused 标记的助手消息。
        """
        conv_repo = ConversationRepository(session)
        citation_repo = AnswerCitationRepository(session)

        assistant_msg = ConversationRepository.make_assistant_message(
            state["conversation_id"],
            content=state["answer"],
            # 把拒答标志写入消息元数据，历史回看时无需再推断
            extra_metadata={"refused": bool(state.get("refused"))},
        )
        await conv_repo.add_messages([assistant_msg])

        if not state.get("refused"):
            # ordinal 同样从 1 开始，与 prompt 编号、citations 事件载荷三者严格对齐；
            # 快照冗余 document_name / page_no / quote，使原文被删除后引用仍可展示。
            citations = [
                AnswerCitation(
                    message_id=assistant_msg.id,
                    ordinal=ordinal,
                    document_id=chunk.document_id,
                    chunk_id=chunk.chunk_id,
                    document_name=chunk.document_name,
                    page_no=chunk.page_no,
                    quote=chunk.content,
                )
                for ordinal, chunk in enumerate(
                    state.get("retrieved_chunks", []), start=1
                )
            ]
            await citation_repo.bulk_add(citations)

        # 单次提交：助手消息 + 引用一起落盘，或一起回滚
        await session.commit()
        state["assistant_message_id"] = assistant_msg.id
