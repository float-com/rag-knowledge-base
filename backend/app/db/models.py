"""
RAG 知识库系统数据模型模块 (backend/app/db/models.py)

【模块核心职责】
本模块基于 SQLAlchemy 2.0 现代声明式映射（Mapped/mapped_column）规范与 PostgreSQL 原生扩展，
定义系统核心持久化数据模型及生命周期状态机：
1. DocumentStatus: 基于 (str, Enum) 实现的文档处理状态枚举，支持状态约束与零成本 JSON 序列化。
2. Document: 原始文档元数据实体表（documents），负责文件防重哈希、对象存储（COS）定位、状态追踪及审计时间戳。
3. DocumentChunk: 语义检索切片明细表（document_chunks），承载正文内容、pgvector 高维嵌入向量、层级面包屑与扩展 JSONB 元数据。

【架构设计特性】
- 向量对齐：通过 pgvector.sqlalchemy.Vector 严格对齐 settings.embedding_dim 配置维度。
- 级联清理：采用数据库层外键 ON DELETE CASCADE 与 ORM passive_deletes=True 配合，保障父子表清理的高吞吐。
- 属性避坑：规避 SQLAlchemy 保留字 Base.metadata，在 ORM 侧使用 extra_metadata 并精准映射至物理列 "metadata"。
"""

from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

# 向量检索扩展：用于支持 pgvector 的 Vector 字段类型
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
# PostgreSQL 方言类型：支持 JSONB 高效二进制存储及原生 UUID 映射
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
# SQLAlchemy 2.0 现代声明式语法核心：
# Mapped 用于 Python 侧类型注解（提供 IDE 智能提示与静态类型检查）
# mapped_column 用于底层数据库字段定义（生成 DDL 约束与列类型）
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import settings
from app.db.base import Base


# ==============================================================================
# 1. 业务状态枚举定义
# ==============================================================================
class DocumentStatus(str, Enum):
    """
    文档生命周期状态枚举类

    【核心继承机制说明】
    1. 继承 str:
       使枚举项具备原生 Python 字符串属性。
       - 支持直接与字符串做值对比（如 status == "ready"），省去手动 .value 解包；
       - 与 FastAPI / Pydantic 及 json.dumps 无缝兼容，序列化时直接输出字面量。
    2. 继承 Enum:
       收敛合法的状态集合，防止业务逻辑中出现散落的“魔法字符串”或拼写手误。
    """

    # 初始阶段：文件已写入腾讯云 COS，元数据待完成数据库入库
    UPLOADING = "uploading"

    # 解析阶段：Docling 解析引擎正在提取文档中的纯文本与结构化版面
    PARSING = "parsing"

    # 切分向量化阶段：文本分块（Chunking）、调用 Embedding 模型计算向量并写入 chunks 表
    INDEXING = "indexing"

    # 就绪终态：向量数据已成功持久化至 pgvector，可被语义检索与 RAG 召回
    READY = "ready"

    # 失败终态：在上传、解析、切分或向量化任意环节发生未捕获异常
    FAILED = "failed"


class UploadSessionStatus(str, Enum):
    """浏览器直传会话生命周期状态。

    【状态说明】：
    - INITIATED：后端已签发 URL，等待浏览器 PUT；
    - UPLOADED：预留状态，表示后续可在需要时记录 PUT 完成事件；
    - FINALIZING：complete 已通过 COS 校验，后台正在哈希和建档；
    - COMPLETED：后台 finalize 已完成；
    - EXPIRED：超过预签名会话有效期；
    - FAILED：后台 finalize 发生异常；
    - ABORTED：用户主动取消或清理任务回收。
    """

    INITIATED = "initiated"
    UPLOADED = "uploaded"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    EXPIRED = "expired"
    FAILED = "failed"
    ABORTED = "aborted"


class UploadSession(Base):
    """记录 COS 预签名直传过程中的临时状态与文件元数据。

    【模型职责】：
    - 保存 init 阶段由后端确认的文件元数据；
    - 保存一次性 Object Key 与 URL 有效期；
    - 为 complete、后台 finalize、失败重试和过期清理提供状态依据。

    【与 Document 的区别】：
    UploadSession 是上传过程记录，Document 是上传成功且完成哈希确认后的业务实体。
    只有 finalize 完成后才创建 Document，因此不会向 documents.file_hash 写入临时占位值。
    """

    __tablename__ = "upload_sessions"

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(PGUUID, primary_key=True, default=uuid4)
    #   业务说明：每个直传会话的全局唯一身份证，同时作为临时 COS 路径的隔离命名空间
    #   类型定义 (PGUUID(as_uuid=True))：使用 PostgreSQL 原生 UUID 数据类型，Python 侧绑定原生 uuid.UUID 对象
    #   约束属性：primary_key=True 设置为物理主键，default=uuid4 在 Python 进程侧生成默认 UUIDv4
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(String(512), nullable=False, unique=True)
    #   业务说明：COS 中的完整 Object Key 绝对路径，作为存储资源的物理引用锚点
    #   约束属性：nullable=False 拒绝空值；unique=True 建立数据库唯一约束，物理级阻断多会话碰撞覆写同一个临时对象
    object_key: Mapped[str] = mapped_column(
        String(512), nullable=False, unique=True
    )

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(String(512), nullable=False)
    #   业务说明：用户上传时的原始文件名（已由 Service 剥离目录路径），用于前端展示及后续落库创建 Document 实体
    #   约束属性：nullable=False 确保文件名必填，列宽 512 字符适配绝大多数长文件名场景
    original_name: Mapped[str] = mapped_column(String(512), nullable=False)

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(String(128), nullable=False)
    #   业务说明：服务端白名单校准后的规范 MIME 类型，作为向 COS 签发 PUT 凭证与 HEAD 校验的强契约标准
    #   约束属性：nullable=False，防范 Content-Type 为空的未定型多媒体资源进入系统
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(String(16), nullable=False)
    #   业务说明：规整后统一小写的文件扩展名（如 .pdf、.docx），供下游文档切片、离线审计与解析引擎策略路由使用
    #   约束属性：nullable=False，列宽 16 字符覆盖所有常见文件格式后缀
    suffix: Mapped[str] = mapped_column(String(16), nullable=False)

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(BigInteger, nullable=False)
    #   业务说明：init 阶段由客户端预报声明、complete 阶段由云存储 HEAD 元数据反查校验的文件字节上限（Byte）
    #   类型定义 (BigInteger)：使用 64 位大整数存储，支持大文件及超大模型权重文件的字节容量表达
    expected_size: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(JSONB, nullable=False, default=list)
    #   业务说明：直传生命周期内暂存的细粒度数据权限与可见性标签集合；待落库 Document 时按需向下沉淀
    #   类型定义 (JSONB)：采用 PostgreSQL 原生二进制 JSON 存储，支持后续基于 JSON 路径的高效 GIN 索引过滤
    #   默认行为 (default=list)：在 Python 实体实例化时默认初始化为空列表引用
    permission_tags: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list
    )

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(String(32), default=UploadSessionStatus.INITIATED)
    #   业务说明：标记当前直传会话的状态机节点（INITIATED / FINALIZING / COMPLETED / FAILED / ABORTED / EXPIRED）
    #   设计权衡：使用字符串列（String(32)）而非 PostgreSQL 物理 ENUM 类型，便于后续平滑扩展状态且无需变更数据库 Schema
    status: Mapped[UploadSessionStatus] = mapped_column(
        String(32), nullable=False, default=UploadSessionStatus.INITIATED
    )

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(DateTime(timezone=True), nullable=False)
    #   业务说明：会话操作截止时间戳（如签发后 5 分钟超时），用于定时任务清理孤儿文件及直传完工前置时效熔断
    #   类型定义 (timezone=True)：启用带时区感知的 TIMESTAMPTZ 类型，统一以 UTC 基准时间落库
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(DateTime(timezone=True), server_default=func.now())
    #   业务说明：记录上传会话初始发起的创建时间戳，用于审计溯源与倒序查询
    #   默认行为 (server_default=func.now())：由数据库端执行 NOW() 函数生成时间，避免应用层服务器时间漂移
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(DateTime(timezone=True), nullable=True)
    #   业务说明：直传及建档（或去重命中）彻底完工的终态时间戳，未完工前保持空值
    #   类型定义 (nullable=True)：字段可为空，用于量化分析上传全流程的耗时性能指标
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(Text, nullable=True)
    #   业务说明：后台 finalize 任务或校验中断时捕获记录的错误摘要信息（截断上限通常为 2000 字符）
    #   类型定义 (Text, nullable=True)：无固定长度上限的文本列，仅在会话遭遇 FAILED 时落库填入
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


# ==============================================================================
# 2. Document 数据表 ORM 模型定义
# ==============================================================================
class Document(Base):
    """
    文档主表（documents）ORM 实体映射

    【SQLAlchemy 2.0 核心语法骨架拆解】
    每个字段遵循统一模板公式：
        变量名: Mapped[Python侧类型] = mapped_column(数据库列类型, 约束条件, comment="注释")
    - Mapped[...]: 供 Python/IDE 识别的类型提示（如 IDE 可自动推导出 .strip()、.year 等方法）。
    - mapped_column(...): 映射至 PostgreSQL 的物理列定义（如 VARCHAR、NOT NULL、UNIQUE 等）。
    """

    # 框架钩子：指定数据库中的物理表名（对应 SQL: CREATE TABLE documents (...)）
    __tablename__ = "documents"

    # --------------------------------------------------------------------------
    # 基础标识与文本元数据
    # --------------------------------------------------------------------------
    # 主键 ID：
    # - PGUUID(as_uuid=True):
    #     * PostgreSQL 特有方言类型。
    #     * 参数 [as_uuid=True]: 决定 ORM 在读取数据库时，自动将底层的原生 UUID 二进制/字符串转换为 Python 标准库的 uuid.UUID 对象，而不是普通的 str。
    # - primary_key=True: 声明该列为数据库主键约束（PRIMARY KEY）。
    # - default=uuid4:
    #     * 【Python 客户端默认值】：注意传入的是函数引用 uuid4 而不是 uuid4()。
    #     * 当通过 Python 代码实例化 Document() 且未给 id 赋值时，SQLAlchemy 会在 Python 进程内执行该函数生成唯一标识。
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        comment="文档全局唯一UUID主键"
    )

    # 原始文件名（带扩展名）：
    # - String(512): 底层对应 PostgreSQL 的 VARCHAR(512)，限制最大存储字符数。
    # - nullable=False: 对应 SQL 的 NOT NULL 非空约束，禁止存入 NULL。
    name: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        comment="文档原始文件名称"
    )

    # 文件内容的 SHA-256 校验哈希（64 位十六进制字符串）：
    # - unique=True: 底层建立 UNIQUE 唯一约束，保证整张表中 hash 绝对唯一。
    # - index=True: 为该列显式建立 B-Tree 索引。
    # - 【核心业务作用】：用于 service 层的秒传和幂等去重校验。文件上传前先算 hash，若表中已存在则直接返回已有记录，避免同份文件重复处理入库。
    file_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        index=True,
        comment="文件内容的 SHA-256 哈希值，用于幂等去重"
    )

    # 文件 MIME 媒体类型（如 application/pdf、text/plain）：
    # - 记录文件的真实类型标头，供后端路由到对应的文本解析器（如 PDF 提取器或 Markdown 提取器）。
    mime_type: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="文件 MIME 媒体类型"
    )

    # 文件物理大小（单位：字节 Byte）：
    # - BigInteger: 对应 SQL 中的 BIGINT（8 字节整型）。普通 Integer 上限仅约 2.14GB，选用 BigInteger 可防止大文件溢出。
    size: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="文件大小（字节 Byte）"
    )

    # --------------------------------------------------------------------------
    # 对象存储定位凭证
    # --------------------------------------------------------------------------
    # 存储服务商标识：
    # - default="cos": Python 层默认设为腾讯云对象存储 "cos"。
    # - 【核心设计作用】：解耦具体的云厂商。未来若需平滑横向扩展支持阿里云 OSS 或 AWS S3 时，只需按此字段做工厂路由，无需推翻数据结构。
    storage_provider: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="cos",
        comment="底层对象存储提供商标识"
    )

    # 腾讯云 COS 存储桶名称（Bucket Name）：定位文件存储的逻辑容器
    cos_bucket: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="腾讯云 COS 存储桶名称"
    )

    # 文件在 COS 存储桶中的完整对象键路径（Object Key）：等同于云端虚拟文件系统的绝对路径
    cos_object_key: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        comment="对象在 COS 中的全路径 Key"
    )

    # 存储桶所在地域标识（如 ap-guangzhou、ap-beijing）：用于初始化 COS SDK Client 时的物理节点定位
    cos_region: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="COS 存储桶所属地域"
    )

    # --------------------------------------------------------------------------
    # 生命周期与审计信息
    # --------------------------------------------------------------------------
    # 文档生命周期处理状态：
    # - DocumentStatus 强类型约束：代码层面只能赋值为该枚举中定义的合法状态，彻底避免魔法字符串。
    # - 数据库层仍以 String(32) 原生字符串落库，默认值为 DocumentStatus.UPLOADING。
    status: Mapped[DocumentStatus] = mapped_column(
        String(32),
        nullable=False,
        default=DocumentStatus.UPLOADING,
        comment="文档生命周期处理状态"
    )

    # 错误追踪详情：
    # - Mapped[str | None]: Python 3.10+ 联合类型写法，等价于 Optional[str]，代表该字段既可为字符串也可为 None。
    # - Text: 对应 SQL 的 TEXT 变长文本类型（无最大长度限制）。
    # - nullable=True: 允许数据库该字段为 NULL（正常流程下为空，仅在任务失败抛异常时持久化报错堆栈）。
    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="解析/索引失败时的错误信息堆栈"
    )

    # 记录创建时间：
    # - DateTime(timezone=True): 带有时区偏移的时间戳（PostgreSQL 的 TIMESTAMPTZ 类型），消除服务器跨时区时间错乱。
    # - func.now(): SQLAlchemy 的 SQL 函数生成器，对应数据库内置的 NOW() 或 CURRENT_TIMESTAMP。
    # - server_default=func.now():
    #     * 【数据库服务端默认值】：此约束直接写入建表 DDL 的 DEFAULT 语句中。
    #     * 无论通过 Python 代码插入还是直接用外部 SQL 脚本插入，均由 PostgreSQL 数据库服务端直接生成时间。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="记录创建时间"
    )

    # 记录更新时间：
    # - onupdate=func.now():
    #     * 【ORM 层面更新钩子】：当对已存在的实例字段进行修改并调用 session.commit() 时，
    #       SQLAlchemy 会在下发的 UPDATE 语句中自动追加 `SET updated_at = NOW()`，实现自动审计更新。
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment="记录更新时间"
    )

    # --------------------------------------------------------------------------
    # 关系映射（导航指针）
    # --------------------------------------------------------------------------
    # 一对多关系：一个 Document 实体关联其拆分出的所有 DocumentChunk 切片
    # - 注意：documents 物理表中不存在 chunks 这个列，它纯粹是 ORM 在内存对象中提供的关联属性指针。
    # - back_populates="document":
    #     * 双向绑定参数。要求在从表 DocumentChunk 模型中，必须存在一个名为 document 的 relationship 属性，保证双方指针双向同步。
    # - cascade="all, delete-orphan":
    #     * 【ORM 级联操作策略】：
    #     * "all": 涵盖 save-update、merge、refresh 等全部常规操作级联；
    #     * "delete-orphan"（删除孤儿）: 一旦某个 DocumentChunk 从父对象的 chunks 列表中被 remove() 移出，ORM 会自动将其标记为待删除并同步从数据库抹除。
    # - passive_deletes=True:
    #     * 【性能优化开销削减】：
    #     * 默认情况下，删除一个 Document 时，SQLAlchemy 会先把它的所有 chunks 查到内存里，再逐条下发 DELETE 语句；
    #     * 设置 passive_deletes=True 后，SQLAlchemy 在 Python 层直接信任从表外键上的 `ON DELETE CASCADE` 约束，直接交由数据库引擎底层内联删除所有切片，大幅减少网络 I/O。
    chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True
    )


# ==============================================================================
# 3. DocumentChunk 数据表 ORM 模型定义
# ==============================================================================
class DocumentChunk(Base):
    """
    文档切片明细表（document_chunks）ORM 实体映射

    【核心定位】
    Chunk 是检索的最小单元，每条记录都带有自己的向量。
    """

    # 映射物理表名
    __tablename__ = "document_chunks"

    # --------------------------------------------------------------------------
    # 主键与外键关联
    # --------------------------------------------------------------------------
    # 切片唯一标识 ID：
    # - PGUUID(as_uuid=True):
    #     * 【命名避坑】：文件头部通过 `from ... import UUID as PGUUID` 引入，
    #       是为了避开与 Python 原生标准库的 `uuid.UUID` 类型同名冲突，区分“PostgreSQL 原生列类型”与“Python 数据类型”。
    #     * as_uuid=True 让 ORM 在读取/写入时自动在 Python 的 uuid.UUID 对象与数据库二进制间转换。
    # - primary_key=True: 声明为主键。
    # - default=uuid4: 内存新建实例时，由 Python 端自动调用 uuid4 生成全局唯一 ID。
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        comment="切片全局唯一UUID主键"
    )

    # 所属文档外键 ID：
    # - ForeignKey("documents.id", ondelete="CASCADE"):
    #     * "documents.id": 指向父表 documents 的 id 物理列。
    #     * ondelete="CASCADE": 【数据库级联删除】在 PostgreSQL 物理外键上添加 ON DELETE CASCADE 约束。
    #       当父文档记录被物理删除时，PostgreSQL 数据库引擎会在底层自动秒删关联的所有切片，无需后端逐条 DELETE。
    # - index=True: 外键列默认不带索引，显式建立 B-Tree 索引可极大加速 "根据 document_id 查该文档全部切片" 的聚合速度。
    document_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属文档ID（外键级联删除）"
    )

    # --------------------------------------------------------------------------
    # 切片文本与向量
    # --------------------------------------------------------------------------
    # 切片清洗后的原始文本正文：
    # - Text: 对应 SQL 中的变长文本 TEXT 类型，用于存放分块后的段落正文。
    # - nullable=False: 检索的核心就是正文，不允许为空。
    content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="切片纯文本正文内容"
    )

    # 核心嵌入向量：
    # - Vector(settings.embedding_dim):
    #     * 【避坑点 1】：使用 pgvector 插件提供的专属向量类型。
    #     * 维度直接从 settings.embedding_dim 动态读取（当前为 1024 维），与配置项及 Embedding 模型严格对齐。
    #     * 【固化特性】：在后续运行 Alembic 数据库迁移建表时，此维度会直接固化到数据库 DDL（如 vector(1024)）。
    #       后续若切换为其他维度模型，必须重建表或新建向量字段。
    # - Mapped[list[float]]: 在 Python 业务代码访问该字段时，直接呈现为纯浮点数列表（如 [0.012, -0.045, ...]）。
    embedding: Mapped[list[float]] = mapped_column(
        Vector(settings.embedding_dim),
        nullable=False,
        comment="文本嵌入向量（与配置维度对齐）"
    )

    # --------------------------------------------------------------------------
    # 溯源与结构元数据
    # --------------------------------------------------------------------------
    # 所在原始文档的物理页码：
    # - Mapped[int | None] / nullable=True: 允许为空。PDF 文档具有真实的物理页码，但纯文本（TXT/Markdown）无页码概念，因此存为 NULL。
    page_no: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="文档所在物理页码"
    )

    # 标题层级路径（面包屑导航）：
    # - String(1024): 存储层级路径（如 "第1章 > 1.2 架构设计 > 模块划分"），召回时供大模型或前端精确定位章节，无层级时允许为 NULL。
    section_path: Mapped[str | None] = mapped_column(
        String(1024),
        nullable=True,
        comment="标题层级面包屑路径"
    )

    # 切片在所属文档内的切分顺序序号：
    # - 用于长文本切分后的时序还原；
    # - 在 RAG 召回命中某一切片后，可根据 chunk_index - 1 和 chunk_index + 1 快速拼装前后上下文窗口。
    chunk_index: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="切片顺序序号"
    )

    # 切片文本哈希（md5(content)）：
    # - String(32): MD5 算出的 32 位十六进制摘要。
    # - index=True: 建立普通索引。
    # - 业务作用：第 12 章做文档增量更新与局部修改重切时的去重比对基准，快速识别哪些切片内容未变无需重新调用 API 算向量。
    chunk_hash: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        index=True,
        comment="切片文本MD5哈希"
    )

    # 扩展元数据：
    # - 【避坑点 2：属性命名冲突与别名映射】：
    #     * SQLAlchemy 的基类 Base 内部自带一个核心保留属性叫做 metadata（用于管理整套 ORM 元数据表结构）。
    #     * 如果我们在类里直接写 `metadata = mapped_column(...)` 会覆盖基类内部属性导致启动报错。
    #     * 【解决方案】：Python 实体属性命名为 `extra_metadata`，
    #       而在 mapped_column 第一个位置传入 `"metadata"` 字符串，明确告知数据库底层列名仍然叫 `metadata`。
    # - JSONB: PostgreSQL 原生二进制 JSON 存储格式，支持对内部 Key 建立 GIN 倒排索引及灵活扩展。
    # - default=dict: Python 端默认给一个空字典 {}，传入 dict 函数指针而非 dict() 执行结果。
    extra_metadata: Mapped[dict] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        comment="扩展元数据（JSONB存储）"
    )

    # --------------------------------------------------------------------------
    # 审计时间戳
    # --------------------------------------------------------------------------
    # 记录创建时间：
    # - DateTime(timezone=True): 带有时区信息的 PostgreSQL TIMESTAMPTZ 类型。
    # - server_default=func.now(): 写入建表 DDL 的 DEFAULT CURRENT_TIMESTAMP，切片入库时直接由数据库引擎赋默认时间。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="切片入库创建时间"
    )

    # --------------------------------------------------------------------------
    # ORM 关系双向绑定
    # --------------------------------------------------------------------------
    # 多对一关联回父表 Document：
    # - back_populates="chunks": 与父模型 Document.chunks 构成严格的双向镜像指针。
    #   在内存中只要通过 `chunk.document` 即可无感知直接访问其归属的父文档对象。
    document: Mapped[Document] = relationship(
        back_populates="chunks"
    )

# ==============================================================================
# 4. 会话角色与对话模型定义 (Conversations & Messages)
# ==============================================================================
class MessageRole(str, Enum):
    """
    会话消息发送方角色枚举类

    【核心继承机制说明】
    - 继承 str: 保证可以直接与字符串字面量对比，且在 JSON 序列化时保持干净的纯字符串。
    - 继承 Enum: 规范大模型对话上下文标准角色的边界，杜绝魔法字符串。
    """

    USER = "user"          # 真实用户提问
    ASSISTANT = "assistant"  # LLM 模型生成的回答
    SYSTEM = "system"        # 系统提示词 (Prompt / 预设规则)


class Conversation(Base):
    """
    会话主表（conversations）ORM 实体映射

    【模型定位】
    代表一次独立的问答对话通道（Session），聚合管理一连串有序的问答消息流。
    """

    __tablename__ = "conversations"

    # --------------------------------------------------------------------------
    # 基础标识与元数据
    # --------------------------------------------------------------------------
    # 会话唯一标识 ID：
    # - PGUUID(as_uuid=True): 声明为 PostgreSQL 原生 UUID 类型，并双向转换为 Python uuid.UUID。
    # - primary_key=True: 主键。
    # - default=uuid4: Python 侧生成默认 UUIDv4。
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        comment="会话全局唯一UUID主键"
    )

    # 会话标题：
    # - 默认缺省值为 "新对话"，后续可通过提问内容自动生成并更新总结标题。
    # - 注：预留 user_id 字段，后续引入多租户与用户体系时可平滑扩展。
    title: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        default="新对话",
        comment="会话展示标题"
    )

    # --------------------------------------------------------------------------
    # 审计时间戳
    # --------------------------------------------------------------------------
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="会话创建时间"
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment="会话最近更新时间"
    )

    # --------------------------------------------------------------------------
    # 关系映射
    # --------------------------------------------------------------------------
    # 一对多关联当前会话下的所有消息（Message）：
    # - cascade="all, delete-orphan": 会话删除时，旗下所有消息级联物理销毁。
    # - passive_deletes=True: 信任数据库底层的外键级联删除能力，减少应用层重复查表开销。
    # - order_by="Message.created_at": 默认在加载会话时按消息的时间正序排列，保证对话时序正确。
    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Message.created_at",
    )


class Message(Base):
    """
    会话明细消息表（messages）ORM 实体映射

    【模型定位】
    记录会话中的单条消息实体（包含用户提问、大模型流式生成结果或系统 Prompt）。
    """

    __tablename__ = "messages"

    # --------------------------------------------------------------------------
    # 主键与外键关联
    # --------------------------------------------------------------------------
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        comment="消息全局唯一UUID主键"
    )

    # 所属会话 ID（外键级联删除）：
    # - ForeignKey("conversations.id", ondelete="CASCADE"): 会话删除即物理清理所有历史消息。
    # - index=True: 建立普通索引，极大提升拉取特定会话历史窗口的性能。
    conversation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属会话ID（外键级联删除）"
    )

    # --------------------------------------------------------------------------
    # 消息内容与角色
    # --------------------------------------------------------------------------
    # 消息角色：映射 MessageRole 枚举，数据库层以 String(16) 落地
    role: Mapped[MessageRole] = mapped_column(
        String(16),
        nullable=False,
        comment="消息角色 (user / assistant / system)"
    )

    # 消息完整正文文本
    content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="消息正文内容"
    )

    # 扩展元数据：
    # - 【避坑规范】：规避 SQLAlchemy 的保留字 Base.metadata，ORM 侧属性使用 extra_metadata，
    #   物理列名精准映射回 "metadata"。
    # - 业务作用：灵活预留用于记录调用模型（model）、消耗 token 数、请求延迟（latency）等分析元数据。
    extra_metadata: Mapped[dict] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        comment="扩展元数据（记录模型、Token开销、耗时等）"
    )

    # --------------------------------------------------------------------------
    # 审计时间戳
    # --------------------------------------------------------------------------
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="消息创建时间"
    )

    # --------------------------------------------------------------------------
    # 关系映射
    # --------------------------------------------------------------------------
    # 多对一关联所属会话
    conversation: Mapped[Conversation] = relationship(
        back_populates="messages"
    )

    # 一对多关联当前消息产生的知识溯源引用记录（仅 assistant 角色的消息会持有）
    # - order_by="AnswerCitation.ordinal": 严格按注入大模型时的切片序号正序排布
    citations: Mapped[list["AnswerCitation"]] = relationship(
        back_populates="message",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="AnswerCitation.ordinal",
    )


# ==============================================================================
# 5. 知识溯源与引用快照模型 (Answer Citations)
# ==============================================================================
class AnswerCitation(Base):
    """
    知识引用明细表（answer_citations）ORM 实体映射

    【核心架构设计考量】
    记录 assistant 角色在回答时具体引用的 chunk 依据。
    - 【快照冗余机制】：冗余存储 document_name / page_no / quote 原文。因为原文 chunk 后续可能因文档重新索引或用户主动删除而被物理抹除，
      但历史会话中的引用卡片仍然必须能够完整展示当时的溯源快照。
    - 【软关联置空 (ON DELETE SET NULL)】：外键 document_id 与 chunk_id 必须声明为 nullable=True，
      一旦关联的文档或分块被删除，数据库仅把外键字段置空，保全本引用历史明细本身不被级联误删。
    """

    __tablename__ = "answer_citations"

    # --------------------------------------------------------------------------
    # 主键与消息强归属外键
    # --------------------------------------------------------------------------
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        comment="引用全局唯一UUID主键"
    )

    # 所属回答消息 ID（强外键绑定，级联删除）：
    # - 本条引用归属于哪一条 assistant 消息；若该消息被删，引用记录无独立保留意义，直接 CASCADE 级联清理。
    message_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属消息ID（级联删除）"
    )

    # --------------------------------------------------------------------------
    # 序号与软关联定位（可空）
    # --------------------------------------------------------------------------
    # 对应 prompt 中给 LLM 看到的【片段 N】编号（从 1 开始递增）：
    # - 核心业务作用：持久化该序号，前端才能将 LLM 生成文本中的角标 [N] 与底部的引用面板按序号精确绑定。
    ordinal: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="上下文注入序号(1..N)，对应大模型生成的[N]角标"
    )

    # 所属源文档 ID：
    # - 外键策略采用 ON DELETE SET NULL，原文档被用户删除时本列置空，历史引用仍安全存留。
    document_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="SET NULL"),
        nullable=True,
        comment="关联文档ID（原文档被删时置空）"
    )

    # 所属原始分块 Chunk ID：
    # - 外键策略采用 ON DELETE SET NULL，文档切片重构被删时本列置空。
    chunk_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("document_chunks.id", ondelete="SET NULL"),
        nullable=True,
        comment="关联切片ID（原切片被删时置空）"
    )

    # --------------------------------------------------------------------------
    # 历史快照冗余字段 (Snapshot Fields)
    # --------------------------------------------------------------------------
    # 文档名称快照（记录引用当时的文件名，避免文档重命名或被删后丢失可读标题）
    document_name: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        comment="文档名称快照"
    )

    # 页码快照（PDF 物理页码，纯文本或未分段文档允许为 NULL）
    page_no: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="当时所在物理页码快照"
    )

    # 引用原文片段快照：
    # - 记录大模型回答时命中的真实上下文正文切片，保障历史会话随时能直接展开查阅原汁原味的溯源证据
    quote: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="引用的原始文本片段快照"
    )

    # --------------------------------------------------------------------------
    # 关系映射
    # --------------------------------------------------------------------------
    # 多对一关联回所属消息
    message: Mapped[Message] = relationship(
        back_populates="citations"
    )