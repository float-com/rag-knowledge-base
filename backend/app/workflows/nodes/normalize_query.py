"""Query 标准化与意图预处理图节点。

【模块职责说明】：
1. 检索查询预处理与透传（Query Normalization / Passthrough）：
   作为 RAG 图工作流中介于上下文加载（load_context）与知识检索（retrieve）之间的预处理节点。
   当前版本作为核心透传节点，将用户的原始输入提问（question）直接映射并标准化为后续检索专用的查询词（query）。
2. 高级检索策略扩展点（Future-Proof Extension Hook）：
   为后续功能演进预留标准的架构扩展位点，便于平滑接入多轮会话指代消解（Coreference Resolution）、
   Query 改写重写（Query Rewriting）、关键词抽取（Keyword Extraction）或 HyDE（假设性文档嵌入）等高级检索优化技术。
3. 状态增量契约对齐（Partial State Update）：
   遵循 LangGraph 的 `TypedDict(total=False)` 规范，仅返回产出的 `{"query": ...}` 键值对，
   交由底层图执行器自动合并写入全局状态 `RAGState`。
"""

# 引入 RAG 共享图状态类型定义
from app.workflows.rag_state import RAGState


async def normalize_query(state: RAGState) -> RAGState:
    """
    RAG 工作流节点：对用户原始提问进行标准化清洗、改写或透传，产出用于向量检索的 query 字符串。

    :param state: 当前工作流状态（必须包含 "question" 字段）
    :return: 仅包含增量字段 `{"query": str}` 的字典，用于合并回全局状态
    """
    # 当前基线版本作为透传节点：直接将用户输入的原始提问 state["question"] 赋给下游使用的检索词 "query"
    # 后续可在此处结合 chat_history 扩展大模型改写逻辑（如消解代词“它/这个”，补充上下文语境）
    return {"query": state["question"]}