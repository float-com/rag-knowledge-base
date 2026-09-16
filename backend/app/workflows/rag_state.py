"""RAG 工作流图状态模型层。

【模块职责说明】：
1. 图工作流全局状态契约（Graph State Contract）：
   基于 LangGraph 范式声明图计算流水线的共享数据载体 `RAGState`，
   打通从初始提问、多轮历史装配、Query 改写、向量检索召回、拒答熔断、LLM 回答生成到最终持久化落库回写的全生命周期。
2. 增量状态合并策略（Partial Updates / total=False）：
   采用 `TypedDict(total=False)` 声明非强约束宽容契约，
   每个独立图节点（Node）仅需聚焦自身业务边界并返回当前产出的增量字段字典，
   交由 LangGraph 底层状态机自动执行增量更新与状态合并，避免各节点传递全量无关参数。
3. 领域对象与图状态解耦（Decoupled Pipeline Flow）：
   解耦各计算节点间的强类型调用依赖，使上下文装载（load_context）、检索（retrieve）、生成（generate）
   均面向统一状态进行读写，保障节点的高内聚与可插拔性。
"""

from typing import TypedDict
from uuid import UUID

# 引入数据库消息持久化实体（用于传递多轮会话记录）
from app.db.models import Message
# 引入检索层返回的不可变切片数据契约
from app.retrieval.vector_retriever import RetrievedChunk


class RAGState(TypedDict, total=False):
    """
    RAG 工作流全局状态字典。

    total=False 表示所有字段均为可选（Optional），节点执行时只需返回其自身更新/追加的字段字典。
    """

    # --- 1. 工作流初始输入阶段 (Input) ---
    conversation_id: UUID       # 当前会话的唯一标识 UUID
    question: str              # 用户在输入框键入的原始提问内容

    # --- 2. 上下文加载节点 (load_context 节点产出) ---
    chat_history: list[Message] # 数据库中预加载的正序最近多轮对话历史列表

    # --- 3. 意图与 Query 改写节点 (normalize_query 节点产出) ---
    query: str                  # 经过标准化/改写后的检索查询词（基础版本与 question 相同）

    # --- 4. 知识检索与熔断判断节点 (retrieve 节点产出) ---
    retrieved_chunks: list[RetrievedChunk] # 向量数据库召回并完成相关度计算的分块列表
    # 是否触发知识库拒答（例如检索结果为空或相似度过低）。为 True 时在条件路由中直接跳过 generate 生成节点
    refused: bool

    # --- 5. 大模型回答生成节点 (generate 节点产出) ---
    answer: str                 # LLM 根据参考片段最终生成的回答内容（包含 [N] 引用标记）

    # --- 6. 持久化后置落库节点 (chat_service 落库后回写) ---
    user_message_id: UUID       # 写入数据库后生成的本轮用户提问 Message 唯一主键
    assistant_message_id: UUID  # 写入数据库后生成的本轮 AI 回复 Message 唯一主键