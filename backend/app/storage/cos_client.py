"""
【模块职责说明】
本模块为腾讯云对象存储（COS）客户端轻量封装层，核心作用如下：

1. 异步 I/O 桥接适配：
   腾讯云官方 cos-python-sdk-v5 内部均为同步阻塞调用。本模块利用 asyncio.to_thread
   将阻塞的底层网络 I/O 调度至独立线程池，避免阻塞 FastAPI 主事件循环。

2. 凭据合法性前置断言：
   在实例化时自动校验 settings.cos_configured，若配置缺失直接中断并抛出 ConfigurationError，
   由全局异常处理器统一向客户端返回标准 HTTP 503 状态码。

3. 轻量健康探活（Ping）：
   封装基于 head_bucket 的非侵入式健康检查方法，用于系统启动检查与可用性监控，验证权限与网络连通性。

4. 惰性单例模式：
   通过 get_cos_client 函数实现单例按需初始化，避免无 COS 操作场景下的多余开销与启动强耦合。
"""

import asyncio

from qcloud_cos import CosConfig, CosS3Client
from qcloud_cos.cos_exception import CosClientError, CosServiceError

from app.core.config import settings
from app.core.exceptions import ConfigurationError
from app.core.logging import get_logger

# 获取当前模块的专属 Logger 实例
logger = get_logger(__name__)


class CosClient:
    """腾讯云 COS 客户端操作包装类。"""

    def __init__(self) -> None:
        """初始化 COS 客户端配置并建立内部 S3 客户端实例。

        若必要配置项缺失，则主动抛出业务配置异常中断操作。
        """
        if not settings.cos_configured:
            raise ConfigurationError("腾讯云 COS 未配置，请在 .env 中填写 COS_* 变量")

        # 装配腾讯云官方基础连接配置
        config = CosConfig(
            Region=settings.cos_region,
            SecretId=settings.cos_secret_id,
            SecretKey=settings.cos_secret_key,
        )
        # 初始化底层 S3 兼容客户端
        self._client = CosS3Client(config)
        self._bucket = settings.cos_bucket

    async def ping(self) -> bool:
        """通过 head_bucket 验证凭据与桶可达性。

        【异步设计说明】：
        cos-python-sdk-v5 是基于 requests 库实现的同步 SDK，调用时会阻塞当前执行线程。
        而 FastAPI 的每个 worker 进程内部跑的是一个单线程 event loop，如果在 async def
        里直接发起这种同步网络 I/O，整个 event loop 就会被卡住，导致同一 worker 上的其他并发请求全部排队挂起。

        因此这里使用 asyncio.to_thread 包裹同步方法 self._client.head_bucket：
        asyncio.to_thread 会把该阻塞调用派发到 Python 默认的线程池中执行；
        主协程在此处 await 挂起并释放 CPU 控制权给 event loop 继续响应其他请求。
        通过这种方式，既能无缝复用成熟的官方同步 SDK，又不会破坏异步服务的高并发吞吐能力。
        """
        try:
            await asyncio.to_thread(self._client.head_bucket, Bucket=self._bucket)
            return True
        except (CosClientError, CosServiceError) as exc:
            logger.warning("COS ping failed: %s", exc)
            return False


# 全局单例缓存指针
_cos_client: CosClient | None = None


def get_cos_client() -> CosClient:
    """惰性获取 CosClient 单例对象。

    首次调用时进行实例化；若配置不全则直接抛出 ConfigurationError，
    由全局错误处理器转换为标准 HTTP 503 JSON 响应。
    """
    global _cos_client
    if _cos_client is None:
        _cos_client = CosClient()
    return _cos_client