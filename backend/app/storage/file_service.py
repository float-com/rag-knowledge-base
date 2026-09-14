"""
【模块职责说明】
本模块为面向业务领域的文件存储应用服务层（FileService），核心作用如下：

1. 业务防腐隔离与存储解耦（Anti-Corruption Layer）：
   作为上层业务引擎（DocumentService）与底层存储驱动（CosClient）之间的防腐层，
   屏蔽具体对象存储介质（如 COS、MinIO、S3）的底层 SDK 细节与调用协议，
   实现上层业务与底层存储基础设施的彻底解耦。

2. 基于内容寻址（CAS）的幂等与秒传设计：
   封装 build_object_key 静态生成规则，采用文件的唯一哈希（如 SHA-256）派生远端存储路径。
   相同内容的文件多次上传必然命中同一个 Object Key，天然具备存储幂等、文件去重与秒传能力；
   同时保留原始文件扩展名，确保控制台直开预览与 HTTP 直链解析格式正常。

3. 业务级核心存储操作（CRUD）：
   - bucket / region: 通过只读属性（@property）向上层代理暴露存储元数据，
     便于上层业务在持久化 Document 实体时直接提取字段。
   - upload: 接收字节流与元数据，自动计算 key 并完成异步上传，返回标准持久化键。
   - download: 异步全量读取指定 Object 二进制数据至内存，直接供给上层解析引擎（如 Docling），
     避免本地磁盘临时文件的 I/O 开销与清理负担。
   - delete: 封装容错删除逻辑。

4. 分布式最终一致性与防御性容错：
   在 delete 操作中执行静默降级策略——捕获底层客户端与服务端异常并记录 Warning 日志，
   严格保证“不向上抛出异常阻断请求”。避免在先删数据库、后删云端存储的常见时序中，
   因存储网络抖动导致整个 HTTP 事务报错，造成“DB 记录已删、用户界面报错”的脏状态。

5. 依赖注入（DI）友好与单例集成：
   构造函数支持外部传入 CosClient 实例，为单元测试中的 Mock/Stub 替换提供原生支持；
   同时提供 get_file_service 工厂函数，便于在 FastAPI 路由中通过 Depends 机制进行依赖注入。
"""

from qcloud_cos.cos_exception import CosClientError, CosServiceError
from uuid import UUID
from typing import Any

from app.core.logging import get_logger
from app.storage.cos_client import CosClient, get_cos_client

# 初始化模块级业务日志记录器
logger = get_logger(__name__)


class FileService:
    """业务文件存储服务类。

    对底层 CosClient 进行二级封装，统一对外暴露纯净的业务文件操作抽象。
    """

    def __init__(self, cos: CosClient | None = None) -> None:
        """初始化文件服务实例。

        【设计说明 - 依赖注入（DI）】：
        1. 允许外部显式传入 cos 实例（便于在单元测试时注入 Mock 对象）。
        2. 若未传入，则通过 get_cos_client() 懒加载获取全局唯一的 CosClient 单例。
        """
        self._cos = cos or get_cos_client()

    # =========================================================================
    # 1. 元数据代理（只读 Getter 机制）
    # =========================================================================
    @property
    def bucket(self) -> str:
        """获取当前绑定的存储桶名称（只读代理）。

        将对底层 CosClient._bucket 的只读属性进一步透明转发给上层业务。
        """
        return self._cos.bucket

    @property
    def region(self) -> str:
        """获取当前存储桶所在的地域标识（只读代理）。"""
        return self._cos.region

    # =========================================================================
    # 2. 远端存储键（Object Key）构建规则
    # =========================================================================
    @staticmethod
    def build_object_key(file_hash: str, suffix: str) -> str:
        """根据文件内容哈希值与扩展名，派生存储桶内的唯一存储路径（Key）。

        【设计说明】：
        1. 静态方法（@staticmethod）：此逻辑为纯计算函数，不依赖实例状态（self），
           既可通过实例调用，也可供外部静态工具类直接计算。
        2. 基于内容寻址（CAS）：以 file_hash（如 SHA-256）作为核心标识，
           使得相同内容的文件多次上传必然映射到同一个远端 Key，具备天然的幂等性与去重潜力。
        3. 保留原始后缀：拼接 suffix（如 .pdf、.docx），确保在云存储控制台直接预览或
           HTTP 直链下载时能被浏览器与系统直接识别文件格式。
        """
        return f"documents/{file_hash}{suffix}"

    # =========================================================================
    # 3. 业务文件增删查操作（CRUD）
    # =========================================================================
    async def upload(
            self,  # 当前 FileService 的实例引用（等价于 Java 中的 this 关键字）
            *,  # 强制关键字传参标记：其后的所有实参必须显式写出 key=...，禁止位置传参，防止字节流与元数据颠倒
            content: bytes,
            file_hash: str,
            suffix: str,
            mime_type: str,
    ) -> str:
        """执行业务文件上传流程，并返回最终持久化的 object_key。

        【设计说明】：
        1. 强制关键字传参（* 参数语法）：防止调用方由于参数顺序混淆而造成字节流传错。
        2. 自动派生 Key：封装了 build_object_key 的拼接细节，上层只需提供核心元数据。
        3. 最终返回生成好的 key：供上层业务构建并持久化 Document 实体记录到数据库。
        """
        # 1. 动态生成符合规范的对象存储路径
        key = self.build_object_key(file_hash, suffix)

        # 2. 委托底层 CosClient 发起非阻塞的线程池上传
        # --------------------------------------------------------------------------
        # 【机制答疑与链路解析】：
        # 问 1：为什么这里能定位到 CosClient 类的方法？
        # 答：在 __init__ 初始化时，通过依赖注入执行了 self._cos = cos or get_cos_client()，
        #     将 CosClient 类的实例对象挂载到了当前实例的成员变量 self._cos 上（类似 Java 的 @Autowired 成员注入）。
        #     因此 self._cos.put_object 能够直接精准索引并调用 CosClient 类中定义的方法。
        #
        # 问 2：为什么这里不需要像 asyncio.to_thread 那样传递函数引用作为第一个参数？
        # 答：因为 asyncio.to_thread 是底层的“线程调度器”，其语法要求接收一个待调度的函数指针；
        #     而当前调用的 self._cos.put_object 本身已经被我们封装成了一个标准的 async 异步协程方法。
        #     它自身在内部已经把线程池调度逻辑全部闭环封装完毕，上层业务只需像常规异步函数一样
        #     加上括号传参，并通过 await 直接等待其执行完成即可。
        # --------------------------------------------------------------------------
        await self._cos.put_object(
            key=key,
            body=content,
            content_type=mime_type,
        )

        return key

    # =========================================================================
    # 4. 浏览器直传业务能力
    # =========================================================================
    # 这组方法只服务于“预签名 PUT + complete”新链路，旧的 upload() 方法继续服务
    # multipart 上传。通过 FileService 统一屏蔽 CosClient 的具体 SDK 参数，避免路由层
    # 直接依赖腾讯云 SDK。
    async def create_presigned_upload(
        self,
        *,
        upload_id: UUID,
        filename: str,
        mime_type: str,
    ) -> tuple[str, str]:
        """创建隔离的临时 Object Key 与浏览器直传预签名 URL。

        【Object Key 设计】：
        - upload_id 提供每次上传的唯一命名空间，避免并发同名覆盖；
        - filename 只保留最后一段文件名，剥离路径分隔符，防止路径穿越；
        - 原始文件名仍由 UploadSession 保存，供后续 Document 展示。
        """
        # 只允许文件名参与 Key 的最后一段，不能让客户端传入的目录改变 COS 路径。
        safe_filename = filename.replace("\\", "/").rsplit("/", 1)[-1] or "upload"
        key = f"document-uploads/{upload_id}/{safe_filename}"
        # 预签名 URL 的 Content-Type 必须和浏览器 PUT 时发送的请求头保持一致。
        url = await self._cos.generate_presigned_put_url(
            key=key,
            content_type=mime_type,
        )
        return key, url

    async def verify_uploaded_object(
        self,
        *,
        object_key: str,
        expected_size: int,
        expected_mime_type: str,
    ) -> dict[str, Any]:
        """读取并校验直传 Object 的大小与 MIME 元数据。

        【防伪校验】：
        1. expected_size 和 expected_mime_type 来自后端保存的 UploadSession；
        2. 实际值来自 COS head_object，而不是再次信任 complete 请求体；
        3. 校验失败时不进入 finalize，也不会创建 Document。
        """
        metadata = await self._cos.head_object(object_key)
        # COS SDK 返回的 Content-Length 通常是字符串，统一转换为整数参与比较。
        actual_size = int(metadata.get("Content-Length", -1))
        # 某些服务端响应可能附带 charset，只比较媒体类型主体。
        actual_mime_type = str(metadata.get("Content-Type", "")).split(";", 1)[0]
        if actual_size != expected_size:
            raise ValueError(
                f"上传文件大小不匹配，预期 {expected_size}，实际 {actual_size}"
            )
        if actual_mime_type != expected_mime_type:
            raise ValueError(
                f"上传文件 MIME 不匹配，预期 {expected_mime_type}，实际 {actual_mime_type}"
            )
        return metadata

    async def hash_object(self, object_key: str) -> str:
        """委托 COS 客户端流式计算对象 SHA-256，供上传业务服务使用。"""
        return await self._cos.hash_object(object_key)

    async def download(self, object_key: str) -> bytes:
        """读取指定 object_key 的全量二进制字节流。

        底层通过工作线程池完整下载流式数据并转为内存 bytes，
        供解析引擎（如 Docling、文本提取器）直接在内存中做分析，无本地临时文件落盘开销。
        """
        return await self._cos.get_object(object_key)

    async def delete(self, object_key: str) -> None:
        """删除云端存储桶中的指定对象。

        【容错设计与分布式一致性说明】：
        1. 为什么“吞掉异常”只打 Warning 而不向外抛出（re-raise）？
           在真实的删除业务时序中，通常优先执行数据库事务删除（DocumentService 先把 DB 数据行软删或物理删除）。
           若后续调用云端 COS 删除因网络抖动偶发失败并抛出异常，会导致外层整个 HTTP 请求报错中断。
           前端用户就会产生“明明点了删除却提示操作失败，但刷新后数据库数据又没了”的严重错觉。
        2. 降级方案：捕获腾讯云客户端与服务端异常并记录 Warn 级别日志，便于后续通过日志或
           定时任务清理孤儿文件，防止阻塞主业务链路。
        """
        try:
            await self._cos.delete_object(object_key)
        except (CosClientError, CosServiceError) as exc:
            # 仅记录告警日志追踪现场，保证调用方流程继续平滑向下推进
            logger.warning(
                "cos delete failed: key=%s, err=%s",
                object_key,
                exc,
            )


# =============================================================================
# 4. FastAPI 依赖注入辅助工厂
# =============================================================================
def get_file_service() -> FileService:
    """获取 FileService 实例的工厂函数。

    【一句白话大白话】：
    不用在每个接口里手动 new 对象，写上 Depends(get_file_service)，FastAPI 会像 Spring 的 @Autowired 一样自动把造好的实例喂给你！

    【核心作用与机制解析】：
    1. 摒弃手动 new，拥抱控制反转（IoC / 依赖注入）：
       - 传统模式下在每个 Router/Controller 内部手动 `service = FileService()` 会导致高耦合；
       - 本工厂提供给 FastAPI 的 Depends 机制使用，让业务端只需声明“我需要它”，
         由框架在收到请求时自动调用此工厂完成实例化并灌入形参（等价于 Java Spring 的 @Autowired 容器装配）。

    2. 使用时机与调用示范：
       - 场景：在路由层（如文件上传、下载接口）或上层 Service（如 DocumentService）中需要访问存储时。
       - 典型用法：
           @router.post("/upload")
           async def upload(
               file: UploadFile,
               file_service: FileService = Depends(get_file_service)  # <-- FastAPI 自动调用本工厂注入
           ):
               ...

    3. 核心优势：
       - 解耦与统一维护：实例化参数与配置全部收敛在此函数内部，修改构造逻辑对上层业务无感知。
       - 单元测试友好（Mock 替换）：在单测中可利用 app.dependency_overrides[get_file_service]
         一键将真实云端存储替换为 Mock 假对象，无需实际联网产生计费与网络开销。
    """
    return FileService()