"""评测与 Bad Case 分析 API 路由层（Transport / Controller Layer，第 10 期）。

【模块职责说明】：
1. 协议入口与能力暴露（RESTful Endpoint）：
   把评测模块的能力暴露成 5 条路径 / 8 个端点，统一挂在 `/api/evaluations` 之下：
   - 评测集：列表（前端"新建评测"下拉）
   - 评测批次：创建 / 列表 / 详情 / 删除
   - 用例明细：列表（可按 Bad Case 与归因筛选）/ 详情 / 人工覆盖归因

2. 异步任务的"响应后执行"（BackgroundTasks 协议）：
   创建 run 时**不阻塞 HTTP 请求**去跑评测，而是把 `execute_evaluation_run` 挂到
   FastAPI 的 BackgroundTasks 上。它的语义是【响应发送给客户端之后】才在同一个进程内执行，
   因此接口能立刻返回 201，前端拿到 run_id 后马上开始轮询进度。

3. 依赖注入与协议转换（Dependency Injection & Protocol Mapping）：
   通过 DbSession 注入请求级数据库会话，交给 EvaluationService 完成业务编排；
   路由层只负责参数校验、调用服务、把 ORM 实体转为响应模型，不承载业务规则。

4. 契约校验下沉到框架（Schema-driven Validation）：
   分页参数的 `ge / le` 与归因分类的 `Literal` 都写在签名上，
   由 FastAPI 自动做校验并返回 422，路由函数体内无需手写任何 if 判断。
   ⚠️ 这些 422 是【框架行为】，与业务异常（NotFoundError → 404 / ValidationError → 400）是两条路。

【依赖说明】：
   - `DbSession`：`app/api/deps.py` 里的 `Annotated[AsyncSession, Depends(get_session)]` 类型别名；
   - `execute_evaluation_run`：`app/services/evaluation_service.py` 里的后台执行器（模块级函数）。
"""

from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Response

from app.api.deps import DbSession, get_current_admin
from app.api.schemas.evaluations import (
    BadCaseCategoryValue,
    DatasetInfo,
    DatasetListResponse,
    EvaluationItemPage,
    EvaluationItemRead,
    EvaluationItemUpdate,
    EvaluationRunCreate,
    EvaluationRunListItem,
    EvaluationRunPage,
    EvaluationRunRead,
)
from app.services.evaluation_service import EvaluationService, execute_evaluation_run

# 语法（APIRouter 路由分组）：APIRouter(prefix="/evaluations", tags=["evaluations"])
#   特性：统一声明评测模块的路由前缀与 OpenAPI 聚合标签
#   核心收益：
#     - 路径拼接公式 = 全局前缀 [/api] + 模块前缀 [/evaluations] + 端点子路径；
#     - Swagger UI (/docs) 会把这一组单独折叠成 "evaluations" 分组，便于查阅。
#   通俗来讲：给评测相关接口挂上统一门牌号 `/evaluations`，和 documents / conversations 并列。
#
# 【第 11 期 · 为什么用 dependencies=[Depends(get_current_admin)] 而不是给每个路由加参数】
# 评测是纯管理面能力（会真金白银调 LLM 跑分），8 个端点应当统一要求管理员。
# 两种写法都能实现，差别在于：
#   ① 逐路由加 `_: CurrentAdmin` 参数 —— 要在 8 个函数签名里重复 8 次；
#   ② 挂在 router 上（本处） —— 声明一次，整组生效，新增端点自动继承保护。
# 缺点 ② 是"路由函数内拿不到 User 对象"，但评测模块本来也不需要知道是谁在调用
# （评测跑批固定用通配权限，不代表任何真实用户），所以这个缺点在这里不构成问题。
router = APIRouter(
    prefix="/evaluations",
    tags=["evaluations"],
    dependencies=[Depends(get_current_admin)],
)


# =============================================================================
# 1. 列出可用评测集（前端"新建评测"的下拉数据）
# =============================================================================
# 语法（路由装饰器配置）：
#   - response_model=DatasetListResponse: 强制出参符合列表契约
#   - operation_id: 供前端 SDK 生成唯一函数标识（8 个 operation_id 已确认与现有 18 个零冲突）
#   - summary: 直接渲染到 Swagger 的接口说明
@router.get(
    "/datasets",
    response_model=DatasetListResponse,
    operation_id="listEvaluationDatasets",
    summary="列出可用评测集（jsonl 文件名 + 条数）",
)
async def list_evaluation_datasets(session: DbSession) -> DatasetListResponse:
    """列出 `app/evaluation/datasets/` 下的全部评测集。

    【注意这里没有 await】：
    `EvaluationService.list_datasets()` 是**同步方法**（它只扫目录数行数，不碰数据库），
    所以直接调用即可 —— 不要习惯性地给它加 await。
    """
    service = EvaluationService(session)
    items = service.list_datasets()
    return DatasetListResponse(
        items=[DatasetInfo(name=name, size=size) for name, size in items]
    )


# =============================================================================
# 2. 创建评测 run（并交给 BackgroundTasks 异步执行）
# =============================================================================
# 语法（路由装饰器配置）：
#   - status_code=201: 新建资源成功的标准状态码
#   - BackgroundTasks 是 FastAPI 直接注入的参数（无需 Depends）
@router.post(
    "/runs",
    response_model=EvaluationRunRead,
    status_code=201,
    operation_id="createEvaluationRun",
    summary="创建评测 run 并通过 BackgroundTasks 异步执行",
)
async def create_evaluation_run(
    payload: EvaluationRunCreate,
    session: DbSession,
    background_tasks: BackgroundTasks,
) -> EvaluationRunRead:
    """创建一轮评测，并立即返回（评测在响应之后才开始跑）。

    【调用时机与顺序约束（本文件最关键的两行）】：
        ① `await service.create_run(...)` —— 内部会 `load_dataset` 做前置校验、
           落库 run、并 **commit**；
        ② `background_tasks.add_task(...)` —— 把执行器登记为"响应后任务"。
    **顺序不能颠倒**：后台执行器第一件事就是按 run_id 去查这条 run，
    如果还没提交，它会因为查不到而打印 warning 并静默 skip（任务永远不跑）。

    【为什么能"立即返回 201"】：
    BackgroundTasks 的任务是在响应体发送给客户端**之后**才执行的，
    所以前端拿到 run_id 时评测才刚开始跑，随即靠轮询 run 详情看进度。

    【为什么用 model_validate 而不是 from_orm】：
    Pydantic v2 已统一为 `Model.model_validate(orm_entity)`，
    配合模型上的 `from_attributes=True` 即可按属性名取值。
    """
    service = EvaluationService(session)
    run = await service.create_run(name=payload.name, dataset_name=payload.dataset_name)
    background_tasks.add_task(execute_evaluation_run, run.id)
    return EvaluationRunRead.model_validate(run)


# =============================================================================
# 3. 评测 run 列表（分页）
# =============================================================================
@router.get(
    "/runs",
    response_model=EvaluationRunPage,
    operation_id="listEvaluationRuns",
    summary="按创建时间倒序分页列出评测 run",
)
async def list_evaluation_runs(
    session: DbSession,
    # 分页参数的双重钳制：
    #   本层用 Query(ge/le) 做"框架级"校验（越界 → 422）；
    #   仓储层还会再 max(min(page_size, 100), 1) 兜一次 —— 因为它可能被脚本/Worker 直接调用，
    #   不能假设调用方一定走 HTTP。
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> EvaluationRunPage:
    """分页列出评测批次，按 created_at 倒序。

    【为什么要手动拼 Page 而不是直接返回元组】：
    服务层返回的是 `(ORM 实体列表, total)`，需要在本层做两件事：
    ① 把每个 ORM 实体转成 `EvaluationRunListItem`（脱敏、裁字段）；
    ② 补齐 page / page_size 这两个"请求侧"信息（服务层只关心数据，不关心页码）。
    """
    service = EvaluationService(session)
    items, total = await service.list_runs(page=page, page_size=page_size)
    return EvaluationRunPage(
        items=[EvaluationRunListItem.model_validate(r) for r in items],
        total=total,
        page=page,
        page_size=page_size,
    )


# =============================================================================
# 4. 评测 run 详情（前端进度轮询的目标接口）
# =============================================================================
@router.get(
    "/runs/{run_id}",
    response_model=EvaluationRunRead,
    operation_id="getEvaluationRun",
)
async def get_evaluation_run(run_id: UUID, session: DbSession) -> EvaluationRunRead:
    """按主键查询评测批次。

    【前端怎么用】：创建 run 后按固定间隔轮询本接口，
    读 `progress_completed / progress_total` 渲染进度条；
    读 `status` 判断是否已 completed / failed。

    【异常契约】：run 不存在时，Service 层抛 `NotFoundError`，
    由全局异常处理器统一翻译成 404 + 标准 JSON，本层不做任何 if 判断。
    """
    service = EvaluationService(session)
    run = await service.get_run(run_id)
    return EvaluationRunRead.model_validate(run)


# =============================================================================
# 5. 删除评测 run（级联删除其下全部 case）
# =============================================================================
@router.delete(
    "/runs/{run_id}",
    status_code=204,
    operation_id="deleteEvaluationRun",
)
async def delete_evaluation_run(run_id: UUID, session: DbSession) -> Response:
    """物理删除评测批次及其全部用例明细。

    【为什么返回 `Response(status_code=204)` 而不是普通返回值】：
    204 No Content 按 HTTP 规范**不允许携带响应体**，
    所以这里必须显式返回一个空的 `Response`；
    同时装饰器上的 `status_code=204` 是给 OpenAPI 文档用的声明。

    【级联怎么发生的】：删除 run 时会连带抹掉 evaluation_items ——
    但**不是**应用层手写两遍 delete，而是靠数据库外键的 ON DELETE CASCADE
    （模型上配了 `passive_deletes=True`，让 SQLAlchemy 信任数据库端处理）。

    【为什么不做成幂等静默成功】：删一个不存在的 run 返回 404，
    是为了向前端**暴露状态不一致**（比如用户在两个标签页里重复删除）。
    """
    service = EvaluationService(session)
    await service.delete_run(run_id)
    return Response(status_code=204)


# =============================================================================
# 6. 某 run 下的 case 列表（支持只看 Bad Case / 按归因下钻）
# =============================================================================
@router.get(
    "/runs/{run_id}/items",
    response_model=EvaluationItemPage,
    operation_id="listEvaluationItems",
    summary="分页列出 run 下的 case。支持仅看 Bad Case 与按归因筛选",
)
async def list_evaluation_items(
    run_id: UUID,
    session: DbSession,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    # 质检开关：只看被判为 Bad Case 的用例
    bad_case_only: bool = Query(False),
    # 归因下钻：类型标注直接用 13 类 Literal，
    # 于是"非法分类值"由 FastAPI 自动拦成 422，路由体内一行校验都不用写。
    category: BadCaseCategoryValue | None = Query(None),
) -> EvaluationItemPage:
    """分页列出某批次下的用例明细。

    【路径为什么不会和 `/items/{item_id}` 撞车】：
    两者的模块前缀不同 —— 本接口是 `/runs/{run_id}/items`，
    另一个是 `/items/{item_id}`，第一段就区分开了，不依赖注册顺序。

    【语义隔离】：Service 层会先 `get_run(run_id)` 做卫哨，
    这样"run 不存在"返回 404，而"run 存在但没有匹配的 case"返回 200 + 空列表 —— 两种语义不会混淆。
    """
    service = EvaluationService(session)
    items, total = await service.list_items(
        run_id,
        page=page,
        page_size=page_size,
        bad_case_only=bad_case_only,
        category=category,
    )
    return EvaluationItemPage(
        items=[EvaluationItemRead.model_validate(i) for i in items],
        total=total,
        page=page,
        page_size=page_size,
    )


# =============================================================================
# 7. 单条 case 详情（抽检报告）
# =============================================================================
@router.get(
    "/items/{item_id}",
    response_model=EvaluationItemRead,
    operation_id="getEvaluationItem",
)
async def get_evaluation_item(item_id: UUID, session: DbSession) -> EvaluationItemRead:
    """按主键查询单条用例的完整体检明细。

    【返回了什么】：输入快照（标注）+ 实际输出（回答 / 引用）+ 白盒轨迹
    （query_route / agent_steps / verify_result / trace_id）+ 指标与归因 ——
    前端"抽检报告"弹窗的全部数据源。
    """
    service = EvaluationService(session)
    item = await service.get_item(item_id)
    return EvaluationItemRead.model_validate(item)


# =============================================================================
# 8. 人工覆盖 Bad Case 归因（PATCH，部分更新）
# =============================================================================
@router.patch(
    "/items/{item_id}",
    response_model=EvaluationItemRead,
    operation_id="updateEvaluationItem",
    summary="人工覆盖 Bad Case 归因 / 备注",
)
async def update_evaluation_item(
    item_id: UUID,
    payload: EvaluationItemUpdate,
    session: DbSession,
) -> EvaluationItemRead:
    """人工修正机器归因，或把误判的用例"平反"。

    【为什么用 PATCH 而不是 PUT】：
    这是**部分更新** —— 三个字段都可以不传。路由层原样透传给 Service，
    由 Service 实现"传 None = 本次不改"的语义；
    这里不做任何字段裁剪，避免把 PATCH 的语义悄悄改成 PUT。

    【三个字段的联动规则（实现在 Service 层）】：
    - `bad_case_category` 非空 → 自动把 `is_bad_case` 置 True（防前端漏传布尔值）；
    - `is_bad_case=False` → 标回非 Bad Case，**并联动清空** `bad_case_category`（防脏数据）；
    - 三者全 None → 两个字段都不动（只可能改到备注）。
    """
    service = EvaluationService(session)
    item = await service.update_item_bad_case(
        item_id,
        bad_case_category=payload.bad_case_category,
        bad_case_note=payload.bad_case_note,
        is_bad_case=payload.is_bad_case,
    )
    return EvaluationItemRead.model_validate(item)
