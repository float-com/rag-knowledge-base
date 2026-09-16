"""RAG 上下文装载图节点。

【模块职责说明】：
1. 节点责任单一化（LangGraph Node Pattern）：
   实现工作流首个执行节点 `load_context`，专门负责从数据库中提取当前会话的上下文记忆，
   避免多轮历史检索与下游的向量检索逻辑相互耦合。
2. 滑动窗口历史截断（Sliding Window Truncation）：
   根据全局配置中的 `chat_history_window` 参数，按“一轮问答 = 2 条消息（User + Assistant）”计算截断条数（`limit = window * 2`），
   防止长对话场景下历史消息无限制膨胀引发 Context Window 溢出或 Token 浪费。
3. 状态局部更新（Partial State Update）：
   遵循 LangGraph 的 `TypedDict(total=False)` 增量合并约定，函数仅返回包含 `{"chat_history": history}` 的局部字典，
   由图执行引擎自动增量合并到全局 `RAGState` 中。
"""

# 引入异步数据库会话类
from sqlalchemy.ext.asyncio import AsyncSession

# 引入全局系统配置单例
from app.core.config import settings
# 引入会话与消息数据仓储
from app.db.repositories.conversation_repo import ConversationRepository
# 引入 RAG 状态类型声明
from app.workflows.rag_state import RAGState


async def load_context(state: RAGState, session: AsyncSession) -> RAGState:
    """
    RAG 工作流节点：加载指定会话最近的历史消息上下文。

    :param state: 当前工作流全局状态（需包含 conversation_id）
    :param session: 外部注入的异步数据库会话（通常由图运行时配置或依赖注入提供）
    :return: 仅包含增量字段 `{"chat_history": history}` 的字典，用于合并回全局状态
    """
    # 1. 实例化会话仓储对象
    repo = ConversationRepository(session)

    # 2. 多轮滑动窗口按消息总条数截取：
    #    为什么乘 2？因为一轮完整问答 = 用户 1 条 + 助手 1 条 = 2 条记录；
    #    配置窗口大小为 3 轮，则 limit = 5 * 2 = 10 条
    history = await repo.recent_messages(
        state["conversation_id"],
        limit=settings.chat_history_window * 2,
    )

    # 3. 增量返回上下文结果，状态机会自动写入 state["chat_history"]
    return {"chat_history": history}