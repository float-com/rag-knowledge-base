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


# =============================================================================
# Query 优化提示词（第 5 期）
# =============================================================================
# 这一组提示词分别驱动四种策略：
#   ROUTE_QUERY_PROMPT         判断该走哪条路（输出英文小写策略名）
#   REWRITE_QUERY_PROMPT       把残缺提问改写成独立完整的检索问句
#   HYDE_PROMPT                生成假设答案（用陈述句形式去匹配文档）
#   MULTI_QUERY_PROMPT         把提问扩展成多个不同角度的子查询
#
# 四个 prompt 都是「system + human」两段式结构，与 RAG_ANSWER_PROMPT 一致。
# 输出约束必须写在 prompt 里（如"只输出策略名"），因为下游要做严格的格式解析与降级判断。

# -----------------------------------------------------------------------------
# 1. 策略路由
# -----------------------------------------------------------------------------
# 核心约束：只输出一个英文小写的策略名，不能有任何解释、引号或标点。
# 原因：下游按字面量精确匹配四种策略，多一个字符都会导致匹配失败并降级为 original。
_ROUTE_SYSTEM = """你是 RAG 系统的查询路由器，要把用户问题归到下列 4 种策略之一：

1. original —— 问题清晰、表达完整、用词具体（含专有名词 / 编号 / 实体），直接检索即可。
2. rewrite —— 问题存在指代（"它"、"这个"、"那"）、省略、口语化或表达不完整，需要改写成独立完整的问题。
3. hyde —— 抽象 / 开放式（"什么是..."、"为什么..."、"如何理解..."），关键词稀疏，直接检索容易召回不到。
4. multi_query —— 问题包含多个角度、多个并列子问题，或者一个角度难以一次召回全（如"对比 A 和 B"、"X 的优缺点"）。

只输出上述 4 个策略名之一（original / rewrite / hyde / multi_query），小写，不要加任何解释、引号或标点。"""

_ROUTE_HUMAN = "{question}"

ROUTE_QUERY_PROMPT = ChatPromptTemplate.from_messages(
    [("system", _ROUTE_SYSTEM), ("human", _ROUTE_HUMAN)]
)


# -----------------------------------------------------------------------------
# 2. 改写（指代消解 / 补全省略）
# -----------------------------------------------------------------------------
# 重点在"改写成独立完整的问题"，并明确禁止扩写、解释与回答：
# 一旦模型顺手把答案也写出来，改写的问句就会带上幻觉内容，反而把检索带偏。
_REWRITE_SYSTEM = """你是一个查询改写助手，把用户问题改写成一个独立完整的检索查询：

1. 消解指代（"它"、"这个"、"那"）和省略，补全缺失主语 / 宾语。
2. 保留原问题的全部语义，不增删信息，不改变意图。
3. 不要扩写、不要解释、不要回答问题。
4. 输出**单行**改写后的问题，不要加引号或序号。"""

REWRITE_QUERY_PROMPT = ChatPromptTemplate.from_messages(
    [("system", _REWRITE_SYSTEM), ("human", "{question}")]
)


# -----------------------------------------------------------------------------
# 3. HyDE（生成假设答案）
# -----------------------------------------------------------------------------
# 要求生成 80~200 字的陈述式假设答案：
#   - 用陈述句而非问句，是因为检索目标是"文档片段"（陈述形式），形式一致才能拉近向量距离
#   - 要求包含相关关键词、术语和概念，让生成的文本在语义空间里更靠近真实文档
#   - 明确禁止"我认为""可能"这类不确定措辞——假设答案的语气越笃定，向量越"像文档"
_HYDE_SYSTEM = """你是一个 HyDE（Hypothetical Document Embeddings）助手，请基于一般领域知识写一段"假设性的回答"用于向向量库召回，不需要真实。
要包含问题相关的关键词、术语和概念。

要求：
1. 长度 80-200 字之间。
2. 用陈述句，陈述具体且明确，不要使用"我认为""可能"这类的词。
3. 不要表达"无法回答"。
4. 输出的内容是用于检索的假设文本，不是真正的回答。"""

HYDE_PROMPT = ChatPromptTemplate.from_messages(
    [("system", _HYDE_SYSTEM), ("human", "{question}")]
)


# -----------------------------------------------------------------------------
# 4. 多查询扩展
# -----------------------------------------------------------------------------
# 要求 n 个"不同角度"的子查询，并特别强调角度要真正不同：
# 措辞不同但视角相同的子查询只会召回同一批片段，白白多付 n 次向量化成本。
_MULTI_QUERY_SYSTEM = """你是一个查询扩展助手，请把用户问题改写成 {n} 个**不同角度**的子查询，用于多路检索以提升召回覆盖率。

要求：
1. 每个子查询需要完整、可独立检索。
2. 各子查询之间角度不同，用词和视角要相互错开，不要只是同义词替换。
3. 每个子查询一行，**不要编号**，不要加解释或标点。
4. 输出 {n} 行，不多不少。"""

MULTI_QUERY_PROMPT = ChatPromptTemplate.from_messages(
    [("system", _MULTI_QUERY_SYSTEM), ("human", "{question}")]
)


# -----------------------------------------------------------------------------
# 提示词装配辅助函数
# -----------------------------------------------------------------------------
# 四个 build_* 函数让节点不必直接与 ChatPromptTemplate 打交道：
# 节点只关心"给我某个策略对应的 messages"，不关心模板在哪、怎么插值。
def build_route_messages(question: str) -> list[BaseMessage]:
    """组装策略路由 messages，期望模型只回一个策略名。

    :param question: 用户原始提问
    :return: 可直接送入 LLM 的消息列表
    """
    return list(ROUTE_QUERY_PROMPT.invoke({"question": question}).to_messages())


def build_rewrite_messages(question: str) -> list[BaseMessage]:
    """组装查询改写 messages，产出独立完整的检索问句。

    :param question: 用户原始提问（可能含指代或省略）
    :return: 可直接送入 LLM 的消息列表
    """
    return list(REWRITE_QUERY_PROMPT.invoke({"question": question}).to_messages())


def build_hyde_messages(question: str) -> list[BaseMessage]:
    """组装 HyDE messages，产出一段用于召回的假设答案。

    :param question: 用户原始提问
    :return: 可直接送入 LLM 的消息列表
    """
    return list(HYDE_PROMPT.invoke({"question": question}).to_messages())


def build_multi_query_messages(question: str, n: int) -> list[BaseMessage]:
    """组装多查询扩展 messages，产出 n 条不同角度的子查询。

    :param question: 用户原始提问
    :param n: 期望生成的子查询条数（对应 settings.multi_query_count）
    :return: 可直接送入 LLM 的消息列表
    """
    return list(MULTI_QUERY_PROMPT.invoke({"question": question, "n": n}).to_messages())


# =============================================================================
# 第 7 期：Agentic RAG 决策器 prompt
# =============================================================================
# 决策器的职责：让模型看「原始问题 + 当前 query/route + 前几轮的检索观察」，
# 输出一个 JSON 决策，告诉节点下一步该做什么。
#
# ⚠️ 下面两处 JSON 示例里的花括号必须写成【双花括号】{{ }}。
#    ChatPromptTemplate 会把单个 {xxx} 当成变量占位符：写成单括号时
#    from_messages() 能正常编译，但每次 invoke() 都会抛
#    KeyError: 'Input to ChatPromptTemplate is missing variables {"action"}'
#    —— 延迟到运行时才炸，是最难查的一类错误。占位符（如 {question}）用单括号。
_AGENT_PLAN_SYSTEM = """你是 RAG 系统的检索决策器。系统会基于检索到的片段回答用户问题，
但上一轮检索的结果不够好（Top1 语义相似度过低或没有命中）。请基于"前几轮的检索观察"，决定下一步：

可选 action:
- proceed: 当前候选已经足够回答问题，直接进入答案生成。
- rewrite_query: 当前 query 不够清晰 / 过于口语化 / 含指代，需要换一个表达再检索；必须给出 new_query。
- switch_route: 换一种检索策略。可选 new_route: original / rewrite / hyde / multi_query。
- refuse: 多轮都召回不到相关内容，知识库可能不覆盖，提前拒答。

策略选择建议:
- 已经尝试过 rewrite 仍未命中 -> 试 hyde（抽象问题）或 multi_query（多角度）。
- 已经尝试过 multi_query 仍未命中 -> 试 refuse。
- 问题里包含明确实体 / 编号但都没检索到 -> 优先 refuse，避免无意义改写。

只输出**单行 JSON**，键固定为 action / reason / new_query / new_route，缺失字段填 null。
示例: {{"action": "rewrite_query", "reason": "原 query 含指代", "new_query": "差旅住宿标准", "new_route": null}}"""


# 供 LangChain / LLM 使用的对话模板：
# 四个占位符全部是单花括号，与 build_agent_plan_messages 传入的键一一对应
_AGENT_PLAN_HUMAN = """用户原始问题: {question}

当前 query: {current_query}
当前 route: {current_route}

历史轮次观察:
{history}

请输入下一步决策的 JSON。"""

AGENT_PLAN_PROMPT = ChatPromptTemplate.from_messages(
    [("system", _AGENT_PLAN_SYSTEM), ("human", _AGENT_PLAN_HUMAN)]
)


def build_agent_plan_messages(
    question: str,
    current_query: str,
    current_route: str,
    history: str,
) -> list[BaseMessage]:
    """组装 Agentic RAG 决策器 messages，期望模型返回单行决策 JSON。

    :param question: 用户原始提问（始终以它为准，避免被改写后的 query 带偏）
    :param current_query: 当前这一轮实际用于检索的查询词
    :param current_route: 当前生效的检索策略（original / rewrite / hyde / multi_query）
    :param history: 前几轮检索观察的文本摘要（由节点从 agent_steps 拼出）
    :return: 可直接送入 LLM 的消息列表
    """
    return list(
        AGENT_PLAN_PROMPT.invoke(
            {
                "question": question,
                "current_query": current_query,
                "current_route": current_route,
                "history": history,
            }
        ).to_messages()
    )


# =============================================================================
# 第 8 期：多轮上下文化 prompt
# =============================================================================

_CONTEXTUALIZE_SYSTEM = """你是一个多轮对话查询改写助手。请基于对话历史把用户当前问题改写成
**独立完整、可单独检索**的问句：

- 消解指代："它"、"这个"、"上面提到的…"、"刚才那个…"等
- 补全省略：用户在追问场景里经常省略主语或宾语，需要从历史里把缺失成分补全
- 不要回答问题，不要扩展含义，不要改变用户的真实意图
- 不要加任何引号、编号、解释，只输出单行改写后的问句
- 如果当前问题已经独立完整，直接原样输出

【对话历史】
{history}"""

_CONTEXTUALIZE_HUMAN = "{question}"

CONTEXTUALIZE_PROMPT = ChatPromptTemplate.from_messages(
    [("system", _CONTEXTUALIZE_SYSTEM), ("human", _CONTEXTUALIZE_HUMAN)]
)


def build_contextualize_messages(question: str, history: str) -> list[BaseMessage]:
    """组装多轮上下文化改写的 messages。

    【为什么 history 是【已格式化好的纯文本】而不是 Message 列表】：
    把 Message → 纯文本的转换放在调用方（query_rewriter 的 _format_history_text），
    让 prompt 层保持"只认字符串"的简单契约 ——
    与 build_agent_plan_messages 的 history 参数同一做法。

    :param question: 用户当前这一轮的原始提问
    :param history: 已格式化的对话历史文本（每行形如"用户: xxx"）
    :return: 可直接送入 LLM 的消息列表
    """
    return list(
        CONTEXTUALIZE_PROMPT.invoke(
            {"question": question, "history": history}
        ).to_messages()
    )


# =============================================================================
# 第 8 期：答案可信度校验 prompt
# =============================================================================

_VERIFY_ANSWER_SYSTEM = """你是一个 RAG 答案可信度校验员。请判断回答是否**完全被给定的参考片段支撑**：

判定标准：
- 答案中每个关键事实结论（数字、名称、规则、时间、条款）都必须能在某个片段原文中**直接找到**
- 礼貌话、转折词、复述问题的句子不算关键结论，可忽略
- 答案标了 [N] 引用编号的，要重点核对：N 号片段是否真的支撑这条结论
- 出现"未在知识库中找到"等明确拒答文案时直接判 verified=true（拒答本身就是被允许的输出）

判定结果：
- verified=true: 所有关键结论都被支撑
- verified=false: 存在编造、引用张冠李戴、或片段里完全没提到的事实

输出**单行 JSON**，键固定为 verified / reason，reason 用一句话说清理由（中文）。
示例：{{"verified": false, "reason": "答案提到差旅住宿 800 元，片段中只提到 600 元"}}"""

_VERIFY_ANSWER_HUMAN = """【用户问题】
{question}

【参考片段】
{chunks_text}

【模型回答】
{answer}

请输出校验结果 JSON。"""

VERIFY_ANSWER_PROMPT = ChatPromptTemplate.from_messages(
    [("system", _VERIFY_ANSWER_SYSTEM), ("human", _VERIFY_ANSWER_HUMAN)]
)


def build_verify_answer_messages(
    question: str, answer: str, chunks_text: str
) -> list[BaseMessage]:
    """组装答案可信度校验的 messages。

    :param question: 用户原始问题
    :param answer: 模型生成的完整回答（校验必须等流式结束，因此用完整文本）
    :param chunks_text: 已格式化的参考片段文本（由 format_context 产出）
    :return: 可直接送入 LLM 的消息列表
    """
    return list(
        VERIFY_ANSWER_PROMPT.invoke(
            {
                "question": question,
                "answer": answer,
                "chunks_text": chunks_text,
            }
        ).to_messages()
    )