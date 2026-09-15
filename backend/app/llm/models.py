"""Chat 模型客户端管理层。

【模块职责说明】：
1. 客户端单例模式与连接池复用（Singleton Pattern & Connection Pooling）：
   LangChain 的 ChatOpenAI 客户端底层持有基于 httpx 的 HTTP 连接池与会话凭据。
   采用模块级单例缓存避免在每次请求中重复实例化，极大减少 TCP 握手与 TLS 协商开销，提升并发吞吐能力。
2. 基础大模型抽象解耦（LLM Abstraction）：
   面向 `BaseChatModel` 抽象接口进行编程与类型标注，解耦具体的大模型厂商依赖，
   上层业务仅依赖标准接口，便于后续无缝平替或扩展其他多模态/大模型实现。
3. 配置守卫与零信任校验（Configuration Guard）：
   在首次延迟加载客户端时，对关键凭据（如 API Key）进行前置守卫校验，
   缺少关键配置时显式抛出强类型业务异常 `ConfigurationError`，杜绝静默失败并指导运维修复。
4. RAG 问答确定性调控（Determinism & Streaming）：
   锁定 `temperature=0` 保证强指令遵循与严格引用编号约束，避免输出漂移与模型幻觉；
   全量开启 `streaming=True`，为全链路 Server-Sent Events (SSE) 流式打字机交互提供底层基建。
"""

# 从 LangChain 核心抽象包引入基础聊天模型协议接口
from langchain_core.language_models.chat_models import BaseChatModel
# 引入基于 OpenAI 规范协议的 Chat 实现类（可无缝接入支持 OpenAI 协议的各类第三方厂商如 DashScope、Ollama 等）
from langchain_openai import ChatOpenAI

# 引入项目全局配置单例
from app.core.config import settings
# 引入项目配置异常基类
from app.core.exceptions import ConfigurationError

# 模块级私有变量，作为 Chat 模型实例的单例缓存容器；未初始化时为 None
_chat_model: BaseChatModel | None = None


def get_chat_model() -> BaseChatModel:
    """
    获取或初始化流式 ChatOpenAI 单例模型客户端。

    单例缓存：模型客户端底层持有 httpx 异步连接池，反复创建会严重浪费系统与网络资源。

    :return: 实现了 BaseChatModel 接口的单例模型对象
    :raises ConfigurationError: 当未在环境变量中正确配置 CHAT_API_KEY 时触发
    """
    global _chat_model

    # 1. 快速路径（Fast Path）：若单例已构建，直接复用已有实例并返回
    if _chat_model is not None:
        return _chat_model

    # 2. 配置守卫校验：强校验 API Key 是否有效配置，缺失则直接拦截并阻断启动或调用
    if not settings.chat_api_key:
        raise ConfigurationError("Chat API key 未配置，请在 .env 设置 CHAT_API_KEY")

    # 3. 初始化并缓存单例实例
    #    ChatOpenAI 默认请求 OpenAI 官方 endpoint，通过传入 base_url 可无缝切换至 DashScope 等兼容端点
    _chat_model = ChatOpenAI(
        model=settings.chat_model,  # 模型版本名称（如 qwen-plus, gpt-4o 等）
        api_key=settings.chat_api_key,  # 模型 API Key
        base_url=settings.chat_base_url,  # 模型兼容端点 Base URL（如阿里 DashScope 的 OpenAI 兼容接口）
        # 知识库问答 + 严格的引用编号约束属于强指令遵循任务，温度设为 0
        # 能够最大限度消除随机性，避免 [N] 编号在不同 chunk 之间发生漂移与幻觉
        temperature=0,
        # 开启底层流式输出支持，便于后续上层实现逐 Token 的 SSE 打字机流式响应
        streaming=True,
    )

    return _chat_model