"""
【模块职责说明】
本模块为知识库流水线中的「向量嵌入适配层」（Embedding Layer）。
主要负责配置、初始化并向全系统提供统一的文本向量化引擎实例（Embeddings）。
核心机制与设计目标如下：

1. 懒汉式单例复用（Lazy Singleton）：
   底层客户端内部维护了 HTTP 连接池、鉴权状态及模型参数。采用模块级私有全局变量 `_embeddings`
   进行懒加载缓存，避免每个文档摄取任务重复构建客户端与频繁建立 TCP/TLS 连接。

2. 契约驱动与依赖抽象（Interface Segregation）：
   对外返回 LangChain 统一抽象基类 `Embeddings`，隐藏具体供应商（如 OpenAI、Ollama 或第三方中转），
   方便未来无缝替换向量模型。

3. 前置环境配置校验（Fail-Fast 机制）：
   在首次尝试创建嵌入引擎时进行严格的环境变量防空校验，若核心凭证未配直接抛出业务异常阻断流程。
"""

from langchain_core.embeddings import Embeddings
from langchain_openai import OpenAIEmbeddings

from app.core.config import settings
from app.core.exceptions import ConfigurationError

# 语法（Python 原生类型注解）：_var: Type | None = None
#   变量名 (_embeddings)：模块私有全局变量（下划线命名约定），用作单例对象常驻内存的缓存槽（类似 Java private static）
#   类型注解 (Embeddings | None)：联合类型（Union Type），声明该槽位要么存放 Embeddings 实例，要么为初始空值 None
#   初始值 (= None)：应用启动初期不执行初始化动作，实行按需惰性加载
_embeddings: Embeddings | None = None


def get_embeddings() -> Embeddings:
    """获取全局唯一的文本嵌入引擎单例。

    【核心流程说明】：
    1. 单例校验：若缓存槽位已存在实例，直接返回，避免重复连接与实例化开销。
    2. 防御校验：读取配置并检查 API Key，若未配置则抛出 ConfigurationError 快速失败。
    3. 引擎构建：基于系统配置初始化 OpenAIEmbeddings 客户端并回写全局缓存。

    【返回值】：
    - Embeddings: LangChain 统一向量嵌入抽象接口实例
    """
    # 语法（Python 原生关键字）：global 变量名
    #   作用：告诉 Python 解释器在当前函数内部要修改的是模块顶层的全局变量 _embeddings，
    #         而不是创建一个同名的局部变量，确保单例赋值持久保留在模块级作用域中
    global _embeddings

    # 语法（Python 原生控制流与空值判断）：if 变量 is not None
    #   判断逻辑：经典懒汉式单例双重检查前置判断；若已被初始化过，直接复用已常驻内存的实例返回
    if _embeddings is not None:
        return _embeddings

    # 语法（Python 原生字符串隐式布尔判断）：if not 字符串
    #   防御性断言：若读取到的 embedding_api_key 为空字符串或 None，触发 Fail-Fast（快速失败）逻辑
    if not settings.embedding_api_key:
        # 语法（自定义业务异常）：raise ExceptionClass(msg) 显式抛出配置缺失异常，提示排查 .env 文件
        raise ConfigurationError("Embedding API key 未配置，请在 .env 设置 EMBEDDING_API_KEY")

    # 语法（外部框架 LangChain）：OpenAIEmbeddings(...)
    #   参数1 (model: str)：指定向量模型名称（如 text-embedding-3-small、bge-m3 等）
    #   参数2 (api_key: SecretStr | str)：API 访问密钥，用于大模型服务商接口鉴权
    #   参数3 (base_url: str | None)：API 请求基地址（用于支持自建网关、内网代理或中转服务商）
    #   参数4 (dimensions: int | None)：指定生成的向量稠密维度（部分新模型如 text-embedding-3 支持降维截断）
    #   参数5 (chunk_size: int)：单次批量请求向 API 投递的最大切片数（Batch Size），防止单次 HTTP Payload 过大导致网关超时
    #   参数6 (check_embedding_ctx_length: bool)：客户端侧是否预先使用本地 tokenizer 计算并拦截超长文本；
    #          设为 False 显式关闭本地长度预检，避免缺失本地分词词表报错，直接交由上游 splitter 保证长度安全
    _embeddings = OpenAIEmbeddings(
        model=settings.embedding_model,
        api_key=settings.embedding_api_key,
        base_url=settings.embedding_base_url,
        dimensions=settings.embedding_dim,
        chunk_size=settings.embedding_batch_size,
        check_embedding_ctx_length=False,
    )

    # 语法（Python 原生返回）：返回最新创建好的全局唯一向量嵌入引擎实例
    return _embeddings