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

4. 核心文件存储能力（CRUD 扩展）：
   - bucket / region: 暴露只读属性，供上层服务快速填充 Document 实体元数据字段。
   - put_object: 字节流上传与同名覆盖写入。
   - get_object: 异步读取远端 Object 全部字节流，用于解析引擎（如 Docling）提取正文。
   - delete_object: 幂等安全删除指定 Object，支持文档物理删除时的级联存储清理。

5. 惰性单例模式：
   通过 get_cos_client 函数实现单例按需初始化，避免无 COS 操作场景下的多余开销与启动强耦合。
"""

import asyncio
import hashlib
from typing import Any

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

    # ==========================================================================
    # 1. 基础配置元数据暴露（只读属性）
    # ==========================================================================
    # @property 装饰器作用：
    # 1. 语法糖封装：将方法转换为类似普通属性的访问形式，外部调用时使用 client.bucket 而无需加括号（client.bucket()）。
    # 2. 只读保护（等价于 Java 的 Getter）：仅声明 Getter 逻辑而未定义对应的 @bucket.setter，
    #    防止外部代码意外执行 client.bucket = "xxx" 篡改运行时存储桶配置。
    @property
    def bucket(self) -> str:
        """获取当前绑定的 COS 存储桶名称。

        供业务层在构建 Document 实体记录时直接读取并持久化到 cos_bucket 字段。
        """
        return self._bucket

    # @property 装饰器作用：
    # 1. 统一对外访问接口：屏蔽底层读取细节（实际委托给全局配置 settings.cos_region），保持面向对象封装的一致性。
    # 2. 只读保护：提供对存储桶所在地域标识的只读访问，禁止外部直接赋值覆写。
    @property
    def region(self) -> str:
        """获取当前 COS 存储桶所属地域标识。

        供业务层在构建 Document 实体记录时直接读取并持久化到 cos_region 字段。
        """
        return settings.cos_region

    # ==========================================================================
    # 2. 健康探活
    # ==========================================================================
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

    # ==========================================================================
    # 3. 文件存储操作（CRUD）
    # ==========================================================================
    async def put_object(self, *, key: str, body: bytes, content_type: str) -> None:
        """上传字节流到指定 object key，同名文件直接覆盖。

        【设计说明】：
        1. 强制关键字传参（* 参数语法）：调用方必须显式声明 key=..., body=..., content_type=...，
           避免在底层大字节流传递时因位置传参颠倒导致静默 Bug。
        2. 异步线程池调度：通过 asyncio.to_thread 调用同步 SDK 的 put_object，确保网络传输耗时不阻塞主线程。
        """
        # =========================================================================
        # 核心语法糖解析：
        # asyncio.to_thread(func, *args, **kwargs)
        #
        # 参数 1: 必须传递函数对象本身（self._client.put_object），切记不能加括号执行它！
        # 参数 2 及以后: 会被无损传递给底层目标函数的关键字参数（Bucket、Key、Body 等）
        #
        # 行为：
        # 1. 将同步阻塞的 put_object 投递给 Python 内部默认的 ThreadPoolExecutor（线程池）；
        # 2. 返回一个 asyncio.Future 协程等待句柄；
        # 3. await 挂起当前上传协程，释放 CPU 资源，让 FastAPI 主事件循环继续处理其他并发请求；
        # 4. 远端数据写入完成、工作线程返回后，唤醒当前协程继续执行后续代码。
        # =========================================================================
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self._bucket,
            Key=key,
            Body=body,
            ContentType=content_type,
        )

    # ==========================================================================
    # 3.1 浏览器直传辅助能力
    # ==========================================================================
    # 旧上传链路使用 put_object，由后端接收完整 bytes 后再转存 COS。
    # 新上传链路只由后端生成临时签名，文件正文直接从浏览器发送至 COS。
    # 以下方法继续遵循“同步 SDK 放入线程池”的统一异步桥接规范。
    async def generate_presigned_put_url(
        self,
        *,
        key: str,
        content_type: str,
        expires: int = 300,
    ) -> str:
        """生成用于浏览器直传的临时 PUT 预签名 URL。

        【签名头一致性】：
        1. Content-Type 会参与签名计算；
        2. 浏览器实际 PUT 时必须发送同一个 Content-Type；
        3. 签名请求头不一致时，COS 会判定签名无效。

        【安全边界】：URL 只具备指定 Key 的 PUT 能力，并通过 Expired 限制有效时间，
        不向前端暴露 SecretId 或 SecretKey。
        """
        return await asyncio.to_thread(
            self._client.get_presigned_url,
            Method="PUT",
            Bucket=self._bucket,
            Key=key,
            Expired=expires,
            Headers={"Content-Type": content_type},
        )

    async def head_object(self, key: str) -> dict[str, Any]:
        """读取指定 Object 的元数据，不下载文件正文。

        complete 接口不能仅依据前端传来的“上传成功”判断文件是否完整，
        因此必须从 COS 服务端读取 Content-Length 和 Content-Type 做第二次校验。
        """
        return await asyncio.to_thread(
            self._client.head_object,
            Bucket=self._bucket,
            Key=key,
        )

    async def hash_object(self, key: str) -> str:
        """在线程池中分块读取 Object，并计算 SHA-256 摘要。

        【实现机制】：
        1. get_object 只负责取得远端响应流；
        2. 每次读取 1 MB，避免为了计算哈希额外构造完整 bytes；
        3. 所有同步网络读取和哈希计算都在工作线程完成；
        4. finally 中关闭流，防止后台 finalize 长时间占用 COS 连接。

        直传 init 阶段后端没有文件正文，因此在 complete 后的后台 finalize 阶段计算哈希，
        继续兼容 documents.file_hash 唯一去重约束。
        """
        def _hash() -> str:
            # 将建立远端响应、分块读取和流关闭封装在同一个同步闭包中，
            # 确保底层 SDK 的阻塞 I/O 不回到 FastAPI 事件循环线程。
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            stream = response["Body"].get_raw_stream()
            digest = hashlib.sha256()
            try:
                # 固定读取块大小，控制哈希阶段的峰值内存。
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
            finally:
                # 无论读取成功还是异常，都释放 COS 返回的底层流资源。
                stream.close()
            return digest.hexdigest()

        return await asyncio.to_thread(_hash)

    async def get_object(self, key: str) -> bytes:
        """读取指定 object 的全部二进制字节流。

        【实现机制与内存考量】：
        1. 闭包辅助函数 _read:
           腾讯云 SDK 返回的 response['Body'] 是一个流式数据源对象（CosServiceResponse），
           如果仅把 get_object 派发到线程池，后续的主线程 read() 依然会触发同步网络 I/O。
           因此将 SDK 获取响应与 get_raw_stream().read() 封装在同一个同步闭包中，
           交由 asyncio.to_thread 完整地在独立工作线程中拉取全部数据。
        2. 适用场景：
           知识库系统后续会将文件字节流直接送入解析器（如 Docling/PDF 提取引擎）进行内存解析。
        """

        # =========================================================================
        # 辅助同步闭包函数：将“请求建立”与“全量流读取”合并打包
        # =========================================================================
        def _read() -> bytes:
            # [步骤 1 - 建立连接与获取响应头]:
            # 调用底层同步 SDK 的 get_object 方法。
            # 注意：此处并没有真正把整个文件的二进制实体全部下载完，
            # 服务端仅返回了 HTTP 状态码、响应头（Headers）以及一个底层的 Socket 原始流句柄。
            response = self._client.get_object(Bucket=self._bucket, Key=key)

            # [步骤 2 - 阻塞拉取全量网络字节流]:
            # response["Body"] 是一个流式对象（CosServiceResponse，对标 Java 的 InputStream）。
            # get_raw_stream().read() 才是真正顺着网络 Socket 一块一块把文件字节（如数十 MB 的 PDF）
            # 完整读取到内存中的过程，整个过程耗时较长且完全是同步阻塞的。
            # 将它写在 _read 闭包内，确保这部分重度 I/O 依然留在工作线程池中执行。
            return response["Body"].get_raw_stream().read()

        # =========================================================================
        # 异步调度与控制权让渡：
        # 1. 传入 _read 函数指针（不加括号），让 Python 默认的 ThreadPoolExecutor 调度执行。
        # 2. 整个“握手 + 流式全量传输”过程都在独立工作线程中闭环完成。
        # 3. await 挂起当前协程，FastAPI 主事件循环可以自由去处理其他用户的并发请求。
        # 4. 当远端流完全读取完毕，返回完整的 bytes 二进制数据。
        # =========================================================================
        return await asyncio.to_thread(_read)

    async def delete_object(self, key: str) -> None:
        """从存储桶中物理删除指定 key 的对象。

        【幂等特性】：
        腾讯云 COS 服务端规范：若请求删除一个不存在的 Key，接口仍会返回 204/成功，
        不会抛出 404 异常。因此本方法天然具备幂等性，无需在调用前二次校验对象是否存在。
        """
        await asyncio.to_thread(
            self._client.delete_object,
            Bucket=self._bucket,
            Key=key,
        )


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