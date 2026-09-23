"""
RAG 知识库系统数据模型模块 (backend/app/db/models.py)

【模块核心职责】
本模块基于 SQLAlchemy 2.0 现代声明式映射（Mapped/mapped_column）规范与 PostgreSQL 原生扩展，
集中定义系统全部持久化实体与生命周期状态机，是数据库结构的【唯一真实来源】。
按业务域分为四组，共 8 张表：

一、文档接入域（第 1-3 期）
1. UploadSessionStatus: 预签名直传会话状态枚举。
2. UploadSession: 预签名上传会话表（upload_sessions），承载分片直传的会话级状态与过期控制。
3. DocumentStatus: 文档处理状态枚举，基于 (str, Enum) 实现，支持状态约束与零成本 JSON 序列化。
4. Document: 原始文档元数据表（documents），负责文件防重哈希、对象存储（COS）定位、状态追踪及审计时间戳。
5. DocumentChunk: 语义检索切片明细表（document_chunks），承载正文内容、pgvector 高维嵌入向量、
   层级面包屑与检索调试元数据（JSONB）。

二、对话问答域（第 4-5 期）
6. MessageRole: 消息角色枚举（user / assistant / system）。
7. Conversation: 会话主表（conversations），一轮多轮问答的容器与标题。
8. Message: 消息表（messages），存用户提问与模型回答；调试卷（查询路由、决策轨迹、校验结果、
   trace_id）统一收纳在其 metadata JSONB 列中。

三、可溯源域（第 6-9 期）
9. AnswerCitation: 答案引用溯源表（answer_citations），一条回答对应多条原文依据；
   冗余快照文档名与原文片段，使原文被删除后引用仍可展示。

四、评测域（第 10 期）
10. EvaluationRunStatus: 评测执行状态枚举（running / completed / failed）。
11. EvaluationRun: 评测执行表（evaluation_runs），一次跑一遍评测集，
    存进度、聚合指标与状态；单条 case 失败不改变整轮状态。
12. EvaluationItem: 评测明细表（evaluation_items），每条 case 的输入快照、实际输出、
    各项指标与 Bad Case 归因。

【架构设计特性】
- 向量对齐：通过 pgvector.sqlalchemy.Vector 严格对齐 settings.embedding_dim 配置维度。
- 级联清理：采用数据库层外键 ON DELETE CASCADE 与 ORM passive_deletes=True 配合，保障父子表清理的高吞吐。
- 属性避坑：规避 SQLAlchemy 保留字 Base.metadata，在 ORM 侧使用 extra_metadata 并精准映射至物理列 "metadata"。
- 输入快照：需要"跨时间对比"的数据一律复制而非引用（引用快照原文片段、评测快照题目与标准答案），
  避免上游数据变更后历史记录失去可解释性。
- 调试卷随行：查询路由 / 决策轨迹 / 校验结果 / trace_id 这批调试卷在 messages 与 evaluation_items
  各存一份，让实时对话与离线评测都能完整回看当时链路。
- 索引显式声明：DocumentChunk 的 GIN 与 HNSW 索引写在 __table_args__ 里（含 postgresql_using / ops）。
  【必须如此】：模型侧不声明时，Alembic 自动比对看不见这两类索引，
  每次 autogenerate 都会生成 drop_index 把它们误删。
"""

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

# 向量检索扩展：用于支持 pgvector 的 Vector 字段类型
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    # 布尔列类型：第 10 期评测模型用来存"是否拒答 / 是否 Bad Case"等开关位
    Boolean,
    # 生成列（GENERATED ALWAYS AS ... STORED）构造器：
    # 声明后 SQLAlchemy 会把它从 INSERT / UPDATE 语句中自动剔除，交给数据库维护
    Computed,
    DateTime,
    # 浮点列类型：第 10 期评测模型用来存 RAGAS 四项指标与自定义业务指标
    Float,
    ForeignKey,
    # 索引声明构造器：第 10 期补 DocumentChunk 的 __table_args__ 时用到，
    # 目的是让"表达式索引 / 特定索引方法（GIN、HNSW）"也能被 Alembic 的自动比对【看见】。
    Index,
    Integer,
    String,
    Text,
    func,
)
# PostgreSQL 方言类型：支持 JSONB 高效二进制存储及原生 UUID 映射
# TSVECTOR：全文检索向量列类型，对应 PostgreSQL 原生 tsvector
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID as PGUUID
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
    # 表级参数：显式声明两个【表达式 / 专用索引方法】的索引（第 10 期补）
    # --------------------------------------------------------------------------
    # 【为什么必须在这里声明】这两个索引在数据库迁移脚本里已经建好，但模型侧原先
    # 没有任何声明。于是 `alembic revision --autogenerate` 的自动比对【看不见它们】，
    # 每次都会生成两条 `op.drop_index(...)`——若照抄执行，会真的把全文检索索引
    # 与向量近邻索引删掉，检索能力当场失效且不报错。
    # 在这里声明后，模型与数据库两侧的"认知"一致，漂移消失，以后 autogenerate 不再误删。
    __table_args__ = (
        # 向量近邻检索索引：HNSW + 余弦距离算子类
        Index(
            "ix_document_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        # 中文全文检索索引：GIN（配合 content_tsv 生成列）
        Index(
            "ix_document_chunks_content_tsv",
            "content_tsv",
            postgresql_using="gin",
        ),
    )

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
    # 中文全文检索向量列（第 6 期新增）
    # --------------------------------------------------------------------------
    # content_tsv: 存放 content 文本中文分词后的 tsvector 检索向量
    #
    # 1. 为什么用 Computed(..., persisted=True)？
    #    - 它是 SQLAlchemy 对应 PostgreSQL "GENERATED ALWAYS AS ... STORED"（生成列）的声明。
    #    - 【核心作用】：向 ORM 明确声明“该列完全由数据库自动算、自动存，代码切勿碰它”。
    #    - 【代码零改动】：有了它，SQLAlchemy 生成 INSERT / UPDATE 语句时会自动忽略本列，
    #      既不会把原有的 chunk_repo.bulk_add 破坏，也避免了向生成列写入 NULL 导致数据库报错。
    #
    # 2. 为什么显式指定 TSVECTOR 类型？
    #    - Python 注解写的是 Mapped[Any]，SQLAlchemy 无法据此自动反推出底层的数据库类型。
    #    - 显式写明 TSVECTOR，才能与 PG 原生的 tsvector 字段类型精准对齐，
    #      避免 Alembic 数据库迁移工具误报“模型与数据库类型不一致”。
    #
    # 3. 表达式必须与数据库迁移脚本（Alembic Migration）保持完全一致：
    #    - 分词配置必须同为 'chinese_zh'，否则 `alembic check` 会判定存在模型漂移。
    # --------------------------------------------------------------------------
    content_tsv: Mapped[Any] = mapped_column(
        # 显式指定 PostgreSQL 原生全文检索向量类型
        TSVECTOR,
        # 生成规则：由数据库调用 chinese_zh 分词器自动生成，并物理持久化存储到磁盘
        Computed("to_tsvector('chinese_zh', content)", persisted=True),
        nullable=False,
        comment="中文全文检索向量（数据库自动维护）"
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
    # 检索调试元数据（第 6 期新增）
    # --------------------------------------------------------------------------
    # 作用：记录当前切片（Chunk）被召回的全过程参数，用于线上排查
    #      （例如分析：某片段到底凭什么排到前列？是被语义模型相中，还是纯粹撞上了关键词？）
    #
    # 字段结构 (以 JSON 存储在 PostgreSQL JSONB 列中)
    # 1. 命中来源：
    #    - sources: list[str]，可能的值：["vector"]、["keyword"] 或 ["vector", "keyword"]
    # 2. 向量语义路 (Dense Retrieval)
    #    - vector_rank  : 向量路排名（从 1 开始）；未命中该路则为 None
    #    - vector_score : 余弦相似度 (Cosine Sim，范围通常在 0~1，越高语义越贴近)；未命中为 None
    # 3. 关键词全文路 (Sparse Retrieval)
    #    - keyword_rank : 关键词路排名（从 1 开始）；未命中该路则为 None
    #    - keyword_score: PG 原生全文检索匹配分 (ts_rank，代表词频命中密度)；未命中为 None
    # 4. 综合仲裁 (RRF Fusion)
    #    - rrf_score    : 双路排名通过 RRF 融合公式计算出的最终得分，决定最终输出顺序
    # --------------------------------------------------------------------------
    retrieval_meta: Mapped[dict | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="检索调试元数据（召回来源、单路排名与分数、RRF融合分）"
    )

    # --------------------------------------------------------------------------
    # 关系映射
    # --------------------------------------------------------------------------
    # 多对一关联回所属消息
    message: Mapped[Message] = relationship(
        back_populates="citations"
    )


# ==============================================================================
# 第 10 期：评测与 Bad Case 分析（两张表对应「一次执行」与「一次执行下的每条 case」）
# ==============================================================================
class EvaluationRunStatus(str, Enum):
    """评测 run 生命周期：BackgroundTasks 跑完前 RUNNING；正常结束 COMPLETED；
    主流程异常（不是单条 case 异常）置 FAILED 并写 error_message。

    【状态粒度为什么只到 run 级】
    单条 case 失败不影响整轮 —— 它的错误写在 evaluation_items.error_message 里，
    整轮照常跑完并 COMPLETED。只有【主流程本身】崩了（数据库连不上、
    评测集加载不了等）才算整轮失败。
    """

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class EvaluationRun(Base):
    """一次评测执行（跑一遍评测集）。

    【模型职责】：
    - 记录一次完整评测作业的生命周期状态、执行配置与进度追踪；
    - 聚合回填全量 Case 跑完后的 RAGAS 四项指标与自研业务评测指标；
    - 统计平均端到端延迟与首 Token 延迟，为 RAG 架构调优提供基准对比数据；
    - 级联管理属于当前 Run 的所有 EvaluationItem 实体。

    【与 EvaluationItem 的关系】：
    一对多强从属关系。EvaluationRun 存储“这一轮执行的宏观上下文”——进度流水、
    Run 级别聚合统计值与主流程健康度；每条 Case 的明细快照、中间链路步骤与单点归因下沉在 EvaluationItem 中。
    """

    __tablename__ = "evaluation_runs"

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(PGUUID, primary_key=True, default=uuid4)
    #   业务说明：单次评测任务（Run）的全局唯一标识，前端路由定位与指标看板汇总的物理主键
    #   类型定义 (PGUUID(as_uuid=True))：使用 PostgreSQL 原生 UUID 数据类型，Python 侧绑定原生 uuid.UUID 对象
    #   约束属性：primary_key=True 设置为物理主键，default=uuid4 在 Python 进程侧生成默认 UUIDv4
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(String(256), nullable=False)
    #   业务说明：用户创建评测任务时填写的友好语义名称（如 "v2.1-hybrid-search-baseline"），便于历史回溯与橫向对比
    #   约束属性：nullable=False 确保评测标识非空，String(256) 长度能够容纳包含版本号、模型名及变更说明的长命名
    name: Mapped[str] = mapped_column(String(256), nullable=False)

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(String(128), nullable=False)
    #   业务说明：本轮评测所绑定的评测集文件名（不含 .jsonl 后缀），用于定位数据源及标识评测基线类型
    #   约束属性：nullable=False，锁定该 Run 与评测集文件的强从属契约，列宽 128 字符适配标准命名规范
    dataset_name: Mapped[str] = mapped_column(String(128), nullable=False)

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(Integer, nullable=False)
    #   业务说明：创建 Run 时从评测集静态固化的总 Case 数，作为当前轮次的基准容量，避免因评测集文件动态变动导致进度分母漂移
    #   约束属性：nullable=False，配合 progress_* 字段为前端提供精确计算执行进度百分比的静态依据
    dataset_size: Mapped[int] = mapped_column(Integer, nullable=False)

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(String(16), nullable=False)
    #   业务说明：标记当前评测 Run 的生命周期状态（RUNNING / COMPLETED / FAILED）
    #   设计权衡：采用 String(16) 而非 PostgreSQL 物理 ENUM 类型，防止后续新增暂停、排队等中间状态时触发数据库 DDL 锁表
    status: Mapped[EvaluationRunStatus] = mapped_column(
        String(16), nullable=False
    )

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(Integer, nullable=False, default=0)
    #   业务说明：后台任务派发的计划执行总 Case 数（通常等同于 dataset_size），提供进度统计的计数器上界
    #   约束属性：nullable=False，default=0 保证初始化状态下数值完备，避免聚合或计算时产生空指针异常
    progress_total: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(Integer, nullable=False, default=0)
    #   业务说明：后台任务已顺利执行完毕（包含判定为 Bad Case 但 RAG 流程正常走完）的 Case 累计计数器
    #   业务联动：由 Worker 每跑完一条原子累加，前端轮询通过 (progress_completed + progress_failed) / progress_total 驱动进度条
    progress_completed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(Integer, nullable=False, default=0)
    #   业务说明：单条 Case 执行期间抛出未捕获异常、调用下游大模型接口硬性超时崩溃的失败用例累计计数器
    #   约束属性：nullable=False，default=0，用于监控任务异常率，若失败占比过高可供运维策略触发告警
    progress_failed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(Float, nullable=True)
    #   业务说明：RAGAS 指标 - 忠实度（Faithfulness）均值，评估生成内容是否完全推导自检索上下文，用于量化模型“幻觉”程度
    #   类型定义 (Float, nullable=True)：单精度浮点数，Run 未跑完或聚合前保持 NULL；取值区间通常为 [0.0, 1.0]
    faithfulness: Mapped[float | None] = mapped_column(Float, nullable=True)

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(Float, nullable=True)
    #   业务说明：RAGAS 指标 - 答案相关度（Answer Relevancy）均值，评估回答是否切题、是否存在答非所问或废话连篇
    #   设计约束：可为空，全量执行完成后由 Worker 离线计算各子项算术平均值并一次性回填落库
    answer_relevancy: Mapped[float | None] = mapped_column(Float, nullable=True)

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(Float, nullable=True)
    #   业务说明：RAGAS 指标 - 上下文精确率（Context Precision）均值，评估检索回来的 Chunks 中相关知识是否排在最前列
    #   业务价值：衡量 Rerank 重排能力的核心指标，得分低通常意味着需要微调或优化重排模型与相关性阈值
    context_precision: Mapped[float | None] = mapped_column(Float, nullable=True)

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(Float, nullable=True)
    #   业务说明：RAGAS 指标 - 上下文召回率（Context Recall）均值，评估标准答案所需的事实点被检索切片覆盖的比例
    #   业务价值：衡量检索向量化与切片分块（Chunking）策略的关键指标，低召回通常意味着知识库存在切片撕裂或召回通道不足
    context_recall: Mapped[float | None] = mapped_column(Float, nullable=True)

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(Float, nullable=True)
    #   业务说明：自研业务指标 - 引用命中率（Citation Hit Rate），衡量实际角标引用文档与期望来源的交集比率
    #   计算口径：仅计算正常作答 case（should_refuse=False），拒答 case 自动排除在分母之外
    citation_hit_rate: Mapped[float | None] = mapped_column(Float, nullable=True)

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(Float, nullable=True)
    #   业务说明：自研业务指标 - 拒答准确率（Refusal Accuracy），衡量系统对于越界、无答案、违规场景该拒答时是否坚决拒答
    #   设计约束：当且仅当评测集中存在 should_refuse 样本时生效，取值区间 [0.0, 1.0]，为空代表无拒答样本或未跑完
    refusal_accuracy: Mapped[float | None] = mapped_column(Float, nullable=True)

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(Float, nullable=True)
    #   业务说明：整轮评测中所有 Case 的端到端执行耗时平均值（毫秒 ms），反映整体 RAG 链路（检索+生成）的吞吐水平
    #   约束属性：nullable=True，评测完成后聚合落库，辅助排查模型降级或网络波动对系统整体性能的影响
    avg_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(Float, nullable=True)
    #   业务说明：整轮评测首 Token 延迟平均值（毫秒 ms），反映用户感知维度的首字等待体验
    #   计算口径：仅统计正常流式输出用例，拒答拦截 Case 不计入；该值暴涨通常预示 Embedding、Milvus 向量检索或 Rerank 链路存在性能瓶颈
    avg_first_token_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(Text, nullable=True)
    #   业务说明：评测 Run 主流程发生不可恢复硬中断时的根因异常栈信息（如评测集 JSONL 解析崩溃、数据库连接池枯竭）
    #   设计权衡：仅记录影响整轮 Run 的致命错误，单条 Case 的异常下沉至 EvaluationItem.error_message 记录
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(DateTime(timezone=True), nullable=True)
    #   业务说明：评测后台任务脱离调度队列、正式开始消费第一条 Case 的物理时间戳
    #   类型定义 (timezone=True)：带有时区信息的 UTC 时间戳，用于精确计算整个任务的周转耗时（Turnaround Time）
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(DateTime(timezone=True), nullable=True)
    #   业务说明：评测任务最终进入终态（COMPLETED 或 FAILED）的时间戳，未结束前保持 NULL
    #   约束属性：nullable=True，配合 started_at 可用于评估整批用例离线评测任务的实际并发效率
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(DateTime(timezone=True), server_default=func.now())
    #   业务说明：记录评测任务在数据库初始创建的时间戳，用于列表按时间倒序排列与任务审计
    #   默认行为 (server_default=func.now())：交由数据库引擎通过当前时间戳生成，防止服务实例时钟不一致
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # 语法（SQLAlchemy 2.0 关系映射）：relationship(...)
    #   业务说明：与 EvaluationItem 构成 1:N 级联关联，支持从 Run 便捷导航获取全部子用例
    #   级联设计：cascade="all, delete-orphan" 在 ORM 侧维护孤儿删除，passive_deletes=True 配合数据库 ondelete="CASCADE" 避免内存全量加载开销
    items: Mapped[list["EvaluationItem"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class EvaluationItem(Base):
    """单条 case 的输入快照 + 实际输出 + 指标 + Bad Case 归因。

    【模型职责】：
    - 固化评测集当次执行的输入数据镜像（快照解耦，杜绝外部数据集变更导致历史基线失真）；
    - 完整落盘 RAG 单次请求执行的真实输出、中间决策路由及性能指标；
    - 记录 RAGAS 及自研指标的单 Case 打分结果；
    - 承载 Bad Case 规则自动初判结果以及研发/标注人员的人工审查修正与标注笔记。

    【快照设计原则】：
    所有 expected_* 与 question 字段从评测集 JSONL 解析并直接冗余落表，不与外部评测集文件建立动态外键关连，
    确保每一次评测历史都是闭环且完全可重现的静态沙盒。
    """

    __tablename__ = "evaluation_items"

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(PGUUID, primary_key=True, default=uuid4)
    #   业务说明：单条评测明细记录的全局唯一身份证，作为前端 Bad Case 标注详情页的寻址主键
    #   类型定义 (PGUUID(as_uuid=True))：使用 PostgreSQL 原生 UUID 数据类型，Python 侧绑定原生 uuid.UUID 对象
    #   约束属性：primary_key=True，default=uuid4 在实体初始化时由 Python 端生成
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(PGUUID, ForeignKey(...), nullable=False, index=True)
    #   业务说明：关联所属评测执行任务（EvaluationRun）的主键 ID
    #   约束属性：nullable=False 拒绝孤儿记录；index=True 建立 B-Tree 索引，确保按 Run 检索全量 Case 或进行批量统计时的高性能
    #   外键行为 (ondelete="CASCADE")：数据库级级联删除，父 Run 记录抹除时自动清理下游关联的全部 Item
    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("evaluation_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # --- 输入快照（从评测集 jsonl 复制，见类文档说明）---

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(String(64), nullable=False)
    #   业务说明：原评测集中标识单条测试用例的业务编号（如 "QA-LAW-0023"），便于在评测集迭代版本间比对同一 Case 的表现演进
    #   约束属性：nullable=False，列宽 64 字符容纳业务语义编码
    case_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(Text, nullable=False)
    #   业务说明：送入 RAG 问答链路的原始用户提问 Prompt 文本快照
    #   类型定义 (Text)：无固定长度限制的文本字段，适配多轮对话拼接、包含大段上下文前置的超长问题
    question: Mapped[str] = mapped_column(Text, nullable=False)

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(Text, nullable=False)
    #   业务说明：评测集预设的标准参考答案（Ground Truth），用于作为 RAGAS 评估指标（如 Context Recall）与人工判定的基准
    #   约束属性：nullable=False，即使是拒答 Case 也需提供标准说明（例如“对不起，根据已有资料无法回答该问题”）
    expected_answer: Mapped[str] = mapped_column(Text, nullable=False)

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(JSONB, nullable=False, default=list)
    #   业务说明：预期回答该问题必须命中的核心文档名称列表，用于计算 Citation Hit Rate 与检索召回精准度
    #   类型定义 (JSONB)：PostgreSQL 原生 JSONB 类型，支持包含数组操作符（如 `@>`、`?|`）的原生高效查询过滤
    #   默认行为 (default=list)：新建实体默认为空数组
    expected_document_names: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list
    )

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(JSONB, nullable=False, default=list)
    #   业务说明：预期答案中必须覆盖的关键事实实体、专业术语或关键命题短语列表，供规则匹配引擎作初筛验证
    #   类型定义 (JSONB)：采用 JSONB 数组格式存储字符串列表
    expected_keywords: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list
    )

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(Boolean, nullable=False)
    #   业务说明：标注该 Case 是否属于“越界/无依据/敏感违规”因而必须触发系统拒答拦截的真值预期
    #   业务联动：作为 refusal_correct 准确率判定的核心条件基准
    should_refuse: Mapped[bool] = mapped_column(Boolean, nullable=False)

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(JSONB, nullable=False, default=list)
    #   业务说明：测试用例的维度属性标签集合（如 ["长文本", "多跳推理", "跨表格"]），用于评测大盘按标签做细分维度下钻分析
    #   类型定义 (JSONB)：方便后续基于标签进行灵活的切片多维过滤与报表聚类
    tags: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    # --- 实际输出（跑完 RAG 链路后回填）---

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(Text, nullable=False, default="")
    #   业务说明：RAG 系统针对该问题端到端生成的最终实际回答文本（包含流式组装完毕后的全量内容）
    #   约束属性：nullable=False，default="" 保证无响应或异常时非空，避免展示层抛出 NullReference
    actual_answer: Mapped[str] = mapped_column(
        Text, nullable=False, default=""
    )

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(Boolean, nullable=False, default=False)
    #   业务说明：记录实际运行中系统是否判定并执行了“主动拒答”（如触发低置信度防御机制或安全策略）
    #   约束属性：nullable=False，default=False，配合 should_refuse 计算系统在负样本上的防御召回水平
    actual_refused: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(JSONB, nullable=False, default=list)
    #   业务说明：生成答案中实际携带的角标引用来源明细列表（如 `[{"index": 1, "doc_name": "xxx.pdf", "chunk_id": "..."}]`）
    #   业务价值：与 expected_document_names 进行比对，作为自动计算 citation_hit 的物理依据
    citations: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list
    )

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(JSONB, nullable=False, default=list)
    #   业务说明：检索及重排阶段召回并真正喂给大模型上下文窗口的 Top-K 文档切片元数据快照（包含 chunk 内容摘要、得分、源文件信息）
    #   设计权衡：全量固化切片元数据，便于在排查 Bad Case 时直接还原上下文现场，无需反查切片底表
    retrieved_chunks_meta: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list
    )

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(JSONB, nullable=True)
    #   业务说明：意图识别与路由决策节点的结构化输出快照（如判断为直接回答、向量检索、混合检索、或走结构化 SQL 路由）
    #   数据来源：与线上问答链路中的 MessageRead.query_route 保持统一的数据结构与协议标准
    query_route: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(JSONB, nullable=True)
    #   业务说明：若走多跳 Agent 规划链路，记录每一步思考、动作工具调用与观察结果的轨迹明细（Agent Trajectory）
    #   类型定义 (JSONB, nullable=True)：动态保存步骤列表，仅在启用 Agent 模式的 RAG 链路上存在有效值
    agent_steps: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(JSONB, nullable=True)
    #   业务说明：事实自校验与一致性检查节点（Fact-Checker/Critic）的执行结果快照，包含自检置信度分值与警告提示
    #   类型定义 (JSONB, nullable=True)：可为空，未挂载验证器时置空
    verify_result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(String(64), nullable=True)
    #   业务说明：链路追踪系统的全局 Trace ID（如 LangSmith / Langfuse / OpenTelemetry Trace ID）
    #   业务价值：建立离线评测与链路可视化平台的直接穿透跳链，研发可通过该 ID 一键定位底层每个 LLM 调用的 Token 消耗与原始 IO
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(Integer, nullable=False, default=0)
    #   业务说明：该条用例走完检索、重排、Prompt 组装、大模型生成全过程的端到端耗时（毫秒 ms）
    #   约束属性：nullable=False，default=0，用于定位尾部高延迟 Case 进行性能专项攻坚
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(Integer, nullable=True)
    #   业务说明：流式输出场景下，从请求发出到收到大模型返回第一个有效 Token 的耗时（毫秒 ms）
    #   设计约束：可为空；若整条链路发生异常或非流式输出时为 NULL，其数值直接衡量了检索+重排阶段的延迟开销
    first_token_latency_ms: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(Text, nullable=True)
    #   业务说明：单条 Case 在执行或打分期间捕获的错误堆栈信息（如 LLM 网关超时、上下文超长截断异常）
    #   设计权衡：单条 Case 报错仅记录在此字段中，不阻断整轮 Run 的推进，实现单点错误物理隔离
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- 指标评估 ---

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(Float, nullable=True)
    #   业务说明：单条 Case 的 RAGAS 忠实度（Faithfulness）得分，判定答案是否完全来自于检出的上下文片段
    #   异常处理：打分模型调用超时或报错时保持为 None，前端按缺失友好呈现（如显示短横线 `-`）
    faithfulness: Mapped[float | None] = mapped_column(Float, nullable=True)

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(Float, nullable=True)
    #   业务说明：单条 Case 的 RAGAS 答案相关度（Answer Relevancy）得分，判定输出内容针对原问题的解答匹配程度
    #   设计约束：可为空，取值范围通常在 [0.0, 1.0] 区间
    answer_relevancy: Mapped[float | None] = mapped_column(Float, nullable=True)

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(Float, nullable=True)
    #   业务说明：单条 Case 的 RAGAS 上下文精确率（Context Precision）得分，评估命中真正关键证据的切片是否排在检索前列
    #   设计约束：可为空，用于分析重排算法是否存在劣质切片挤占有效切片窗口问题
    context_precision: Mapped[float | None] = mapped_column(Float, nullable=True)

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(Float, nullable=True)
    #   业务说明：单条 Case 的 RAGAS 上下文召回率（Context Recall）得分，衡量检索切片对参考答案知识点的覆盖程度
    #   设计约束：可为空，低分通常直接导向切片丢失、Embedding 语义断层等 Bad Case 归因
    context_recall: Mapped[float | None] = mapped_column(Float, nullable=True)

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(Boolean, nullable=True)
    #   业务说明：自研指标 - 角标命中判定布尔值，标识 actual citations 是否准确命中了 expected_document_names
    #   边界处理：当 should_refuse=True 时固定为 NULL，因为拒答用例不应产生引用，严禁拉低或计入正常引用命中率的分母
    citation_hit: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(Boolean, nullable=False, default=False)
    #   业务说明：自研指标 - 拒答一致性布尔判定，判定公式为 `actual_refused == should_refuse`
    #   业务价值：不仅考核该拒答的是否拒答，也考核不该拒答的是否被误杀（False Refusal）
    refusal_correct: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    # --- Bad Case 归因 ---

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(Boolean, nullable=False, default=False)
    #   业务说明：核心筛选标记，标识当前 Case 是否被认定为问题用例（Bad Case）
    #   生命周期：初始由规则引擎自动初判（如 RAGAS 指标低于阈值或拒答错误），研发与质检人员后续可通过 PATCH 接口人工覆写修正
    is_bad_case: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(String(64), nullable=True)
    #   业务说明：Bad Case 的细分技术归因分类字面量（如 RETRIEVAL_MISSING、RERANK_DEMOTED、HALLUCINATION、REFUSAL_LEAK 等 13 种规范枚举）
    #   约束属性：可为空（仅在 is_bad_case=True 时有实际业务意义），与 API Schemas 及前端呈现规范保持强契约对齐
    bad_case_category: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(Text, nullable=True)
    #   业务说明：研发、Prompt 算法工程师针对该 Bad Case 填写的自由排查笔记、根因定性与后续优化方向规划
    #   类型定义 (Text, nullable=True)：大文本类型，容纳深度的复盘论证与上下文推演记录
    bad_case_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 语法（SQLAlchemy 2.0 声明式列映射）：mapped_column(DateTime(timezone=True), server_default=func.now())
    #   业务说明：记录评测条目落库的初始物理时间戳
    #   默认行为 (server_default=func.now())：由数据库端生成带有时区信息的当前时间
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # 语法（SQLAlchemy 2.0 关系映射）：relationship(...)
    #   业务说明：反向关联父级 EvaluationRun 实体，便于从单条 Case 快速反查其执行批次的整体上下文
    run: Mapped[EvaluationRun] = relationship(back_populates="items")