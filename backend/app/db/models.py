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