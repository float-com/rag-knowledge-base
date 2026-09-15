"""RAG 提示词模板与上下文消息装配层。

【模块职责说明】：
1. 提示词工程与引用规则收敛（Prompt Engineering & Citation Discipline）：
   统一管控面向大模型的系统规则设定（System Prompt），通过严苛的“片段级负样本与格式规约”约束（如严格对齐 [N] 编号、禁止同文档混淆、标点排他性），
   从源头抑制大模型幻觉，保障知识库回答的精准溯源与确定性输出。
2. 领域模型与框架协议适配（Entity-to-Message Adapter Pattern）：
   充当业务领域/持久层与底层框架之间的适配器，负责将 SQLAlchemy ORM 实体（Message）转换对齐为 LangChain 标准消息协议（BaseMessage 系列），
   隔离底层存储演进对大模型编排层带来的代码侵入。
3. 知识切片结构化序列化（Context Markdown Formatting）：
   封装不可变切片视图（RetrievedChunk）的文本重组逻辑，注入文档名称、页码定位与章节层级等多维元数据，
   以显式 Markdown 格式为 LLM 提供清晰的上下文隔离边界与溯源线索。
4. 全链路多轮消息装配（Dynamic Multi-Turn Assembly）：
   基于 LangChain 的 `ChatPromptTemplate` 与 `MessagesPlaceholder` 提供一站式变量动态插值与消息管道构建，
   平滑兼容“首轮免历史冷启动”与“多轮有状态问答”调用，对外交付即用型的 `list[BaseMessage]` 领域对象。
5. 确定性拒答标准统筹（Deterministic Fallback Authority）：
   收敛知识库未命中或检索失效时的兜底应答常量（REFUSAL_ANSWER），保障全系统在极端与缺省场景下的交互一致性。
"""
# 从 LangChain 核心提示词模块引入多消息模板和消息占位符
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

# 引入 LangChain 核心消息抽象体系
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

# 引入数据库持久层的消息模型与角色枚举
from app.db.models import Message, MessageRole
# 引入检索层返回的不可变切片数据契约
from app.retrieval.vector_retriever import RetrievedChunk

_SYSTEM_PROMPT = """你是企业知识库助手，必须严格遵守以下规则：

1. 只基于下面【参考资料】中提供的【片段】作答，禁止使用片段之外的常识或主观推断。
2. 如果所有片段都无法回答用户问题，直接回复："抱歉，知识库中没有找到相关信息。" 不要编造。
3. 回答使用简体中文，使用 Markdown 排版（必要时使用列表、加粗等结构）。
4. 引用规则（**最重要**，违反任何一条都视为错误）：
   - 在每个结论后用方括号标注片段编号，例如 [1] 或 [2][3]。
   - 编号 N 必须**精确指向"下方编号为 N 的那个片段"**，并且该结论的内容能在 N 号片段的原文中**直接找到对应文字**。
   - **禁止**因为某个片段与结论"同属一份文档"就标该片段编号；同一份文档的不同片段算不同片段。
   - **禁止**把多个编号合写成 [1, 2] 或 [1-3]，多个并列写成 [1][2]。
   - **禁止**在编号外加反引号或尖括号，如 `[1]`、<1>。
   - 找不到能直接支撑该结论的片段，就**不要给那句话加引用**，宁缺毋滥。
5. 不要重复粘贴参考资料原文，只引用其中关键信息。

【正确示例】
片段 1：差旅住宿标准为一线城市每晚不超过 600 元。
片段 2：差旅日均餐补为 100 元。
回答："住宿标准为一线城市每晚不超过 600 元 [1]，餐补每日 100 元 [2]。"

【错误示例】（同一份文档不同片段，不可串用）
片段 1：差旅住宿标准为一线城市每晚不超过 600 元。
片段 2：差旅日均餐补为 100 元。
回答："餐补每日 100 元 [1] "  ← 错：餐补信息出自片段 2，不是片段 1。

【参考资料】
{context}
"""

# 组合三段式提示词结构，生成最终用于指导模型生成的 Prompt 模板单例
RAG_ANSWER_PROMPT = ChatPromptTemplate.from_messages(
    [
        # 1. System 消息：注入系统全局规则、严格的引用约束（[N]编号规范）、正反例以及待填充的参考资料 {context}
        ("system", _SYSTEM_PROMPT),

        # 2. 动态历史消息占位符：
        #    - "chat_history": 映射传入的历史对话消息列表（list[BaseMessage]）
        #    - optional=True: 关键容错参数，当外部未传入历史记录（如单轮对话或首轮问答）时，自动忽略占位符而不报错
        MessagesPlaceholder("chat_history", optional=True),

        # 3. Human 消息：承接用户当前最新提出的问题变量 {question}
        ("human", "{question}"),
    ]
)


def format_context(chunks: list[RetrievedChunk]) -> str:
    """
    把检索结果拼接成给 LLM 的【参考资料】文本。

    用「片段 N」而非「来源: xxx」做强标记，避免 LLM 把 [N] 误解为
    "第 N 份文档"——同一文档命中多 chunk 时这种误解会导致引用张冠李戴。

    :param chunks: 检索召回的 RetrievedChunk 列表
    :return: 格式化后的参考资料纯文本
    """
    # 边界防御：如果未召回到任何分块，返回占位文本，避免传空导致模型幻觉
    if not chunks:
        return "(无)"

    parts: list[str] = []
    # 从 1 开始按顺序为每一个 chunk 赋予唯一的片段序号
    for index, chunk in enumerate(chunks, start=1):
        # 组装文档溯源元数据：首要展示所属文档名
        meta = f"来自《{chunk.document_name}》"
        # 若包含页码信息，则追加页码标记
        if chunk.page_no is not None:
            meta += f"，第 {chunk.page_no} 页"
        # 若包含章节路径信息（如：1.2节 / 架构设计），则追加章节路径
        if chunk.section_path:
            meta += f"，章节: {chunk.section_path}"

        # 拼接单个片段格式：【片段 N】 (元数据)\n内容
        parts.append(f"【片段 {index}】 ({meta})\n{chunk.content}")

    # 各片段之间通过空行与分割线隔开，形成清晰的结构边界
    return "\n\n---\n\n".join(parts)


def history_to_messages(history: list[Message]) -> list[BaseMessage]:
    """
    把数据库持久层的 Message 实体列表转成 LangChain 的 BaseMessage 列表，用于塞进 prompt。

    :param history: 数据库中读取的历史消息列表
    :return: 转换后的 LangChain 消息抽象列表
    """
    messages: list[BaseMessage] = []
    for msg in history:
        # 将持久层 USER 角色映射为 LangChain 的 HumanMessage
        if msg.role == MessageRole.USER:
            messages.append(HumanMessage(content=msg.content))
        # 将持久层 ASSISTANT 角色映射为 LangChain 的 AIMessage
        elif msg.role == MessageRole.ASSISTANT:
            messages.append(AIMessage(content=msg.content))
        # 将持久层 SYSTEM 角色映射为 LangChain 的 SystemMessage
        elif msg.role == MessageRole.SYSTEM:
            messages.append(SystemMessage(content=msg.content))

    return messages


def build_answer_messages(
        question: str,
        chunks: list[RetrievedChunk],
        history: list[Message],
) -> list[BaseMessage]:
    """
    组装最终送给 LLM 的 messages 列表。

    :param question: 当前用户提问
    :param chunks: 当前召回的参考资料分块
    :param history: 当前会话的历史消息记录
    :return: 填入变量并解析完成后的最终 messages 列表
    """
    # 动态执行 Prompt 模板插值（invoke）：
    # 1. format_context(chunks) 生成 {context}
    # 2. question 传入 {question}
    # 3. history_to_messages(history) 填充到 MessagesPlaceholder("chat_history")
    prompt_value = RAG_ANSWER_PROMPT.invoke(
        {
            "context": format_context(chunks),
            "question": question,
            "chat_history": history_to_messages(history),
        }
    )
    # 将 LangChain 的 PromptValue 结构转化为纯 list[BaseMessage]
    return list(prompt_value.to_messages())


# 检索失败时的固定拒答文案，集中管理便于后续章节统一调整
REFUSAL_ANSWER = "抱歉，知识库中没有找到与该问题相关的可靠依据。"