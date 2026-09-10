"""
【文档与切块模型定义：Pydantic Schemas - API 数据契约层 (DTO)】

1. 核心定位：
   统一管理文档主表（Document）与切块子表（DocumentChunk）对外暴露的数据传输对象，
   承担接口入参安检拦截、ORM 实体脱敏映射、阶梯式性能优化与响应序列化。

2. 关键设计与安全防线：
   - 强类型字面量约束：通过 DocumentStatusValue (Literal) 精确约束 5 态生命周期，
     驱动前端代码生成工具产出强类型联合守卫，彻底杜绝字符串拼写漂移；
   - 字段级脱敏隔离：通过 from_attributes=True 建立白名单输出通道，
     主文档彻底抹除 cos_bucket 等物理存储配置，切块层坚决屏蔽高维 Embedding 向量；
   - 阶梯式性能防爆：切块列表（DocumentChunkRead）强制执行 100 字符文本截断与省略号拼接，
     防范超长文本打爆网络带宽与前端 DOM；全量正文严格收敛至单切块详情（DocumentChunkDetail）；
   - 动态派生与指标聚合：利用 @classmethod 自定义转换工厂，在内存中安全派生 char_count；
     结合 DocumentChunkStats 交付切块总数与长度分布指标，直观透传分块质量。

通俗来讲：
这是整个文档与切片模块的“出海海关条例与数据规格说明书”——
它死死把控前端能看什么、不能看什么：翻页查切片时只放行前 100 字摘要，查详情才放行全文，
并且把底层的腾讯云路径、高维向量全扣留在后方，既保性能又防泄密。
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

##=============================================================================
# 阶段1：定义文档模型
##=============================================================================

# =============================================================================
# 1. 文档生命周期状态字面量联合类型
# =============================================================================
# 语法（类型注解字面量）：Literal["...", "..."]
#   特性：与数据库底层 app.db.models.DocumentStatus 严格保持值同步
#   核心收益：自动生成 OpenAPI Schema 时，前端代码生成工具（如 openapi-typescript / gen:api）
#            能够直接产出精准的 TypeScript 联合类型：
#            "uploading" | "parsing" | "indexing" | "ready" | "failed"
#            而不是宽泛弱类型的 string，在编译期实现前端与后端的强类型保护。
#   通俗来讲：不让前端随便传乱七八糟的字符串，死死卡住只能是这 5 种状态之一。
DocumentStatusValue = Literal[
    "uploading",
    "parsing",
    "indexing",
    "ready",
    "failed",
]


# =============================================================================
# 2. 单文档详情读取响应模型
# =============================================================================
class DocumentRead(BaseModel):
    """单文档完整详情响应模型。

    【脱敏设计】：
    - 仅输出业务查看所需的核心元数据与时间戳；
    - 不向外部暴露底层物理敏感参数（如 cos_bucket、cos_region、cos_object_key），
      实现云端物理存储细节的彻底封装。
    """

    # 语法（Pydantic V2 配置字典）：model_config = ConfigDict(from_attributes=True)
    #   特性：兼容 ORM 模式（等价于 V1 的 orm_mode=True）
    #   核心作用：允许使用 `DocumentRead.model_validate(orm_obj)` 直接从 SQLAlchemy 的
    #            Document 实体对象中按属性名（getattr）读取并映射数据，免除手动编写 dict 解包的繁琐操作。
    #   通俗来讲：给 Pydantic 装上“透视镜”，让它能直接读懂 SQLAlchemy 从数据库捞出来的原生对象。
    model_config = ConfigDict(from_attributes=True)

    # 语法（UUID 主键映射）：id: UUID
    #   特性：全局唯一标识符，输出为标准 36 位连字符 UUID 格式
    #   通俗来讲：文档唯一的“身份证号”。
    id: UUID

    # 语法（文件名标识）：name: str
    #   特性：文档展示名称（如 "API架构规范.pdf"）
    #   通俗来讲：文件的实际名字。
    name: str

    # 语法（哈希摘要指纹）：file_hash: str
    #   特性：文档全文 SHA-256 64 位哈希特征码，供秒传幂等判断与溯源校验
    #   通俗来讲：文件的数字指纹，用来证明文件内容独一无二。
    file_hash: str

    # 语法（网络媒体格式）：mime_type: str
    #   特性：标准 MIME 类型字符串（如 "application/pdf"）
    #   通俗来讲：告诉前端这个文件在网络协议里算什么格式。
    mime_type: str

    # 语法（物理字节数）：size: int
    #   特性：文件总长度（Bytes 字节）
    #   通俗来讲：文件的真实物理体积大小。
    size: int

    # 语法（强类型状态约束）：status: DocumentStatusValue
    #   特性：受限的五态字面量枚举类型，保证状态值语义可预测
    #   通俗来讲：当前文档处于什么进度阶段（上传中、解析中、就绪还是失败）。
    status: DocumentStatusValue

    # 语法（联合类型与默认值）：error_message: str | None = None
    #   特性：可选异常说明；仅当 status 为 failed 时记录底层解析或向量化的具体堆栈摘要，其余状态均为 None
    #   通俗来讲：只有在文档处理失败时才记录报错原因，平时正常就留空。
    error_message: str | None = None

    # 语法（时间戳感知类型）：created_at / updated_at: datetime
    #   特性：ISO 8601 标准时间戳，分别记录创建时刻与最后修改状态时刻
    #   通俗来讲：文档被创建和被更新的精确时间戳。
    created_at: datetime
    updated_at: datetime


# =============================================================================
# 3. 文档分页列表包装响应模型
# =============================================================================
class DocumentListResponse(BaseModel):
    """文档多条件查询分页列表响应包装模型。

    【核心结构】：
    - 统一将“数据明细切片（items）”与“翻页元数据（total/page/page_size）”打平成统一载荷返回，
      便于前端通用分页表格与分页条控件统一消费。
    """

    # 语法（泛型列表嵌套）：items: list[DocumentRead]
    #   特性：当前页符合检索条件的文档详情集合
    #   通俗来讲：当前这一页展示的文档列表数据。
    items: list[DocumentRead]

    # 语法（匹配总行数）：total: int
    #   特性：命中筛选条件的全量数据库记录总数，用于前端计算总页数（Math.ceil(total / page_size)）
    #   通俗来讲：符合条件的数据总共有多少条。
    total: int

    # 语法（字段边界守卫）：Field(ge=1)
    #   特性：约束当前页码必须大于等于 1
    #   通俗来讲：表示当前是第几页，页码必须大于等于 1。
    page: int = Field(ge=1)

    # 语法（容量边界限流守卫）：Field(ge=1, le=100)
    #   特性：限制每页最大拉取量为 100 条，杜绝恶意传入 page_size=99999 撑爆网络带宽与应用内存
    #   通俗来讲：每页展示多少条，最小 1 条，最多 100 条，防止一次性拉太多把系统拖垮。
    page_size: int = Field(ge=1, le=100)


# =============================================================================
# 阶段 2：定义切片（Chunk）模型与响应 Schema
# =============================================================================

# 语法（模块级私有常量定义）：_CONTENT_EXCERPT_LIMIT = 100
#   特性：单下划线声明为模块内部使用的截断阈值，统一限制列表返回时文本摘录的最大字符长度
#   核心收益：避免超长 Chunk 在列表接口中全量序列化，杜绝列表接口因超长文本导致响应体爆炸、网络拥塞及前端 DOM 假死
#   通俗来讲：这是切片列表的“字数门禁”——翻页看列表时最多只给看前 100 个字，想看全文必须走详情接口。
_CONTENT_EXCERPT_LIMIT = 100


# =============================================================================
# 1. 切片列表单项摘要读取模型
# =============================================================================
class DocumentChunkRead(BaseModel):
    """Chunk 列表展示项。content_excerpt 已在 API 层截断到固定长度。

    【性能优化设计】：
    - 不直接返回全文 `content`，而是暴露经过首尾裁剪的 `content_excerpt`；
    - 保留切片的物理定位（页码 page_no、标题树路径 section_path）及字符统计指标。
    """

    # 语法（UUID 主键）：id: UUID
    #   特性：切块全局唯一主键，用于定位单个切块的详情或向量追溯
    #   通俗来讲：切块的专属身份证号。
    id: UUID

    # 语法（切块序号）：chunk_index: int
    #   特性：记录在父文档中的绝对顺序索引（0, 1, 2...），保障切片重构时的上下文连续性
    #   通俗来讲：这是第几个切片，用来按先后顺序排队。
    chunk_index: int

    # 语法（联合可选类型）：page_no: int | None = None
    #   特性：PDF 等分页文档中记录物理页码，纯文本/Markdown 解析场景为 None
    #   通俗来讲：属于第几页的内容，网页或 Markdown 没有页码就留空。
    page_no: int | None = None

    # 语法（联合可选类型）：section_path: str | None = None
    #   特性：文档大纲层级结构路径（如 "第1章 > 1.2节 > 核心架构"），用于辅助检索与展示
    #   通俗来讲：这个切片在文章目录里的层级位置。
    section_path: str | None = None

    # 语法（截断后文本摘录）：content_excerpt: str
    #   特性：截断至 100 字符以内并带省略号的文本摘录，代替原始全量文本
    #   通俗来讲：切片文本的前 100 字缩略摘要。
    content_excerpt: str

    # 语法（字符数统计）：char_count: int
    #   特性：记录此切块实际包含的原始字符长度，无需前端再次计算
    #   通俗来讲：这个切片到底包含多少个字。
    char_count: int

    # 语法（切片内容指纹）：chunk_hash: str
    #   特性：针对 chunk 正文生成的哈希摘要，用于切块去重及增量更新识别
    #   通俗来讲：切片内容的数字指纹，用来核对文本有没有被改过。
    chunk_hash: str

    # 语法（类方法工厂模式）：@classmethod def from_orm_chunk(cls, chunk) -> "DocumentChunkRead"
    #   特性：手动实现 ORM 到 DTO 的转换工厂，精确实施 100 字符切片裁剪与省略号拼接，替代简单的自动映射
    #   类型注视：# type: ignore[no-untyped-def] 规避底层 ORM 参数动态类型推导时的静态检查报错
    #   通俗来讲：给类装上一个“智能打包机”，把数据库里的原始切片塞进去，它会自动把长文本剪成 100 字摘要并打上“...”，打包成交付用的数据对象。
    @classmethod
    def from_orm_chunk(cls, chunk) -> "DocumentChunkRead":  # type: ignore[no-untyped-def]
        content = chunk.content or ""
        excerpt = content[:_CONTENT_EXCERPT_LIMIT]
        if len(content) > _CONTENT_EXCERPT_LIMIT:
            excerpt += "..."
        return cls(
            id=chunk.id,
            chunk_index=chunk.chunk_index,
            page_no=chunk.page_no,
            section_path=chunk.section_path,
            content_excerpt=excerpt,
            char_count=len(content),
            chunk_hash=chunk.chunk_hash,
        )


# =============================================================================
# 2. 切片整体分布统计模型
# =============================================================================
class DocumentChunkStats(BaseModel):
    """切分统计：直观看到 chunk_size / overlap 配置的实际效果。

    【分析与调优价值】：
    - 汇总展示切分后的总块数与长度分布区间；
    - 协助评估当前文档解析与切分参数是否出现极端碎片化（如 min_length 过小）或超载（如 max_length 过大）。
    """

    # 语法（切块总数）：total: int
    #   特性：父文档切分出的有效分块总量
    #   通俗来讲：一篇文章切成了多少块。
    total: int

    # 语法（平均字符长度）：avg_length: int
    #   特性：全量切块的平均字符容量，评估分块基准线
    #   通俗来讲：每个切片平均包含多少个字。
    avg_length: int

    # 语法（最小分块长度）：min_length: int
    #   特性：全量切块中字符最少的值，排查是否存在无意义的零碎分块
    #   通俗来讲：切出来的最小块包含多少个字。
    min_length: int

    # 语法（最大分块长度）：max_length: int
    #   特性：全量切块中字符最多的值，排查是否超过 Token 限制
    #   通俗来讲：切出来的最大块包含多少个字。
    max_length: int


# =============================================================================
# 3. 切片分页列表与聚合统计组合响应模型
# =============================================================================
class DocumentChunkListResponse(BaseModel):
    """切块列表分页响应组合载荷。

    【结构特点】：
    - 组合 items 分页数据与全局 stats 统计指标，一次请求同时交付列表数据与质量体检报告。
    """

    # 语法（泛型切片列表）：items: list[DocumentChunkRead]
    #   特性：当前页包含的轻量级截断切块对象列表
    #   通俗来讲：这一页切块的缩略摘要列表。
    items: list[DocumentChunkRead]

    # 语法（记录总行数）：total: int
    #   特性：命中该文档的切块总行数
    #   通俗来讲：数据库里总共存了多少块。
    total: int

    # 语法（分页校验边界）：Field(ge=1)
    #   特性：请求页码下限约束
    #   通俗来讲：当前是第几页（从 1 开始）。
    page: int = Field(ge=1)

    # 语法（容量边界限流）：Field(ge=1, le=100)
    #   特性：约束单页容量上限为 100，防止批量读取造成网络与内存过载
    #   通俗来讲：每页最多取 100 个切块。
    page_size: int = Field(ge=1, le=100)

    # 语法（联合统计类型与默认值）：stats: DocumentChunkStats | None = None
    #   特性：全局汇总统计指标；若文档处于 UPLOADING 或未切片状态则为 None
    #   通俗来讲：整篇文档的切片体检汇总指标，还没切好时为 None。
    stats: DocumentChunkStats | None = None


# =============================================================================
# 4. 单切片（Chunk）完整详情响应模型
# =============================================================================
class DocumentChunkDetail(BaseModel):
    """chunk 详情：返回完整 content。

    【核心业务定位与安全边界】：
    - 专用于单块切片的精细化详情查看，与列表模型的 100 字符截断形成鲜明互补；
    - 完整暴露原始切片正文 `content`、所属父文档主键 `document_id` 与创建时间戳 `created_at`；
    - 关键安全防线：严格隐藏底层用于向量检索的高维浮点数组（Embedding Vector），
      避免数百甚至数千维度的浮点数据造成巨额序列化开销并泄露底层特征空间。
    """

    # 语法（Pydantic V2 ORM 映射模式）：model_config = ConfigDict(from_attributes=True)
    #   特性：允许 Pydantic 直接从 SQLAlchemy ORM 实体（DocumentChunk）中读取对应字段
    #   通俗来讲：给详情包装盒装上透视镜，能直接把数据库查出来的切片实体字段识别出来。
    model_config = ConfigDict(from_attributes=True)

    # 语法（UUID 切块主键）：id: UUID
    #   特性：当前文本切块的全局唯一标识符
    #   通俗来讲：这个切块唯一的“身份证号”。
    id: UUID

    # 语法（UUID 父级外键）：document_id: UUID
    #   特性：所属父级文档的主键 ID，明确从属归属链条
    #   通俗来讲：标明这个切片是从哪一篇主文档里切出来的。
    document_id: UUID

    # 语法（顺序位置标识）：chunk_index: int
    #   特性：切块在父文档内的绝对排列位次（从 0 开始自增）
    #   通俗来讲：这是文档切出的第几块，方便前端按原书先后次序回显。
    chunk_index: int

    # 语法（联合可选类型）：page_no: int | None = None
    #   特性：PDF 等分页文档中记录对应的原始物理页码，无物理页码文档为 None
    #   通俗来讲：如果在 PDF 里切出来的就记录在第几页，普通网页没页码就留空。
    page_no: int | None = None

    # 语法（联合可选类型）：section_path: str | None = None
    #   特性：标题树的大纲面包屑路径（如 "引言 > 背景介绍"）
    #   通俗来讲：标明切片在整篇文档目录树里的章节位置。
    section_path: str | None = None

    # 语法（全量正文字符串）：content: str
    #   特性：未经裁剪的完整切片正文（列表接口只返回 100 字摘要，详情接口交付完整原文）
    #   通俗来讲：切片里面一字不差的完完整整文本内容。
    content: str

    # 语法（字符数统计）：char_count: int
    #   特性：记录此切片包含的真实字符总数
    #   通俗来讲：这个切块到底有多少个字。
    char_count: int

    # 语法（内容哈希防伪指纹）：chunk_hash: str
    #   特性：针对正文计算的哈希特征指纹，供前端或校验器比对文本内容变动
    #   通俗来讲：切片文本的防伪数字印章。
    chunk_hash: str

    # 语法（创建时刻时间戳）：created_at: datetime
    #   特性：切块生成的精确系统时间戳（ISO 8601 标准）
    #   通俗来讲：这个切片被 AI 或解析器生产出来的具体时间。
    created_at: datetime

    # 语法（类方法工厂模式）：@classmethod def from_orm_chunk(cls, chunk) -> "DocumentChunkDetail"
    #   特性：自定义对象转换工厂，显式处理 content 为 None 的边界异常并动态计算 char_count
    #   类型注解：# type: ignore[no-untyped-def] 屏蔽动态类型推导告警
    #   通俗来讲：为详情页定制的“打包流水线”，把数据库捞出的原始切片喂给它，自动补齐字数和创建时间，封箱交货。
    @classmethod
    def from_orm_chunk(cls, chunk) -> "DocumentChunkDetail":  # type: ignore[no-untyped-def]
        return cls(
            id=chunk.id,
            document_id=chunk.document_id,
            chunk_index=chunk.chunk_index,
            page_no=chunk.page_no,
            section_path=chunk.section_path,
            content=chunk.content,
            char_count=len(chunk.content or ""),
            chunk_hash=chunk.chunk_hash,
            created_at=chunk.created_at,
        )