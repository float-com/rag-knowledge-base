"""RAG 大模型流式生成图节点。

【模块职责说明】：
1. 异步流式生成与逐 Token 推送（Streaming Generation & Token-by-Token Yield）：
   作为 RAG 流水线中核心的 LLM 推理生成节点，不同于常规节点返回 `dict` 增量，
   本函数返回 `AsyncIterator[str]` 异步生成器，以便上层直接挂接 Server-Sent Events (SSE) 协议向前端实时吐字。
2. 消息动态组装桥接（Prompt Orchestration Bridge）：
   抽取全局状态中的 `question`、`retrieved_chunks` 与可选的 `chat_history`，
   调用 `build_answer_messages` 辅助函数完成变量插值与标准 `list[BaseMessage]` 组装。
3. LangChain 统一内容模型兜底（Strict Type Guard & Polyfill）：
   由于 LangChain 消息分块的 `content` 属性在类型注解中为 `str | list[str | dict]`（针对多模态与复合块），
   本模块实现类型守卫分支，确保无论是纯文本字符串还是多段字典字典切片，均能统一提取文本并无损 `yield`，保证类型检查器严格通过。
4. 职责正交分离（Separation of Concerns）：
   本节点专注高并发下的“逐块流式产出”，不介入字符串的全局聚合；
   全局答案的拼接拼接与最终回写 `state["answer"]` 全权交由外层编排调度器负责。
"""

from collections.abc import AsyncIterator

# 引入获取模型单例工厂方法
from app.llm.models import get_chat_model
# 引入提示词消息构建工具
from app.llm.prompts import build_answer_messages
# 引入 RAG 状态数据定义
from app.workflows.rag_state import RAGState


async def stream_generate(state: RAGState) -> AsyncIterator[str]:
    """
    流式生成：逐 token yield。

    调用方负责拼接成完整答案并写回 state；这里只关心"逐块输出"。
    refused 状态下调用方应直接跳过本函数。

    :param state: 当前工作流状态字典（必须包含 question, retrieved_chunks，可选 chat_history）
    :return: 字符串增量的异步迭代器 AsyncIterator[str]
    """
    # 1. 组装输入给大模型的多消息列表
    #    注入当前提问、检索到的知识库分块以及历史对话
    messages = build_answer_messages(
        question=state["question"],
        chunks=state["retrieved_chunks"],
        history=state.get("chat_history", []),
    )

    # 2. 异步调用 Chat 模型的 astream 方法，获取流式切片流
    async for chunk in get_chat_model().astream(messages):
        text = chunk.content

        # 过滤空 content（如仅包含元数据或流首帧空包）
        if not text:
            continue

        # 3. 类型守卫与分支收窄（Type Narrowing）：
        # LangChain 的 AIMessageChunk.content 声明为联合类型：str | list[str | dict]
        # 大多数大模型返回纯字符串；若为真，Pyright 会将 text 类型强收窄为 str
        if isinstance(text, str):
            # 异步生成器产出单个纯文本 Token，直供下游 SSE 推流
            yield text
        else:
            # 2. 复合结构兜底解析（防范多模态或结构化返回）：
            # 此时 text 为列表，例如：[{"type": "text", "text": "你好"}, ...]
            # 语法拆解：
            # - if isinstance(part, dict)：列表推导过滤，仅处理字典类型的切片元素
            # - part.get("text", "")：字典安全取值，避免抛出 KeyError 导致流式中断
            # - (... for part in text ...)：生成器表达式（惰性求值，节约内存）
            # - "".join(...)：将各片段提取的文本无缝拼接为单个完整字符串
            yield "".join(part.get("text", "") for part in text if isinstance(part, dict))