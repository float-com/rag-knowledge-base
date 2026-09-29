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

# 评测裁判专用的单例缓存（与问答客户端分开持有，理由见 get_judge_model 的文档字符串）
_judge_model: BaseChatModel | None = None


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


def get_judge_model() -> BaseChatModel:
    """获取或初始化【评测裁判专用】的 Chat 客户端（与问答客户端刻意分开）。

    【为什么不能直接复用 get_chat_model() —— 一次线上事故的修复】
    问答客户端为了 SSE 打字机效果开了 `streaming=True`，并且【没有设置 timeout】
    （openai SDK 的默认超时是 600 秒）。这套参数用在 RAGAS 裁判上有两个致命问题：

    1. **没有请求级超时**：RAGAS 一轮 50 题评测要发上千次裁判调用，
       跨云链路上只要有个别请求"连上了但对端不回"，就会一直挂到超时上限 ——
       实测现象就是"整轮评测跑了一小时还没结束"。
    2. **流式是白付的开销**：裁判要的是"一次性完整输出"（结构化判定结果），
       流式只会多一层聚合，不产生任何收益。

    因此裁判单独用这个客户端：**非流式 + 请求级超时 + 收敛的重试次数**。

    【为什么重试次数要压到 1】
    RAGAS 自身还有一层 tenacity 重试（RunConfig.max_retries）。
    若 SDK 也按默认重试 2 次，两层叠乘会让最坏耗时变成 3×3×45 秒 ≈ 6 分钟，
    必然撞穿作业级超时。把重试交给 RAGAS 一层负责，整条链路才可预测。

    :return: 实现了 BaseChatModel 接口的单例裁判模型对象
    :raises ConfigurationError: 当未在环境变量中正确配置 CHAT_API_KEY 时触发
    """
    global _judge_model

    # 快速路径：单例已构建则直接复用（与问答客户端同理，避免重复建连接池）
    if _judge_model is not None:
        return _judge_model

    if not settings.chat_api_key:
        raise ConfigurationError("Chat API key 未配置，请在 .env 设置 CHAT_API_KEY")

    _judge_model = ChatOpenAI(
        model=settings.chat_model,
        api_key=settings.chat_api_key,
        base_url=settings.chat_base_url,
        # 裁判要的是确定性判定，温度同样设为 0
        temperature=0,
        # ★ 与问答客户端的关键区别 1：非流式。裁判不需要打字机效果。
        streaming=False,
        # ★ 与问答客户端的关键区别 2：请求级超时（秒）。挂住的请求快速失败，而非挂满 600 秒。
        timeout=settings.ragas_request_timeout_seconds,
        # ★ 与问答客户端的关键区别 3：SDK 级重试收敛为 1 次，把重试权交给 RAGAS（理由见上文）。
        max_retries=settings.ragas_request_max_retries,
    )

    return _judge_model