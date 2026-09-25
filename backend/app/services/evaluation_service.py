from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.db.models import EvaluationItem, EvaluationRun, EvaluationRunStatus
from app.db.repositories.evaluation_repo import (
    EvaluationItemRepository,
    EvaluationRunRepository,
)
from app.db.session import AsyncSessionLocal
from app.evaluation import (
    classify_bad_case,
    compute_citation_hit,
    compute_refusal_correct,
    load_dataset,
)
from app.evaluation.dataset import list_datasets
from app.evaluation.ragas_runner import RagasMetrics, RagasSample, evaluate_batch
from app.services.chat_service import ChatService, EvaluationAnswer

logger = get_logger(__name__)


# =========================================================================
# 6.1 EvaluationService —— 前端能调的同步动作
#     CRUD（创建 / 查询 / 删除 run、列出 items）+ Bad Case 归因 PATCH
#
# 注意：真正的"跑评测"是后台异步任务，不在这个类里 —— 见下方 6.2。
# =========================================================================
class EvaluationService:
    """评测业务动作服务（轻量同步业务层）：负责评测实体 CRUD、列表多维筛选及 Bad Case 人工介入修正。

    【核心架构职责与设计考量】：
    1. 读写分离与长短事务解耦：
       本服务只响应来自前端控制台的高频短请求（查看列表、创建元数据、人工修正分类）；
       耗时较长、并发量大的“大模型实际跑测与打分流”由 FastAPI BackgroundTasks
       在【同进程内】异步接管（见下方 6.2），严禁在此阻塞 HTTP 请求。
    2. 领域仓储聚合（Repository Pattern）：
       同时持有并协调 EvaluationRunRepository（批次）与 EvaluationItemRepository（单题用例），
       由 Service 统一把控 SQLAlchemy 事务提交（commit）边界。
    """

    def __init__(self, session: AsyncSession) -> None:
        """注入请求级异步数据库会话，并初始化对应仓储实例。

        :param session: 由 FastAPI 依赖注入框架提供的请求生命周期 AsyncSession
        """
        self.session = session
        self.run_repo = EvaluationRunRepository(session)
        self.item_repo = EvaluationItemRepository(session)

    async def create_run(self, *, name: str, dataset_name: str) -> EvaluationRun:
        """初始化一条评测运行批次记录（EvaluationRun），确立待跑任务元数据。

        【防御性设计 - 强校验防空跑】：
        在落库前必须先在内存中真实加载指定评测集（load_dataset）。两种情况都会被挡在落库之前，
        防止在数据库中生成大小为 0 的垃圾占位记录：
        - 评测集【文件缺失】：load_dataset 内部抛 NotFoundError（→ HTTP 404）；
        - 评测集【内容为空】：由本方法的 `if not cases` 兜住并抛 ValidationError（→ HTTP 400）。

        :param name: 本次评测的自定义名称（如 "RAG_V2.0_全量回归测试"）
        :param dataset_name: 挂载的离线评测集标识（如 "qa_gold_standard_v1"）
        :return: 已落库并刷新时间戳的 EvaluationRun 实体对象
        """
        # ---------------------------------------------------------------------
        # 步骤 1：前置加载评测集（先验检查，防止向数据库插入无意义的空跑批次）
        # ---------------------------------------------------------------------
        # 调底层的 load_dataset 把指定名字的评测文件读到内存中（本项目只支持 .jsonl）
        cases = load_dataset(dataset_name)

        # 边界守卫：文件不存在的情况 load_dataset 内部已经抛 NotFoundError 了，走不到这里；
        # 这里只兜「文件存在，但一条有效数据都没有」（例如整份文件全是空行）
        if not cases:
            # 立即中断并抛出 400 参数校验异常，绝不继续往下走：
            # 避免数据库凭空多出一条 dataset_size=0、没法跑也没法算百分比的僵尸记录
            raise ValidationError(f"评测集 {dataset_name} 为空")

        # ---------------------------------------------------------------------
        # 步骤 2：构造评测批次（Run）的主表实体对象
        # ---------------------------------------------------------------------
        run = EvaluationRun(
            # 本次评测的自定义名称（供前端列表与看板展示）
            name=name,
            # 引用的评测集标识文件名
            dataset_name=dataset_name,
            # 总题数：基于实际读出来的 cases 数量赋值
            dataset_size=len(cases),
            # 状态初始化：建好即代表即将进入执行，置为「运行中」
            status=EvaluationRunStatus.RUNNING,
            # 进度条分母：前端计算进度百分比用的基准（已完成数 / progress_total）
            progress_total=len(cases),
        )

        # ---------------------------------------------------------------------
        # 步骤 3：数据持久化与字段回填（严格遵循仓储与事务分层规范）
        # ---------------------------------------------------------------------
        # 1. 仓储层只做 add（写入 SQLAlchemy 内存一级缓存），不负责 commit：
        #    把事务的提交权留给当前 Service 业务层统一掌控
        await self.run_repo.add(run)

        # 2. 真正向 PostgreSQL 发送 INSERT 语句并落盘提交事务
        await self.session.commit()

        # 3. 重新向数据库拉取最新快照（关键避坑点）：
        #    - run.id：来自 models.py 的 default=uuid4，由【Python 端】在 flush 时生成
        #      （不是自增，也不是数据库 server_default）；
        #    - run.created_at：来自 server_default=func.now()，由【数据库端】生成。
        #    两者在刚 new 出来的 Python 对象上都是 None，执行 refresh 才能拿到真实值，
        #    保证最后 return 给前端的 JSON 响应里带有完整的 id 和创建时间。
        await self.session.refresh(run)

        return run

    async def get_run(self, run_id: UUID) -> EvaluationRun:
        """按主键查询评测批次，不存在时显式抛出 404 业务异常。"""
        # 1. 委托仓储层按 UUID 主键到数据库查询 EvaluationRun 实体记录：
        #    实现是 session.get()：先查 Identity Map（一级缓存），命中就直接返回内存对象；
        #    未命中才会发出 `SELECT * FROM evaluation_runs WHERE id = :run_id`
        run = await self.run_repo.get(run_id)

        # 2. 空值校验与业务异常收敛（防 None 穿透）：
        #    - 若数据库没有匹配记录，run 的结果为 None；
        #    - 这里绝不向外返回 None，而是统一抛出领域自定义的 NotFoundError；
        #    - 外层 FastAPI 全局异常处理器（Exception Handler）捕获到 NotFoundError 后，
        #      会自动将其转换为标准 HTTP 404 状态码和规整的 JSON 错误体下发给前端。
        if run is None:
            raise NotFoundError("评测 run 不存在")

        # 3. 校验通过，安全交付非空的持久化实体对象
        return run

    async def list_runs(
        self, page: int, page_size: int
    ) -> tuple[list[EvaluationRun], int]:
        """分页拉取评测批次列表，按创建时间倒序呈现。

        【为什么返回值设计为二元元组 tuple[list[EvaluationRun], int]】：
        1. 契合标准分页协议：前端分页表格不仅需要拿到「当前页的数据明细（list）」，
           还需要知道「全量总记录数（total: int）」，用以计算总页数并渲染底部的分页页码栏。
        2. 事务与读优化：本操作属于纯读取（SELECT），无需开启写事务也不用 commit，
           直接委托仓储层底层执行 `LIMIT :page_size OFFSET :offset` 及 `COUNT(*)` 查询后透传返回。

        :param page: 目标页码（从 1 开始计）
        :param page_size: 单页最大返回条目数
        :return: (当前页的实体对象列表, 数据库中匹配该条件的总记录数 total)
        """
        # 直接透传委托仓储层完成多参数分页 SQL 查询，保持 Service 层只做编排、不写 SQL
        return await self.run_repo.list_page(page=page, page_size=page_size)

    async def delete_run(self, run_id: UUID) -> None:
        """物理级联删除评测批次及其所属的所有 Case 明细记录。

        【设计考量与排坑细节】：
        1. 数据库外键级联（ON DELETE CASCADE）：
           删除主表 evaluation_runs 的记录时，数据库底层会自动将外键关联的
           所有 evaluation_items（单题明细）一并物理抹除，无需应用层手写两遍 delete。
        2. 显式 404 语义 vs 幂等静默成功：
           - 有些系统会认为“删除一个本就不存在的东西也算删完”，返回 200/204；
           - 但在此处必须通过 `if not deleted:` 抛 404：这是为了向前端强硬暴露状态不一致的 Bug
             （例如前端缓存未刷新、用户在另外的标签页重复删除），防范脏数据与无效请求。
        3. 事务边界掌控（Service Commit 原则）：
           仓储层（run_repo.delete）内部走的是 ORM 对象级删除 `session.delete(entity)` + `flush()`，
           **不是** Core 的 `execute(delete(stmt))` —— 只有这样，模型上声明的
           `cascade="all, delete-orphan"` / `passive_deletes=True` 才会把级联真正交给数据库执行。
           真正的提交落盘统一由当前 Service 层的 `await self.session.commit()` 闭环。

        :param run_id: 待删除的目标评测批次主键 UUID
        :raises NotFoundError: 当数据库未受影响（即该 run_id 不存在）时抛出
        """
        # 1. 委托仓储层执行删除 SQL，deleted 返回布尔值（表示底层受到影响的行数是否 > 0）
        deleted = await self.run_repo.delete(run_id)

        # 2. 状态检查：如果受影响行数为 0，说明主键不存在，立即阻断并抛出领域 404 异常
        if not deleted:
            raise NotFoundError("评测 run 不存在")

        # 3. 确认删除成功，向数据库发送 COMMIT 提交事务，物理落地删除操作
        await self.session.commit()

    async def list_items(
        self,
        run_id: UUID,
        page: int,
        page_size: int,
        *,
        bad_case_only: bool,
        category: str | None,
    ) -> tuple[list[EvaluationItem], int]:
        """多维组合筛选并分页拉取指定评测批次下的单条 Case 详情。

        【业务价值与排坑细节】：
        1. 语义隔离与防歧义（先查父表再查子表）：
           - 若直接拿 run_id 去查 evaluation_items 表，查无数据时只会返回空列表 `[]`；
           - 这样前端无法区分是「run_id 传错导致 404」，还是「批次存在但符合筛选条件的 Case 为 0 条（200 + 空列表）」；
           - 故在入口处显式 `await self.get_run(run_id)` 守卫拦截，确保父级批次必然存在。
        2. 多维组合筛选（动态 SQL 委托）：
           - bad_case_only: 专注于质检排查，快速捞出模型幻觉、拒答、检索遗漏的不及格 Bad Case；
           - category: 精细化归因下钻（如只看 `generation_off_context` 幻觉类、
             或 `embedding_recall_miss` 检索漏召回类）；
           - 仓储层用 filters 列表动态拼装（AND 关系）：run_id 是恒定条件；
             `bad_case_only=True` 时追加 `is_bad_case IS TRUE`；
             `category` 非空时追加 `bad_case_category = :category`（与 bad_case_only 相互独立）。
        3. 强制关键字传参（`*` 语法防御）：
           - bad_case_only 与 category 为筛选维度参数，强制调用方显式写出键名（如 `bad_case_only=True`），
             彻底避免开发时把布尔值或分类字符串传错位置导致查出非预期数据。

        :param run_id: 所属评测批次的主键 UUID
        :param page: 目标分页页码（从 1 开始计）
        :param page_size: 单页最大返回条目数
        :param bad_case_only: 是否仅筛选被标记为不合格的 Bad Case
        :param category: 按归因分类筛选（如 "generation_off_context"、"embedding_recall_miss"），传 None 则不限制
        :return: (当前页的 EvaluationItem 实体列表, 匹配过滤条件的总记录数 total)
        """
        # 1. 显式前置检查父批次存在性：若 run 不存在，内部会直接抛出 NotFoundError(404) 截断
        await self.get_run(run_id)

        # 2. 校验通过，安全委托仓储层执行携带动态条件的 LIMIT/OFFSET 与 COUNT 分页查询
        return await self.item_repo.list_page(
            run_id,
            page=page,
            page_size=page_size,
            bad_case_only=bad_case_only,
            category=category,
        )

    async def get_item(self, item_id: UUID) -> EvaluationItem:
        """按主键查询单条评测 Case 的完整体检明细（包含指标、决策路径与归因）。

        【业务价值与设计细节】：
        1. 细粒度白盒归因底座：
           - 该接口返回的 EvaluationItem 包含了单道题的完整画像：原始问答、RAGAS 各项打分（忠实度/相关性）、
             检索命中的切片原文、多轮 Agent 思考轨迹（agent_steps）以及后置幻觉校验详情；
           - 前端点击某条用例查看“抽检报告”弹窗时，核心依赖此接口获取数据。
        2. 严格的空值收敛与 404 契约：
           - 避免把底层的 None 向上泄露给 Controller 或 API 路由层；
           - 统一转换为领域异常 NotFoundError，保证对外暴露规整的 HTTP 404 状态与标准错误 JSON。

        :param item_id: 待查询评测明细用例的主键 UUID
        :return: 数据库持久化实体 EvaluationItem 对象
        :raises NotFoundError: 当根据 item_id 未查到任何记录时抛出
        """
        # 1. 委托 EvaluationItem 仓储层按主键 UUID 检索数据库实体
        item = await self.item_repo.get(item_id)

        # 2. 防御性空值判定：查无此记录时显式抛出领域 404 异常，截断后续链路
        if item is None:
            raise NotFoundError("评测 case 不存在")

        # 3. 校验通过，安全交付非空的完整用例实体对象
        return item

    async def update_item_bad_case(
        self,
        item_id: UUID,
        *,
        bad_case_category: str | None,
        bad_case_note: str | None,
        is_bad_case: bool | None,
    ) -> EvaluationItem:
        """前端控制台用于人工复核与纠偏 Bad Case 的状态更新接口（PATCH 语义）。

        【业务背景与设计细节】：
        1. 自动化评测的误判兜底（Human-in-the-Loop）：
           - RAGAS 等自动化大模型打分机制存在主观偏差，可能把优质回答误判为低分；
           - 本方法为运营或质检人员提供人工覆盖入口，支持把误判用例“平反”或补充人工归因。
        2. 智能联动的状态机流转（防御脏数据）：
           - 状态互斥联动：若标记为非 Bad Case（is_bad_case=False），归因原因必须强制抹平清空为 None，
             绝不允许数据库中出现「is_bad_case=False 但 category="generation_off_context"」的矛盾脏数据；
           - 容错隐式升级：若操作者直接选择了归因类型（bad_case_category 不为空），系统默认判定其为问题用例，
             自动将 is_bad_case 纠偏升为 True，避免前端因为漏传布尔值导致分类未生效。
        3. 强制关键字传参（* 语法）：
           - 避免调用方混淆 bad_case_category（字符串）与 bad_case_note（备注字符串）的位置顺序。

        :param item_id: 待复核的评测明细用例主键 UUID
        :param bad_case_category: 人工指派的归因分类（如 "embedding_recall_miss"、"generation_off_context"；
                                  取值必须落在 scoring.py 的 `BadCaseCategory` 那 13 类之内）
        :param bad_case_note: 人工复核备注（说明为什么这样修改，沉淀团队知识）
        :param is_bad_case: 是否界定为 Bad Case（True/False，传 None 表示本次不修改该状态）
        :return: 数据库更新落盘并刷新后的最新 EvaluationItem 实体对象
        """
        # ---------------------------------------------------------------------
        # 步骤 1：查询目标用例并锁定上下文
        # ---------------------------------------------------------------------
        # 内部会先查数据库，若 item_id 不存在直接抛 NotFoundError(404) 截断，
        # 确保拿到的一定是持久化上下文中的非空实体。
        item = await self.get_item(item_id)

        # ---------------------------------------------------------------------
        # 步骤 2：状态机分支判定（处理误判抹除与隐式升级）
        # ---------------------------------------------------------------------
        # 显式使用 `is False` 判定：区分“明确要取消 Bad Case（False）”与“没传该字段（None）”
        if is_bad_case is False:
            # 【平反分支】：人工判定此题合格
            item.is_bad_case = False
            # 必须联动清空归因类型，杜绝“合格用例身上挂着幻觉标签”的矛盾数据
            item.bad_case_category = None
        else:
            # 【确诊与分类分支】：
            if bad_case_category is not None:
                # 容错处理：只要前端传递了具体的归因原因，自动联动将 is_bad_case 确认为 True
                item.bad_case_category = bad_case_category
                item.is_bad_case = True
            elif is_bad_case is True:
                # 仅将用例标为不合格，保留原有的 category（或等待后续再细分归因）
                item.is_bad_case = True

        # ---------------------------------------------------------------------
        # 步骤 3：人工备注按需局部更新（PATCH 语义）
        # ---------------------------------------------------------------------
        # 只有在显式传递了备注（非 None）时才执行覆盖，传 None 则保留数据库中的历史备注不变
        if bad_case_note is not None:
            item.bad_case_note = bad_case_note

        # ---------------------------------------------------------------------
        # 步骤 4：事务持久化提交与实体回显刷新
        # ---------------------------------------------------------------------
        # 1. 向 PostgreSQL 发送 UPDATE 语句并提交当前会话事务
        await self.session.commit()

        # 2. 执行 refresh 重新从数据库拉取该行，确认落库结果确实就是刚写入的值。
        #    注意：EvaluationItem 【没有】 updated_at 列，它唯一的数据库生成列是 created_at
        #    （server_default，且 UPDATE 不会改动它），所以这里的 refresh 是"确认"而非"回填"。
        await self.session.refresh(item)

        return item

    def list_datasets(self) -> list[tuple[str, int]]:
        """纯只读工具方法：列出当前工程中所有可供选用的离线评测集及其包含的测试用例数量。

        :return: 形如 [("seed", 50), ("smoke", 5)] 的列表（本项目当前的两个评测集）
        """
        return list_datasets()


# =========================================================================
# 6.2 执行器主流程 —— BackgroundTasks.add_task 排队的评测任务入口
#
# 一次 run 分两个阶段：先逐条跑 RAG（边跑边落 item），再批量跑 RAGAS 与聚合。
# 外层 try/except 兜底：任意阶段抛异常都把 run 标记成 failed，
# 绝不留一个永远是 running 状态的僵尸任务。
# =========================================================================
async def execute_evaluation_run(run_id: UUID) -> None:
    """评测批次（Run）核心后台异步执行器：由 FastAPI BackgroundTasks 接管并排队执行。

    【核心流程体系】：
    1. 激活阶段：开启短会话标记 started_at 时间戳，加载测试集题目列表；
    2. 阶段 1（逐题推理）：边跑 RAG 边落盘单条 Item（保证前端进度条实时跳动）；
    3. 阶段 2（批量评测与聚合）：调用 RAGAS 计算各项自动化指标，完成 Bad Case 归因与平均分聚合；
    4. 终态收尾：标记 COMPLETED 与 finished_at；若中途崩盘则强保 FAILED，杜绝僵尸状态。

    :param run_id: 待执行的评测批次主键 UUID
    """
    logger.info("evaluation run start: run_id=%s", run_id)
    try:
        # ---------------------------------------------------------------------
        # 步骤 1：初始化任务状态与记录启动时间戳（短数据库会话）
        # ---------------------------------------------------------------------
        # 采用独立上下文 async with 开启短连接，用完立即释放，绝不在耗时的 LLM 推理期间霸占连接
        async with AsyncSessionLocal() as session:
            run_repo = EvaluationRunRepository(session)
            run = await run_repo.get(run_id)

            # 防御性校验：若外部调度前任务已被误删，打印警告并安全退出
            if run is None:
                logger.warning("evaluation run not found, skip: %s", run_id)
                return

            # -----------------------------------------------------------------
            # 记录正式开始处理的绝对 UTC 时间戳（打上任务启动的精准时刻印记）
            # -----------------------------------------------------------------
            # 【为什么必须用 datetime.now(timezone.utc) 而不用 datetime.utcnow()？】
            # 1. 业务目的：
            #    - 记录该评测批次实际开始跑题目的起始时间，供前端控制台展示“开始时间”，
            #      后续任务收尾时结合 finished_at 精准计算出全量跑批的总耗时。
            # 2. 技术与规范考量（Python 3.12+ 强规范）：
            #    - 传统的 datetime.utcnow() 在 Python 3.12+ 中已被官方标记为废弃（Deprecated）；
            #    - utcnow() 返回的是“无时区感知（Naive）”的时间对象（tzinfo=None），直接写进
            #      PostgreSQL 的 TIMESTAMPTZ 列容易被误当成本地时间叠加二次时区偏移（导致差 8 小时）；
            #    - datetime.now(timezone.utc) 显式挂载零时区元数据（Aware Datetime，带 +00:00），
            #      确保无论后端部署在哪个机房或本地物理时钟如何变动，数据库落库的时间戳全局绝对统一。
            run.started_at = datetime.now(timezone.utc)
            await session.commit()
            # 提前把评测集名字取出来暂存，让后续流程彻底不依赖 session 与 ORM 对象。
            # 【理由要说准】：本项目 AsyncSessionLocal 配了 expire_on_commit=False
            #   （见 app/db/session.py），且 dataset_name 是普通列而非 relationship，
            #   即便出了会话再读它也不会报错、更不会触发懒加载；
            #   提前取是为了"意图清晰 + 不依赖 session 的过期策略"，不是被迫的。
            dataset_name = run.dataset_name

        # 从磁盘全量读入评测集（List[TestCase]）
        cases = load_dataset(dataset_name)

        # ---------------------------------------------------------------------
        # 步骤 2：阶段 1 —— 逐条执行 RAG 问答推理与进度实时上报
        # ---------------------------------------------------------------------
        # 逐个用例循环调用，每个 case 内部都是独立事务 INSERT 一行 evaluation_items，
        # 并在同一事务里更新 run 的进度计数器：
        #   progress_completed 无条件 +1（= 已跑完总数，【含失败】），
        #   失败时再【额外】+1 progress_failed（它是 completed 的【子集】，不是互斥计数）。
        # 前端只轮询 run 这一行即可（EvaluationListPage / EvaluationDetailPage 读的都是
        #   progress_completed / progress_total），不需要去 COUNT evaluation_items。
        for case in cases:
            await _run_single_case(run_id, case)

        # ---------------------------------------------------------------------
        # 步骤 3：阶段 2 —— 批量指标评测（RAGAS Batch）与 Bad Case 归因聚合
        # ---------------------------------------------------------------------
        # 此时所有题目已产出答案和切片，在此处批量调用 RAGAS 评估器打分；
        # 随后由第 3 节的 classify_bad_case 按【四级短路漏斗】归因
        #   （L0 异常 -> L1 拒答 -> L2 引用 -> L3 RAGAS 四指标，命中即停，并非"比一个阈值"）；
        # 最后把各指标均值与命中率聚合回填到 run 主表
        await _finalize_run(run_id)

        # ---------------------------------------------------------------------
        # 步骤 4：正常收尾 —— 标记任务成功与完成时刻（短数据库会话）
        # ---------------------------------------------------------------------
        async with AsyncSessionLocal() as session:
            run = await EvaluationRunRepository(session).get(run_id)
            if run is not None:
                # 状态跃迁至完成终态
                run.status = EvaluationRunStatus.COMPLETED
                # 记录任务全链路跑完的结束时间
                run.finished_at = datetime.now(timezone.utc)
                await session.commit()
        logger.info("evaluation run done: run_id=%s", run_id)

    except Exception as exc:
        # ---------------------------------------------------------------------
        # 步骤 5：全局异常兜底与防僵尸保护（Anti-Zombie Guard）
        # ---------------------------------------------------------------------
        # 打印完整异常堆栈，便于定位大模型欠费、断网或代码致命缺陷
        logger.exception("evaluation run failed: run_id=%s", run_id)

        # 无论发生什么致命错误，必须重新拉起短会话将任务打标为 FAILED：
        # 彻底避免后台任务静默退出导致前端页面永远卡在 RUNNING / 进度 99% 的“僵尸任务”
        async with AsyncSessionLocal() as session:
            run = await EvaluationRunRepository(session).get(run_id)
            if run is not None:
                # 状态跃迁至失败终态
                run.status = EvaluationRunStatus.FAILED
                # 记录崩溃时间戳
                run.finished_at = datetime.now(timezone.utc)
                # 【双重防御与主动截断】：
                # - 优先取异常信息，若 str 为空则回退为类名（如 "RuntimeError"）；
                # - [:500] 是【主动】截断，**不是**数据库约束 —— 该列在 models.py 里是 Text
                #   （PostgreSQL 的 Text 无长度上限），截断的目的是别把超长堆栈塞进接口响应与前端展示。
                run.error_message = (str(exc).strip() or exc.__class__.__name__)[:500]
                await session.commit()

# =========================================================================
# 6.3 单条 case 与最终聚合 —— 上面执行器的两个阶段
# =========================================================================
async def _run_single_case(run_id: UUID, case) -> None:
    """跑一条 case：利用会话上下文驱动 RAG，随后复用会话完成单条 Item 落库与进度更新。

    【核心流程与会话流转设计】：
    1. 驱动问答沙箱：用一个 async with 包住 RAG 调用，跑完 answer_for_evaluation 立即退出。
       【实测澄清】answer_for_evaluation 全程不碰 self.session（第 5 步已验证），
       所以这个块里【不会发出任何 SQL】—— 既不借连接（pool.checkedout=0），
       也不开事务（in_transaction=False）。它的真正价值是"防御性外壳"：
       一旦将来该方法开始使用 session，连接生命周期已经被这个上下文管理器正确管住了。
    2. 本地规则比对：在 Python 内存中零开销计算引用命中（citation_hit）与拒答正确性（refusal_correct）；
    3. 数据落库与进度累加：再次复用该 session 对象（SQLAlchemy 会自动触发 autobegin 开启全新写事务），
       重新 get 出处于新事务受管状态的 run 对象，将 item 插入与进度自增原子 commit 提交。

    :param run_id: 所属评测批次主键 UUID
    :param case: 从评测集加载出的单条测试用例对象（包含 question, expected_answer 等）
    """
    # -------------------------------------------------------------------------
    # 步骤 1：驱动 RAG 推理链路（短会话沙箱）
    # -------------------------------------------------------------------------
    # 只创建一个会话对象（注意：连接是【懒加载】的，此刻尚未借用连接、也未开启事务），
    # 它在这里的作用是满足 ChatService(session) 的构造签名
    async with AsyncSessionLocal() as session:
        chat_service = ChatService(session)
        # 执行完整的非流式评测 RAG 问答，捕获不可变的 EvaluationAnswer 结果快照。
        # 该方法内部自建短会话去跑检索，与这里的 session 无关 —— 所以本块不发 SQL。
        answer: EvaluationAnswer = await chat_service.answer_for_evaluation(case.question)
    # 退出块：会话 close()。因为全程没发过 SQL，此处【没有连接或事务需要释放】；
    # 真正的"借连接 -> 开事务 -> 提交归还"发生在下面的步骤 4。

    # -------------------------------------------------------------------------
    # 步骤 2：本地规则计算轻量指标（纯 Python 内存比对，无需调 LLM）
    # -------------------------------------------------------------------------
    # 1. 计算引用文档命中情况（citation_hit）：
    #    - 若用例是【超纲 / 知识库无依据】题（case.should_refuse=True），本就不该出引用，赋为 None；
    #    - 若是正向题，比对实际引用的文档名与关键词是否命中测试集标注的黄金标准（expected_document_names）
    #
    # 【已知口径取舍 · 教程原文如此，本项目暂不改行为】
    #   这里只排除了"本就该拒答"的用例，【没有】排除 answer.error_message 非空的降级用例。
    #   后果链：
    #     ① 降级用例的 answer.citations 是空列表（EvaluationAnswer 的默认值）；
    #     ② compute_citation_hit 对空引用直接 `return False`（scoring.py 的前置边界防御）；
    #     ③ 于是它拿到的是 False 而不是 None —— 在 _finalize_run 里
    #        `if i.citation_hit is not None` 拦不住它，它进了 citation_hit_rate 的分母、被算作"未命中"。
    #   影响：一批 5 条里 1 条因网络中断降级 → 命中率被算成 4/5 = 0.8（本应 4/4 = 1.0），
    #        把基础设施故障混进了"检索质量"指标。
    #   而且同一 item 的 bad_case_category 会被正确归为 other（is_error=True 走 Level 0 短路），
    #       于是【同一行数据里两个字段口径不一致】：一个说"系统故障"，一个说"检索漏召回"。
    #   若要修，一个条件即可：  None if case.should_refuse or answer.error_message else ...
    #       （与第 3 节「None = 不适用，不是表现差」的哲学一致；等第 7 步 API 收口时再决定）
    citation_hit = (
        None
        if case.should_refuse
        else compute_citation_hit(
            actual_citations=answer.citations,
            expected_document_names=case.expected_document_names,
            expected_keywords=case.expected_keywords,
        )
    )

    # 2. 计算拒答判定正确性（refusal_correct）：
    #    - 比对实际模型行为（answer.refused）与测试集预期行为（case.should_refuse）；
    #    - 该拒答而拒答 = True（合格），不该拒答却拒答 = False（误杀），该拒答却硬答 = False（漏判/幻觉）
    refusal_correct = compute_refusal_correct(answer.refused, case.should_refuse)

    # -------------------------------------------------------------------------
    # 步骤 3：装配单题评测结果实体（EvaluationItem）
    # -------------------------------------------------------------------------
    item = EvaluationItem(
        # 外键关联与用例标识
        run_id=run_id,
        case_id=case.case_id,
        # 评测用例原题与标注真值（Ground Truth）
        question=case.question,
        expected_answer=case.expected_answer,
        expected_document_names=case.expected_document_names,
        expected_keywords=case.expected_keywords,
        should_refuse=case.should_refuse,
        tags=case.tags,
        # RAG 系统的实际输出与状态快照
        actual_answer=answer.answer,
        actual_refused=answer.refused,
        # 结构化引用角标列表
        citations=answer.citations,
        # 检索切片摘要（过滤冗余字段，便于前端在抽检报告中回溯召回现场）
        retrieved_chunks_meta=[_chunk_meta(c) for c in answer.chunks],
        # 白盒链路排查元数据：意图路由决策与 Agent 多轮思考轨迹
        query_route=answer.query_route or None,
        agent_steps=answer.agent_steps or None,
        # 后置反幻觉校验结果
        verify_result=_verify_payload(answer.verify_result),
        # LangSmith 链路追踪根 ID
        trace_id=answer.trace_id,
        # 性能画像：端到端延迟与首字到达耗时（TTFT）
        latency_ms=answer.latency_ms,
        first_token_latency_ms=answer.first_token_latency_ms,
        # 异常隔离容错记录（若子任务出错，保留报错描述）
        error_message=answer.error_message,
        # 本地规则计算得出的两项确定性指标
        citation_hit=citation_hit,
        refusal_correct=refusal_correct,
    )

    # -------------------------------------------------------------------------
    # 步骤 4：持久化单题结果并原子更新批次进度（复用会话开启全新写事务）
    # -------------------------------------------------------------------------
    # 利用 SQLAlchemy 特性：已 close 的 session 执行后续操作会自动重新借用连接（autobegin）；
    # 1. 挂入新生成的单题 Item 实体
    await EvaluationItemRepository(session).add(item)

    # 2. 重新从数据库 get 出当前 run 实体：
    #    重新查出的 run 会被纳入当前这个新事务的 Identity Map 监控追踪中
    run = await EvaluationRunRepository(session).get(run_id)
    if run is not None:
        # 已完成题目计数累加 1（前端轮询此字段以实时渲染百分比进度条）
        run.progress_completed += 1
        # 若本题产生未捕获异常，失败计数器同步累加 1
        if answer.error_message:
            run.progress_failed += 1

    # 3. 提交新事务：将 item 写入与进度自增原子落地，释放行锁与连接
    await session.commit()


async def _finalize_run(run_id: UUID) -> None:
    """跑完所有 case 后，一次性跑 RAGAS + Bad Case 归因 + 聚合指标。

    【核心阶段与算法架构】：
    1. 数据全量预载：拉取本批次已落盘的所有 EvaluationItem 用例；
    2. 样本过滤与下标对齐（Index-Preserving Filter）：
       - 排除拒答题（should_refuse）、报错题（error_message）以及切片为空的用例，避免无效的大模型调用；
       - 通过带原数组下标的元组 (idx, RagasSample) 记录位置，确保批量评测后能精准回填，位置不跑偏；
    3. 批量 RAGAS 指标评测（Batch Evaluation）：调用底层 evaluate_batch 计算 4 项经典指标；
    4. Bad Case 自动分类归因：结合规则指标与大模型打分，对每道题进行质量定性与分类标签下发；
    5. 全局聚合指标计算：过滤空值（None），统计全批次的宏平均分、命中率与整体延迟。
    """
    async with AsyncSessionLocal() as session:
        item_repo = EvaluationItemRepository(session)
        # 1. 查询当前 run 下已持久化的所有测试用例明细
        items = await item_repo.list_by_run(run_id)
        # 防空检查：若无用例直接退出，避免除零等空指针异常
        if not items:
            return

        # ---------------------------------------------------------------------
        # 步骤 1：过滤无效样本并构建带原始下标的 RAGAS 评测输入
        # ---------------------------------------------------------------------
        # 为什么要过滤（三个判据、三个不同理由）：
        # - should_refuse=True：该题的标准答案就是"不回答"，而 RAGAS 四指标的前提是"该有答案"，
        #   对它不适用；而且第 3 节的漏斗在 Level 1 就会因 refusal_correct 短路，RAGAS 分数根本用不上。
        #   ⚠️ 注意【不是】因为拒答题没有切片 —— 实测拒答题照样有 chunks（越界题拿到 20 条）。
        # - error_message 非空：RAG 链路已降级，actual_answer 是空串，没有可评的内容。
        # - retrieved_chunks_meta 为空：这一条【才是】真的没有参考切片。
        # 三者的共同后果：送给 LLM 评测只会浪费 Token 并引发评测器报错。
        # 为什么带 idx（带标保序）：
        # - 过滤后有效样本数量会少于原始 items 长度；记录 (idx, sample) 才能在打分后精确插回原位置。
        samples_with_index: list[tuple[int, RagasSample]] = []
        for idx, item in enumerate(items):
            if item.should_refuse or item.error_message or not item.retrieved_chunks_meta:
                continue
            samples_with_index.append(
                (
                    idx,
                    RagasSample(
                        question=item.question,
                        answer=item.actual_answer,
                        # 从检索元数据字典中提取切片文本，转换为标准 contexts 字符串列表
                        retrieved_contexts=[
                            str(c.get("content", "")) for c in item.retrieved_chunks_meta
                        ],
                        reference_answer=item.expected_answer,
                    ),
                )
            )

        # ---------------------------------------------------------------------
        # 步骤 2：执行 RAGAS 批量评测与指标回填对齐
        # ---------------------------------------------------------------------
        # 初始化一个与 items 等长的全 None 列表，为每条用例预留指标槽位
        metrics_list: list[RagasMetrics | None] = [None] * len(items)
        if samples_with_index:
            # 剥离原数组下标，仅提取 RagasSample 列表，一次性并发/批量调用 RAGAS 评测器
            indexed_metrics = await evaluate_batch([s for _, s in samples_with_index])
            # 利用 zip(..., strict=True) 将评测输出与原始下标精确匹配，填入对应的槽位
            for (idx, _), m in zip(samples_with_index, indexed_metrics, strict=True):
                metrics_list[idx] = m

        # ---------------------------------------------------------------------
        # 步骤 3：单题指标回填与 Bad Case 自动化规则归因
        # ---------------------------------------------------------------------
        for item, metrics in zip(items, metrics_list, strict=True):
            # 若该用例参与了 RAGAS 评测并拿到了有效打分，回填 4 项核心质量指标
            if metrics is not None:
                item.faithfulness = metrics.faithfulness              # 忠实度（反幻觉度量）
                item.answer_relevancy = metrics.answer_relevancy        # 答案相关性
                item.context_precision = metrics.context_precision    # 上下文精确率（排序优劣）
                item.context_recall = metrics.context_recall          # 上下文召回率（信息全不全）

            # 执行多维度规则判定：输入确定性指标与大模型评分，自动识别问题并划分归因类别
            # （返回值取值必须落在 scoring.py 的 BadCaseCategory 那 13 类里，
            #   如 generation_off_context 幻觉、embedding_recall_miss 检索漏召回、
            #   context_judge_too_strict 误拒答、other 运行硬故障 等）
            rule = classify_bad_case(
                should_refuse=item.should_refuse,
                actual_refused=item.actual_refused,
                refusal_correct=item.refusal_correct,
                citation_hit=item.citation_hit,
                faithfulness=item.faithfulness,
                answer_relevancy=item.answer_relevancy,
                context_precision=item.context_precision,
                context_recall=item.context_recall,
                is_error=bool(item.error_message),
            )
            # 回写用例的质量判定结果，供前端控制台一键过滤与排查
            item.is_bad_case = rule.is_bad_case
            item.bad_case_category = rule.category

        # ---------------------------------------------------------------------
        # 步骤 4：主表全局宏平均指标聚合计算（Run-Level Aggregation）
        # ---------------------------------------------------------------------
        # 重新获取持久化主表实体对象
        run = await EvaluationRunRepository(session).get(run_id)
        if run is not None:
            # 1. RAGAS 四项指标聚合：_avg 内部自动剔除 None 项，只对有打分的有效样本求算术平均
            run.faithfulness = _avg([i.faithfulness for i in items])
            run.answer_relevancy = _avg([i.answer_relevancy for i in items])
            run.context_precision = _avg([i.context_precision for i in items])
            run.context_recall = _avg([i.context_recall for i in items])

            # 2. 引用命中率（Citation Hit Rate）：
            #    - 分母严格排除本就该拒答的用例（citation_hit 为 None 的项），
            #      确保只统计真正需要引用文档的有效回答场景；
            non_refusal_hits = [i.citation_hit for i in items if i.citation_hit is not None]
            run.citation_hit_rate = (
                sum(1 for h in non_refusal_hits if h) / len(non_refusal_hits)
                if non_refusal_hits
                else None
            )

            # 3. 拒答准确率（Refusal Accuracy）：全量用例中拒答决策判定正确的比例
            run.refusal_accuracy = sum(1 for i in items if i.refusal_correct) / len(items)

            # 4. 平均全链路延迟（毫秒）：所有用例总耗时算术平均
            run.avg_latency_ms = sum(i.latency_ms for i in items) / len(items)

            # 5. 平均首字延迟（TTFT 毫秒）：
            #    - 拒答或异常用例的 first_token_latency_ms 为 None，必须过滤后求均值；
            #    - 若全量用例均未调 LLM（极端全拒答/全报错），安全回退为 None。
            first_token_values = [
                i.first_token_latency_ms for i in items if i.first_token_latency_ms is not None
            ]
            run.avg_first_token_latency_ms = (
                sum(first_token_values) / len(first_token_values)
                if first_token_values
                else None
            )

        # ---------------------------------------------------------------------
        # 步骤 5：提交事务
        # ---------------------------------------------------------------------
        # 单事务原子提交：同时持久化所有 item 的 RAGAS 指标/Bad Case 标签，以及 run 主表的全局聚合均分
        await session.commit()


def _chunk_meta(chunk) -> dict:
    """把 RetrievedChunk 实体序列化为存储在 evaluation_items.retrieved_chunks_meta 中的轻量字典。

    【字段取舍与业务价值】：
    1. 为 RAGAS 提供上下文底座：
       - 保留完整的 `content` 字段，后续批处理提取 `[c["content"] for c in retrieved_chunks_meta]`
         作为 RagasSample.retrieved_contexts，用以计算忠实度（faithfulness）和上下文召回率（context_recall）；
    2. 为前端抽检报告提供溯源依据：
       - `document_name`、`page_no`、`section_path` 让质检人员在抽检 Bad Case 时能一眼看出引自哪篇文档哪一页；
    3. 为检索策略调优保留白盒排查分值：
       - 保留 `vector_score`、`rerank_score`、`rrf_score`，便于分析切片是因为初检排序靠后、还是精排重塑被挤掉的。
       - ⚠️ 但 RetrievedChunk 上的 `sources` / `vector_rank` / `keyword_rank`【没有】落库。
         将来若要排查"是不是 RRF 融合把某条好切片挤掉了"，rank 比 score 更直接，
         届时需要在这里补上这三个字段。
    4. 类型安全（UUID 字符串化）：
       - 主键 ID 显式 `str(...)` 强转，防止直接落库或做 JSON 序列化时触发 UUID 对象的编码异常。
    """
    return {
        # 强制将 UUID 转为标准字符串，保证后续 JSON/JSONB 序列化兼容性
        "chunk_id": str(chunk.chunk_id),
        "document_id": str(chunk.document_id),
        # 文档元数据快照（文档名、页码、章节层级路径），避免原文档若被物理删除后无法溯源
        "document_name": chunk.document_name,
        "page_no": chunk.page_no,
        "section_path": chunk.section_path,
        # 核心切片文本：必须保留，供 RAGAS 评估器比对模型回答是否脱缰幻觉
        "content": chunk.content,
        # 检索各环节分数切片，供模型工程师下钻分析召回打分分布
        "vector_score": chunk.vector_score,
        "rerank_score": chunk.rerank_score,
        "rrf_score": chunk.rrf_score,
    }


def _verify_payload(result) -> dict | None:
    """把 VerifyResult 校验实体转换为可持久化入库的 JSON 字典。

    【空值与空串规范化（Normalization）】：
    1. 短路防护：若 result 为 None（说明走的是拒答路径或未开启答案校验），直接返回 None，
       数据库 JSONB 列如实存为 NULL；
    2. 空串转 None（语义净化）：
       - `result.reason or None`：当校验通过（verified=True）时，reason 往往是空字符串 `""`；
       - 空字符串在 JSON 里没有有效信息量，将其统一归一化为 None（对应 JSON 的 null），
         方便前端判断“当前无需展示拦截原因”，杜绝界面渲染空气泡或多余占位。
    """
    # 守卫判断：未执行校验时保持 None 穿透
    if result is None:
        return None

    return {
        # 布尔标记：是否通过事实一致性核验（True 为通过，False 为幻觉拦截）
        "verified": result.verified,
        # 拦截理由：空字符串转换为 None，仅在存在有效失败理由时透出文本
        "reason": result.reason or None,
    }


def _avg(values: list[float | None]) -> float | None:
    """计算含 None 浮点数列表的有效算术平均值（防除零与空值安全聚合）。

    【算法与防御性设计】：
    1. 动态滤空（None Filtering）：
       - 自动化打分（如 RAGAS）在遇到样本拒答、报错跳过时，对应字段为 None；
       - 通过列表推导式提取所有非 None 的有效打分 `nums`，保证聚合基准分母的准确性；
    2. 防除零安全返回（ZeroDivision Defense）：
       - 若整个批次没有任何有效打分（例如整个测试集全是超纲拒答题、或有切片的全崩了），
         此时 `len(nums) == 0`；
       - 提前 `if not nums:` 拦截并安全返回 None，彻底杜绝底层抛出 `ZeroDivisionError`
         导致整个聚合汇总事务被熔断。
    """
    # 1. 过滤掉所有缺失值与未打分占位项（保留纯有效浮点数）
    nums = [v for v in values if v is not None]

    # 2. 边界检查：若没有一个样本具备有效分数，安全回退为 None（对应数据库主表字段为 NULL）
    if not nums:
        return None

    # 3. 正常计算算术平均值
    return sum(nums) / len(nums)
