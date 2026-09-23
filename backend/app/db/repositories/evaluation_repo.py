"""评测与 Bad Case 分析数据仓储层（第 10 期）。

【模块架构定位与设计哲学】：
1. 仓储模式（Repository Pattern）的职责边界：
   - 屏蔽底层持久化实现细节：将 SQLAlchemy 的 Select/Insert 语句拼装、多条件 Filter 过滤、
     排序稳定性兜底以及分页偏移量（Limit/Offset）计算等 SQL 细节完全封锁在仓储内部；
   - 对外提供纯粹面向领域实体的交互接口（Domain Interface），使 Service 层只需关注评测状态流转、
     指标调度与 Bad Case 研判等核心业务逻辑，实现持久化与业务逻辑解耦。

2. 工作单元与事务边界的严格契约（Unit of Work）：
   - 【核心法则】：仓储层内部**只执行 `flush()`，严禁调用 `commit()` 或 `rollback()`**；
   - 【原理解析】：
     * `flush()` 会将当前 Session 内存中的变化转换成 SQL（INSERT/UPDATE/DELETE）发送给 PostgreSQL 数据库，
       使数据库分配自增主键、生成默认值（如 default=uuid4、server_default=now()），同时触发数据库层面的
       外键与非空约束检查，但**不结束当前物理事务**；
     * `commit()` 则会持久化落盘并关闭事务。若仓储内擅自 commit，将破坏外层的长链事务原子性；
   - 【评测场景特殊性】：
     后台 Worker 评估评测集时，采用的是“逐条 Case 评测 -> flush 落库 -> 外层 Session 手动 Commit”的模式。
     若一次性整体 commit，长达数分钟的评测期间前端轮询查到的进度将一直是 0；
     若仓储私自 commit，则多表联动或指标聚合阶段发生异常时，外层将无法实施事务回滚。

3. 读写口径的分治隔离（Batch Processing vs. UI Pagination）：
   - `list_by_run()`（全量顺序批量）：
     供评测集全量跑完后，批量拉取快照送入 RAGAS 框架计算 Faithfulness、Answer Relevancy 等指标。
     该场景要求“样本绝对完整”且“物理顺序严格一致”，严禁分页截断；
   - `list_page()`（分页条件投影）：
     供前端管理控制台展示与排查 Bad Case。支持多种过滤维度（如只看异常用例、按归因类别下钻），
     核心要求是“低延迟、小吞吐、分页绝对稳定”。
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import EvaluationItem, EvaluationRun


class EvaluationRunRepository:
    """评测执行总表（evaluation_runs）仓储。

    【核心职责】：
    负责管理 Run 维度的物理持久化生命周期：包括创建评测任务实例、按主键点查任务上下文、
    控制台全局评测任务分页列表查询，以及级联级删除操作。
    """

    def __init__(self, session: AsyncSession) -> None:
        """注入异步数据库会话（AsyncSession）。

        Session 的生命周期通常由外部依赖注入（如 FastAPI Depends 或 Celery Task Session Context）维护。
        """
        self.session = session

    async def add(self, run: EvaluationRun) -> None:
        """新增一轮评测执行记录（写入 evaluation_runs 表）。

        【执行行为】：
        1. `self.session.add(run)`：将新创建的 Run 实例纳入当前 Session 的 Identity Map 管理；
        2. `await self.session.flush()`：立即向数据库下发 INSERT 语句，使数据库层面的默认值（如 uuid4
           主键、created_at 默认时间戳）被成功赋值并回填到 Python 实体对象的属性中。

        【事务安全提醒】：
        此时并未向数据库发出 COMMIT 指令，该行数据处于当前数据库事务的活动隔离区中，
        直至调用该方法的上层业务显式执行 `await session.commit()`，其他数据库连接才能读到该记录。
        """
        self.session.add(run)
        await self.session.flush()

    async def get(self, run_id: UUID) -> EvaluationRun | None:
        """按主键 UUID 点查单轮评测任务的完整信息。

        【设计考量】：
        - 利用 SQLAlchemy 的 `session.get()` 内置优化机制：若当前 Session 缓存中已加载该实体，直接走内存命中；
        - 若未命中则执行 `SELECT ... WHERE evaluation_runs.id = :run_id`；
        - 记录不存在时规范化返回 `None`，不抛出异常。是否转译为 HTTP 404 由上层 API Service 决策。
        """
        return await self.session.get(EvaluationRun, run_id)

    async def list_page(
        self,
        page: int,
        page_size: int,
    ) -> tuple[list[EvaluationRun], int]:
        """分页获取所有评测任务列表，并返回匹配的总记录数。

        【防御性设计 - 参数强制钳制（Parameter Clamping）】：
        1. `page = max(page, 1)`：物理屏蔽非法的 0 或负数页码，防止出现负 offset 导致 SQL 语法解析崩溃；
        2. `page_size = max(min(page_size, 100), 1)`：将单页拉取上限封顶为 100，
           即使恶意调用方传入 `page_size=10000` 也不会拖垮数据库连接池与服务内存；
        3. 即使上层 Pydantic Schema 已定义参数校验，仓储层也必须自主实现防线，
           因为此方法后续可能被数据修复脚本、离线调度 Worker 直接调用。

        【排序与分页确定性（Deterministic Ordering）】：
        - 采用 `order_by(created_at.desc(), id.desc())` 复合排序：
        - 为什么单用 `created_at.desc()` 不够安全？
          高并发批量创建场景下，完全可能产生纳秒级相同的 `created_at`。
          若排序列不具备全局唯一性，数据库（PostgreSQL）在分页时无法保证游标顺序，
          用户在翻页时会出现数据项“漏看”或“重复看到”的幽灵分页现象；引入唯一物理主键 `id` 兜底可彻底杜绝该问题。

        :return: (当前页的 Run 实体列表, evaluation_runs 表总记录数)
        """
        page = max(page, 1)
        page_size = max(min(page_size, 100), 1)
        offset = (page - 1) * page_size

        # 1. 组装数据项分页查询语句
        stmt = (
            select(EvaluationRun)
            .order_by(EvaluationRun.created_at.desc(), EvaluationRun.id.desc())
            .limit(page_size)
            .offset(offset)
        )
        items = list((await self.session.execute(stmt)).scalars().all())

        # 2. 组装全局总数统计语句
        total = int(
            (await self.session.execute(select(func.count(EvaluationRun.id)))).scalar_one()
        )
        return items, total

    async def delete(self, run_id: UUID) -> bool:
        """根据主键物理删除一轮评测执行。

        【级联删除机制剖析（Cascading Architecture）】：
        - 为什么不需要在此方法中手动编写 `DELETE FROM evaluation_items WHERE run_id = ...`？
          1. 物理外键约束保障：数据表外键定义了 `ondelete="CASCADE"`，PostgreSQL 底层引擎会在删除父记录时，
             利用其外键索引以极高效率级联抹除挂在它下面的所有子行（evaluation_items）；
          2. ORM 层级协同：模型关系声明了 `cascade="all, delete-orphan", passive_deletes=True`。
             `passive_deletes=True` 指示 SQLAlchemy 信任数据库端的级联能力，
             严禁在 Python 内存中把成百上千条子项先 SELECT 出来再发多条单点 DELETE，从而规避了内存暴涨和性能骤降。

        :return: True 表示确实存在并执行了删除标记；False 表示对应 Run 不存在，无需操作。
        """
        run = await self.get(run_id)
        if run is None:
            return False
        await self.session.delete(run)
        await self.session.flush()
        return True


class EvaluationItemRepository:
    """评测明细表（evaluation_items）仓储。

    【核心职责】：
    负责管理单条 Case 粒度（Prompt、上下文快照、实际生成结果、RAGAS 四维打分、Bad Case 归因）
    的高吞吐持久化、批量检索与多维筛选分页。
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def bulk_add(self, items: Sequence[EvaluationItem]) -> None:
        """批量写入多条用例明细实体。

        【前置防护与语义约定】：
        - 若传入的 `items` 为空集合，立即提前返回（Short-circuit）。
          虽然调用 `session.add_all([])` 在语法上不会报错，但在上层业务流中，“批量写入 0 条数据”
          往往意味着评测集解析异常或调度空转，尽早拦截能避免无效的空操作以及后续白白触发数据库 flush 往返开销。
        - 内部执行 `flush()`，使整批实体的默认属性在一次网络往返（Round-trip）中完成数据库同步。
        """
        if not items:
            return
        self.session.add_all(items)
        await self.session.flush()

    async def add(self, item: EvaluationItem) -> None:
        """单条新增评测明细。

        【使用场景】：
        后台异步评测任务逐个 Case 运行时调用。
        每执行完一个 Case，将其状态通过此方法 flush 到数据库；
        外层服务捕获成功后即可独立 commit，驱动前端进度条实时跳动（+1）。
        """
        self.session.add(item)
        await self.session.flush()

    async def get(self, item_id: UUID) -> EvaluationItem | None:
        """按主键获取单条评测明细详情。

        主要供人工介入复核（Human-in-the-loop）场景使用，例如前端点击单个 Bad Case 弹窗，
        读取其完整上下文切片（retrieved_chunks_meta）或提交人工修正笔记（bad_case_note）。
        """
        return await self.session.get(EvaluationItem, item_id)

    async def list_by_run(self, run_id: UUID) -> list[EvaluationItem]:
        """获取指定评测任务（run_id）名下的【全部】测试用例明细。

        【为什么坚决不分页（No Pagination by Design）】：
        1. 业务消费者是自动化评估器：该方法主要供后台 Worker 收集完整样本，组装成 Dataset 喂入 RAGAS，
           指标计算（如 Context Recall、Faithfulness）依赖全局完整的数据集，少一页会导致整体均值严重失真；
        2. 数据量可控：单轮评测集规模通常在数十至数百条之间，Python 内存完全可以安全承载其实体列表；
        3. 顺序一致性：严格按照 `created_at ASC, id ASC` 正序返回，确保与原始评测集 JSONL 的声明顺序
           完全吻合，方便研发按用例编号从头到尾进行基准对齐。
        """
        stmt = (
            select(EvaluationItem)
            .where(EvaluationItem.run_id == run_id)
            .order_by(EvaluationItem.created_at.asc(), EvaluationItem.id.asc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_page(
        self,
        run_id: UUID,
        page: int,
        page_size: int,
        *,
        bad_case_only: bool = False,
        category: str | None = None,
    ) -> tuple[list[EvaluationItem], int]:
        """分页检索指定 Run 下的 Case 明细，支持针对 Bad Case 的多维交叉下钻过滤。

        【过滤条件组合逻辑（AND 关系）】：
        - 基础约束：`run_id == run_id` 构筑数据隔离底线，严禁跨任务串数据；
        - 条件 1：`bad_case_only=True` 时追加 `is_bad_case IS TRUE` 约束，用于研发人员只关注劣质 Case；
        - 条件 2：`category` 存在时追加 `bad_case_category == category` 约束，
          用于在劣质 Case 中进一步按归因定位（例如：专攻“检索召回缺失”或专攻“模型幻觉”）；
        - 多条件通过 `filters` 列表收集，在 SQL 中以 `AND` 关系叠加。

        【统计与数据严格一致性（Consistency Rule）】：
        - 数据语句（stmt）与总数计数语句（total_stmt）必须**强共享同一套 `filters` 列表**；
        - 如果统计总数漏掉了 category 条件，前端计算出的总页数就会偏大，导致用户翻到末尾页时出现大面积空白，
          破坏用户交互体验。

        :return: (当前页的 Case 明细实体列表, 满足当前过滤条件的总用例数)
        """
        # 参数边界防御性收敛
        page = max(page, 1)
        page_size = max(min(page_size, 100), 1)
        offset = (page - 1) * page_size

        # 动态组装 Filter 谓词链
        filters = [EvaluationItem.run_id == run_id]
        if bad_case_only:
            filters.append(EvaluationItem.is_bad_case.is_(True))
        if category:
            filters.append(EvaluationItem.bad_case_category == category)

        # 1. 查询当前分页下的实际数据切片（稳定排序正序返回）
        stmt = (
            select(EvaluationItem)
            .where(*filters)
            .order_by(EvaluationItem.created_at.asc(), EvaluationItem.id.asc())
            .limit(page_size)
            .offset(offset)
        )
        items = list((await self.session.execute(stmt)).scalars().all())

        # 2. 精确计算在相同过滤条件下的全局匹配总条数
        total_stmt = select(func.count(EvaluationItem.id)).where(*filters)
        total = int((await self.session.execute(total_stmt)).scalar_one())

        return items, total