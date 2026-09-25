"""评测与 Bad Case 分析 API 请求与响应模型（契约层，第 10 期）。

【模块职责说明】：
1. 协议契约声明（API Contract Declaration）：
   用 Pydantic 模型统一描述评测模块对外的请求体与响应体结构。
   路由层与 Service 层之间只通过本模块的模型交换数据，
   避免把 SQLAlchemy 实体（EvaluationRun / EvaluationItem）直接暴露到 HTTP 边界。

2. 枚举契约的"单一事实源"问题（Single Source of Truth）：
   BadCaseCategoryValue 是 13 类归因的字面量联合类型，它必须与
   `app/evaluation/scoring.py` 的 `BadCaseCategory` **逐字对齐**（值 + 顺序）。
   本项目目前存在 4 份拷贝：scoring.py / 本文件 / frontend 的 types.gen.ts / 数据库列注释。
   ⚠️ 改动任何一处都必须同步另外三处 —— 将来若跑 `gen:api`，本文件会成为 TS 类型的来源。

3. 两个 Run 读模型为什么要分开（Read vs ListItem）：
   `EvaluationRunRead`（详情）比 `EvaluationRunListItem`（列表）多三个字段：
   `error_message` / `started_at` / `finished_at`。
   列表页只展示名称、状态、进度与聚合分，不需要"失败原因"和起止时间戳；
   分开定义可以让列表响应更小、也更贴合前端表格的列。

4. ORM 实体到 DTO 的转换（Entity-to-DTO Mapping）：
   通过 `model_config = ConfigDict(from_attributes=True)`，
   Pydantic 可以直接按属性名从 ORM 实体取值（`Model.model_validate(entity)`），
   免去手写一堆转换函数。

5. 三态与 PATCH 语义：
   - `citation_hit: bool | None`：`None` 表示"该题本就该拒答、不考引用"（不是"未命中"）；
   - `EvaluationItemUpdate` 三个字段全为可选：**传 `None` = 本次不改这一项**，
     这是 PATCH 的部分更新语义，与 PUT 的全量覆盖完全不同。
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


# =============================================================================
# 1. 字面量契约（Literal 枚举）
# =============================================================================
# run 生命周期状态：
# 用 Literal 而非 Enum，使 OpenAPI 直接输出候选值字符串数组，
# 前端生成 TS 时得到 `"running" | "completed" | "failed"`，无需额外映射。
EvaluationStatusValue = Literal["running", "completed", "failed"]

# 与 app/evaluation/scoring.py 中 BadCaseCategory Literal 对齐
# 13 类的完整语义（每一类由哪一层产出、能不能被规则引擎自动判定）见 scoring.py 的注释。
BadCaseCategoryValue = Literal[
    "document_parse_failed",    # 文档解析失败
    "chunk_split_bad",          # 文本分块不合理
    "embedding_recall_miss",    # 向量检索未命中
    "keyword_recall_miss",      # 关键字检索未命中
    "rrf_fusion_error",         # 混合检索融合策略故障
    "rerank_order_error",       # 重排序次序错误
    "context_judge_too_loose",  # 边界闸门判定过松（该拒没拒）
    "context_judge_too_strict", # 边界闸门判定过严（不该拒却拒）
    "prompt_constraint_weak",   # 提示词约束过弱（答非所问）
    "generation_off_context",   # 生成偏离上下文（幻觉）
    "citation_parse_failed",    # 引用格式解析失败
    "permission_filter_error",  # 权限过滤错误
    "other",                    # 运行环境报错等硬故障
]


# =============================================================================
# 2. Run（评测批次）相关模型
# =============================================================================
class EvaluationRunCreate(BaseModel):
    """创建 run 的请求体。dataset_name 不带后缀（如 `seed`）。"""

    # 长度约束 = 数据库列宽（String(256) / String(128)）：
    # 让超长输入在这里就返回 422，而不是到数据库层才报错。
    name: str = Field(min_length=1, max_length=256, description="便于回看的 run 名称")
    dataset_name: str = Field(
        min_length=1, max_length=128, description="评测集文件名（不带 .jsonl）"
    )


class EvaluationRunRead(BaseModel):
    """run 详情响应体。

    【为什么 8 个聚合分都是 `float | None`】：
    刚创建（running）时还没跑完，聚合分全为 NULL；
    跑完后也只有"有有效样本"的指标才有值（例如整批全是拒答题时 faithfulness 仍是 NULL）。
    前端看到 `null` 才知道"还没测/没测出来"，看到 `0` 会误以为"全错"。
    """

    model_config = ConfigDict(from_attributes=True)

    # --- 标识与元数据 ---
    id: UUID
    name: str
    dataset_name: str
    dataset_size: int
    status: EvaluationStatusValue

    # --- 进度三元组（前端进度条 = progress_completed / progress_total）---
    # ⚠️ progress_failed 是 progress_completed 的【子集】：
    #    跑完一条就 completed+1；该条降级失败时再额外 failed+1。二者不是互斥计数。
    progress_total: int
    progress_completed: int
    progress_failed: int

    # --- 8 个聚合指标（跑完后才有值）---
    faithfulness: float | None = None
    answer_relevancy: float | None = None
    context_precision: float | None = None
    context_recall: float | None = None
    citation_hit_rate: float | None = None
    refusal_accuracy: float | None = None
    avg_latency_ms: float | None = None
    avg_first_token_latency_ms: float | None = None

    # --- 失败原因与时间戳 ---
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime


class EvaluationRunListItem(BaseModel):
    """列表元素：与 Read 一致字段，提取出来后续可裁字段也方便。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    dataset_name: str
    dataset_size: int
    status: EvaluationStatusValue
    progress_total: int
    progress_completed: int
    progress_failed: int
    faithfulness: float | None = None
    answer_relevancy: float | None = None
    context_precision: float | None = None
    context_recall: float | None = None
    citation_hit_rate: float | None = None
    refusal_accuracy: float | None = None
    avg_latency_ms: float | None = None
    avg_first_token_latency_ms: float | None = None
    created_at: datetime


class EvaluationRunPage(BaseModel):
    """run 分页响应体。

    items / total / page / page_size 四件套是项目统一的分页协议：
    前端表格既要当前页数据，也要 total 才能算总页数、渲染页码栏。
    """

    items: list[EvaluationRunListItem]
    total: int
    page: int
    page_size: int


# =============================================================================
# 3. Item（单条 case）相关模型
# =============================================================================
class EvaluationItemRead(BaseModel):
    """单条 case 的输入快照 + 实际输出 + 指标 + Bad Case 归因。

    这是"抽检报告"的完整数据源，字段按四段组织：
    ① 输入快照（评测集标注的期望）→ ② 实际输出（RAG 真实产物）
    → ③ 白盒轨迹（路由 / Agent 步骤 / 校验）→ ④ 指标与归因。
    """

    model_config = ConfigDict(from_attributes=True)

    # --- ① 身份与输入快照 ---
    id: UUID
    run_id: UUID
    # 用例业务编号（如 smoke_001），跨批次对账同一条 case 时用它
    case_id: str
    question: str
    expected_answer: str
    # 【为什么用 Field(default_factory=list) 而不是 = []】：
    # 直接写 [] 会让所有实例共享同一个列表对象，是 Pydantic 的经典可变默认值陷阱。
    expected_document_names: list[str] = Field(default_factory=list)
    expected_keywords: list[str] = Field(default_factory=list)
    should_refuse: bool
    tags: list[str] = Field(default_factory=list)

    # --- ② RAG 实际输出 ---
    actual_answer: str
    actual_refused: bool
    # 结构化引用角标（9 个键：ordinal / chunk_id / document_name / quote / retrieval_meta ...）
    citations: list[dict] = Field(default_factory=list)
    # 检索切片的轻量快照（9 键，保留 content 供 RAGAS 当上下文用）
    retrieved_chunks_meta: list[dict] = Field(default_factory=list)

    # --- ③ 白盒轨迹与可观测性 ---
    # 语义路由结果（route / query / rewritten_query / hyde_answer / multi_queries）
    query_route: dict | None = None
    # Agentic 检索循环的逐轮「决策 + 观察」轨迹
    agent_steps: list[dict] | None = None
    # 后置反幻觉校验结果（verified + reason）；拒答路径不做校验，为 None
    verify_result: dict | None = None
    # LangSmith 链路追踪 ID（未启用观测时为 None）
    trace_id: str | None = None
    latency_ms: int
    # 首字耗时；拒答路径不调 LLM，为 None
    first_token_latency_ms: int | None = None
    # 单条 case 的降级失败原因（由评测入口捕获，不向上抛）
    error_message: str | None = None

    # --- ④ 指标与归因 ---
    faithfulness: float | None = None
    answer_relevancy: float | None = None
    context_precision: float | None = None
    context_recall: float | None = None
    # ⚠️ 三态：True 命中 / False 未命中 / None 不适用（该题本就该拒答）
    citation_hit: bool | None = None
    refusal_correct: bool

    is_bad_case: bool
    # 机评给出的归因分类；人工可通过 PATCH 覆盖
    bad_case_category: BadCaseCategoryValue | None = None
    # 人工复核备注（机评不写这个字段）
    bad_case_note: str | None = None
    created_at: datetime


class EvaluationItemPage(BaseModel):
    """case 分页响应体（与 run 分页同构）。"""

    items: list[EvaluationItemRead]
    total: int
    page: int
    page_size: int


class EvaluationItemUpdate(BaseModel):
    """前端覆盖 Bad Case 归因。

    传 `is_bad_case=null` 时保持原值；显式传 `is_bad_case=false` 可手动把
    误判的 case 标回"非 Bad Case"。

    【PATCH 语义要点】：
    三个字段都是可选，统一遵循"**传 None = 本次不改这一项**"。
    注意一个反直觉之处：**传 `bad_case_category=None` 并不会清空数据库里的分类** ——
    清空归因只有一条路，就是显式传 `is_bad_case=False`（由 Service 层联动抹平）。
    """

    bad_case_category: BadCaseCategoryValue | None = None
    bad_case_note: str | None = None
    is_bad_case: bool | None = None


# =============================================================================
# 4. 评测集元数据（下拉列表用）
# =============================================================================
class DatasetInfo(BaseModel):
    """单个评测集的元信息：名称 + 用例条数。

    ⚠️ 这里的 `size` 来自对 .jsonl 的**行数扫描**（不解析 JSON），
    因此它等于"有效非空行数"，而不是解析成功后的用例数。
    """

    name: str
    size: int


class DatasetListResponse(BaseModel):
    """评测集列表响应体：外层包一个 items，便于将来加 meta 字段而不破坏契约。"""

    items: list[DatasetInfo]
