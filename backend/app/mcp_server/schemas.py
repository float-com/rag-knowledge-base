"""【模块职责说明】MCP Server 的入参 / 出参 Schema 层（第 3 章）。

本模块只做一件事：**定义所有 MCP 工具对外暴露的契约（工具见到的字段长什么样）**。

================================================================================
【步骤 1】为什么专门为 MCP 另建一份 schema，而不复用 api/schemas/documents.py
================================================================================

这是本章唯一的设计决策，理由与第 1 章《方案设计》里定的"Schema 独立设计"一致：

| | 网页侧契约（app/api/schemas/*） | 本模块（app/mcp_server/schemas.py） |
| --- | --- | --- |
| 读者是谁 | 自家前端（types.gen.ts 逐字段对齐） | **外部 Agent**（Cursor / Claude Desktop 的模型） |
| 字段来源 | 往往直接从 ORM 列映射过来 | **只保留 Agent 真正需要的字段** |
| 能否带内部字段 | 可以（前端不管这些） | **绝不可以**：retrieval_meta / rerank_score / cos_object_key 等 |
| 随表结构变化 | 加一列就跟着加一个字段 | **不动**：由 Agent 的需求决定，而不是由 documents 表的列决定 |

一句话：**同一个业务能力，面向不同消费者时应该有各自的门面。**
第 11 期的 `api/schemas/auth.py` 已经做过一次同构判断（`UserRead` 必须与前端类型逐字段一致），
本章是它的镜像：**MCP 的字段集合由 Agent 的需求决定，不由 ORM 的列决定。**

好处很实在：第 12 期给 documents 表加过 `version` 列，今后再加列（比如多一个内部指标），
MCP 的契约不会被动跟着变 —— 否则每次迁移都要回头想"这列要不要给 Agent 看"。

================================================================================
【步骤 2】本模块的三个区块，与 5 个工具一一对应
================================================================================

    问答相关：MCPCitation / MCPAnswer          → ask_knowledge_base 出参
    文档相关：MCPUploadResult / MCPDocumentItem / MCPDocumentList / MCPDocumentStatus
                                              → upload_document / list_documents /
                                                get_document_status 出参（list_documents 的
                                                行模型是 MCPDocumentItem，列表壳是 MCPDocumentList）
    统计相关：MCPStats                         → get_knowledge_base_stats 出参

================================================================================
【步骤 3】命名约定：为什么统一叫 Result / Item / List，而不是 Read
================================================================================

网页侧出参一律叫 `XxxRead`（沿 FastAPI 惯例）；本模块没有用 Read，而是
Result / Item / List / Status —— 因为它们**不是 ORM 的"读模型"**，
而是"工具调用的返回值"。这个命名差别是刻意的：**看到名字就知道自己站在哪一侧**，
不会有人误以为可以拿它去 `model_validate(orm_obj)`（只有 MCPDocumentItem 支持，
而且那是靠显式声明的 `from_attributes=True`）。

⚠️ 另外两个必须同时遵守的约定：
1. **都是纯 Pydantic 模型**，不继承 ORM、不引用 Session —— 本模块不碰数据库；
2. **写到这里就够了**：FastMCP 之后会自动把这些模型抽成 JSON Schema 暴露给 Agent，
   模块里**不需要**写任何 `model_json_schema()` 之类的调用。
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.api.schemas.documents import DocumentStatusValue, IngestionTaskStatusValue

# =============================================================================
# 问答相关
# =============================================================================


class MCPCitation(BaseModel):
    """问答引用快照。与 SSE / 历史接口的 `citation` 形状对齐，但只保留外部 Agent 真正需要的字段，
    避免把 `retrieval_meta` / `rerank_score` 等内部调试元数据塞给 Agent。
    """

    # 【语法点 1：Field(description=...) 不是注释，而是契约的一部分】
    # 网页侧的 description 主要给人看文档；MCP 侧的 description 是**写给模型看的自然语言提示**——
    # FastMCP 抽 JSON Schema 时会把它一起带给客户端，模型据此判断这个参数/字段是什么。
    # 所以这里的 description 要按"给模型讲清楚"的标准写，而不是按"给同事看一眼"的标准写。
    # 引用在本次回答里排第几（prompt 里写的「片段 N」就是它，用于回答正文与来源对上号）
    ordinal: int = Field(description="prompt 中给 LLM 的「片段 N」编号，从 1 开始")
    # 被引用文档的主键：Agent 想进一步查这份文档时，拿它去调 get_document_status
    document_id: UUID
    # 文档名（人看的名字，不是对象键）：Agent 直接展示给用户的那一行来源
    document_name: str
    # 该片段在第几页；解析不出页码（如纯 markdown、txt）时为 null
    page_no: int | None = None
    # 片段所属的章节路径（如「3.2 鉴权」）；没有标题层级信息时为 null
    section_path: str | None = None
    # 引用原文（片段正文），Agent 用来做引述、核对回答依据
    quote: str = Field(description="该片段在 prompt 里的原文")


class MCPAnswer(BaseModel):
    """`ask_knowledge_base` 出参。"""

    # 最终答案正文：Agent 要转述给用户的内容；拒答时这里放的是拒答话术
    answer: str
    # 【为什么拒答要显式出一个布尔字段，而不是让 Agent 从答案文本里猜】
    # 拒答时 answer 里也是一段人话，模型容易把"知识库里没有"误当成一个正常答案。
    # 单独给一个结构化标志位，Agent 才能做确定性判断（例如据此改问法或直接告诉用户查不到）。
    refused: bool = Field(description="是否触发拒答（命中阈值不足或答案校验失败）")
    # 【语法点 2：list[...] 的可变默认值必须用 default_factory，不能写 = []】
    # Pydantic 会深拷贝 default，写 = [] 本身不至于串数据，
    # 但 default_factory 表达的是"每次构造时新造一个空列表"，语义更准，也是官方推荐写法。
    # 答案用到的引用列表：为空不代表出错（模型可以直接作答，也可能是拒答）
    citations: list[MCPCitation] = Field(default_factory=list)
    # 本次调用的可观测 ID：Agent 可以把它交回给用户在 LangSmith 上追整条链路
    trace_id: str | None = Field(
        default=None,
        description="LangSmith trace_id，未启用观测时为空",
    )
    # 【语法点 3：为什么可空字段必须显式写 default=None】
    # 在 Pydantic v2 里 `str | None` 只是"允许 None"的类型标注，**并不等于有默认值**——
    # 不写 default=None，这个字段就变成必填，Agent 拿不到观测 ID 时构造就会失败。
    # 这与第 1 章 §7.2 的风险预警直接相关：语义缓存命中时会跳过整张图，trace_id 本来就是空的。


# =============================================================================
# 文档相关
# =============================================================================


class MCPUploadResult(BaseModel):
    """`upload_document` 出参。"""

    # 新建文档的主键：Agent 后续调 get_document_status / list_chunks 都用它
    document_id: UUID
    # 文件名（上传时传的名字）
    name: str
    # 刚上传时的状态：一般立刻是 uploading / parsing，Agent 应再轮询状态接口
    status: DocumentStatusValue
    # 文档版本号：同一份文件重复上传会递增，Agent 可据此确认"这次是新版本还是复用旧文档"
    version: int
    # 文件内容指纹（sha256）：Agent 重试上传时靠它判断"这份文件其实已经在库里"，避免重复入库
    file_hash: str = Field(
        description="sha256；文件级幂等键，相同 hash 复用现有文档"
    )
    # 【为什么要把 file_hash 这个"内部键"也给 Agent】
    # 它不是调试信息，而是**幂等语义**：Agent 重试上传时能靠它判断"这份文件其实已经在库里"。
    # 判断标准始终是"Agent 需不需要"，而不是"这是不是内部字段"。


class MCPDocumentItem(BaseModel):
    """`list_documents` 列表项。"""

    # 【语法点 4：ConfigDict(from_attributes=True) 是 Pydantic v2 的 ORM 模式开关】
    # v1 时代靠内部类 `class Config: orm_mode = True`；v2 改成构造参数 `model_config = ConfigDict(...)`。
    # 打开它之后，`MCPDocumentItem.model_validate(orm_document)` 才能按**属性**（而不是按字典键）取值。
    # ⚠️ 本模块里只有这一个模型需要它 —— 其余模型都是"我们自己拼出来的返回值"，
    #    从 ORM 对象直接转（见第 4 章的 model_validate 边界，那里有踩过的坑）。
    model_config = ConfigDict(from_attributes=True)

    # 文档主键：Agent 拿它去调 get_document_status / list_chunks
    id: UUID
    # 文件名：列表页展示、也是 Agent 向用户指认文档的依据
    name: str
    # 当前生命周期状态：ready 才表示"可被检索"，见 DocumentStatusValue 五个取值
    status: DocumentStatusValue
    # MIME 类型（如 application/pdf）：让 Agent 知道能不能预览、该不该下载
    mime_type: str
    # 文件字节数：Agent 可据此判断是否适合整份读取
    size: int
    # 文档版本号：第 12 期增量索引引入，重建索引后仍指向最新版本
    version: int
    # 权限标签是**这个用户能看见这份文档的依据**，Agent 侧可见，不属内部细节
    permission_tags: list[str] = Field(default_factory=list)
    # 创建时间（UTC）：Agent 回答"最近上传了什么"这类问题时用
    created_at: datetime
    # 最后一次变更时间（含改权限、重建索引）：注意它**不等于**最后一次索引完成时间
    updated_at: datetime


class MCPDocumentList(BaseModel):
    """`list_documents` 出参，标准分页结构。"""

    # 当前页的文档行（已按调用者权限过滤后的结果）
    items: list[MCPDocumentItem]
    # 满足条件的总条数：Agent 据此决定"还要不要再翻一页"
    total: int
    # 当前页码：原样回显请求里的 page
    page: int = Field(ge=1)
    # 【语法点 5：ge / le 是"数值约束"，会写进 JSON Schema 让模型自己收敛】
    # ge=1 表示 >= 1，le=100 表示 <= 100。FastMCP 抽 schema 时把它带给客户端，
    # 模型填参数时就知道"别一次要 1000 条"，而不是等调用被服务端拒掉再重试。
    # ⚠️ 与第 2 章的鉴权无关：这里是**参数形状**的防线，权限过滤仍然在业务层按 permission_tags 做。
    page_size: int = Field(ge=1, le=100)


class MCPDocumentStatus(BaseModel):
    """`get_document_status` 出参。

    `latest_task_*` 字段允许外部 Agent 直接观察 ingest / reindex 进度，
    避免它们再多调一次 `list_documents`。
    """

    # 文档主键：Agent 拿它回填到后续调用（status / chunks）
    document_id: UUID
    # 文件名：状态卡片上显示的那一行
    name: str
    # 当前状态（与 MCPDocumentItem.status 同义）：ready 才代表可检索
    status: DocumentStatusValue
    # 文档版本号：重建索引/重复上传后递增，Agent 可确认自己看的是不是最新版
    version: int
    # 失败原因（仅当 status == "failed" 时有值）：Agent 可直接把这句话转述给用户
    error_message: str | None = None
    # 【语法点 6：Literal[...] 把字符串"枚举化"，同时也是给模型的选项清单】
    # 下面两个字段复用网页侧的 DocumentStatusValue / IngestionTaskStatusValue 两个 Literal 别名
    # （定义在 app/api/schemas/documents.py）。复用它们是有意的：
    # **状态取值只有一份定义**，网页侧将来加一个状态，MCP 侧自动跟着对上。
    #   - 复用类型别名：可以（防止两套取值口径漂移）
    #   - 复用整个模型：不可以（那会把网页侧的内部字段一起带过来，见【步骤 1】）
    #
    # 最近一次任务是入库还是重建索引；从未有过任务时为 null
    latest_task_type: Literal["ingest", "reindex"] | None = None
    # 该任务的状态（pending / running / success / failed）
    latest_task_status: IngestionTaskStatusValue | None = None
    # 任务总工作量（切片总数）：与 progress_done 组成进度分母/分子
    latest_task_progress_total: int | None = None
    # 已完成的切片数：progress_done / progress_total 就是百分比
    latest_task_progress_done: int | None = None
    # 任务级失败原因（与文档级 error_message 不是同一件事，别混用）
    latest_task_error_message: str | None = None
    # 【为什么 latest_task_* 全部可空】
    # 文档可能**从未入过库**（刚上传、任务还没落库），此时一个任务都没有；
    # 也可能上传任务早已被清理。硬要求非空，Agent 就会在"没有任务"这种正常状态下拿到报错。
    # ⚠️ 这与第 1 章 §7.2 的提醒一致：可空字段是真的会为 None，不是"理论上可空"。


# =============================================================================
# 统计相关
# =============================================================================


class MCPStats(BaseModel):
    """`get_knowledge_base_stats` 出参。

    严格按调用者权限范围统计；admin 视角看全量，普通用户只看自己有权访问的。
    """

    # 该用户可见的文档总数（admin 为全库）
    document_count: int
    # 对应的切片总数：Agent 用它判断"知识库规模/是否值得检索"
    chunk_count: int
    # 【口径写进 description，是因为"最近一次"有歧义】
    # 不写清楚，模型可能把"最后一次上传时间"当成"最后一次可检索时间"讲给用户听。
    last_indexed_at: datetime | None = Field(
        default=None,
        description="最近一次进入 ready 状态的文档时间；库为空时返回 null",
    )
