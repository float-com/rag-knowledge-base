from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import IngestionTask, IngestionTaskStatus, IngestionTaskType


class IngestionTaskRepository:
    """文档摄取与索引任务仓储（Repository）。

    【架构定位】
    纯粹的数据访问层（搬运工），负责 `ingestion_tasks` 表的新增、状态流转与查询，
    不包含 Celery 调度或文本切片等业务逻辑。

    【本仓储没有"删除"】
    任务记录随文档一起消亡：`ingestion_tasks.document_id` 上挂了 `ON DELETE CASCADE`，
    文档被删时数据库会级联清掉它的全部任务流水。
    这里再补一个 delete 方法反而会绕开那条约束、把清理入口分散到两处。

    【⚠️ 读本文件最该记住的一条约定：仓储层不 commit】
    下面除 create 会 flush 一次外，所有方法都【只改内存里的对象】，一行 commit 都没有。
    原因是"事务边界属于业务用例"—— 提交与回滚的时机应由外层编排决定
    （实际都封装在 pipeline.py 的 _mark_task / _mark_task_success 等辅助函数里）。
    直接后果，两条都要记住：
      · create 之后不 commit：任务行不会落库，但 `task.id` 已经可用（flush 过）；
      · mark_running / mark_success / mark_failed / set_progress_total /
        increment_progress 之后【必须由调用方 commit】，
        否则状态改动只活在内存里，函数一返回就没了 —— 而且不报任何错。
    """

    def __init__(self, session: AsyncSession) -> None:
        # 持有当前的异步数据库会话，供所有数据操作复用同一个事务上下文
        self.session = session

    async def create(
        self,
        document_id: UUID,
        task_type: IngestionTaskType,
    ) -> IngestionTask:
        """创建一条新的入库/重建索引任务（初始状态为 PENDING）。

        :param document_id: 关联的文档主键 UUID
        :param task_type: 任务类型枚举（INGEST 首次入库 / REINDEX 增量重建）
        :return: 刚创建、且已回填了默认字段的任务 ORM 实体
        """
        # 步骤 1：构造 ORM 实例，默认状态直接置为排队等待中（PENDING）
        task = IngestionTask(
            document_id=document_id,
            task_type=task_type,
            status=IngestionTaskStatus.PENDING,
        )
        # 步骤 2：加入当前会话跟踪列表
        self.session.add(task)
        # 步骤 3：flush 把 INSERT 真正下推给数据库，好让"默认值"被回填出来：
        #   · id         —— Python 侧 default=uuid4，在 flush 时生成
        #                   （本项目主键是 UUID，【不是自增】，表里没有任何自增列）
        #   · created_at —— 数据库侧 server_default=now()，由 INSERT 语句带回
        #   但【不 commit】：事务边界交给外层调用方（见类 docstring 里的约定）。
        await self.session.flush()
        return task

    async def get_latest_by_document(
        self, document_id: UUID
    ) -> IngestionTask | None:
        """详情页「最近一次任务」卡片用：取 created_at 最新一条。

        :param document_id: 目标文档的 UUID
        :return: 最新的任务实例，若该文档尚无任何任务记录则返回 None
        """
        # 步骤 1：构建带排序与限流的 SQL 查询语句
        #   📌 这条查询正是模型侧那个复合索引 ix_ingestion_tasks_document_created
        #      的服务对象：索引按 (document_id, created_at DESC) 建，
        #      顺序与下面的 WHERE + ORDER BY 完全对齐，可以直接沿索引取到第一条，
        #      不必再额外排序。（模型层为什么要显式声明这个索引，见 models.py 的注释。）
        stmt = (
            select(IngestionTask)
            # 按文档 ID 过滤
            .where(IngestionTask.document_id == document_id)
            # 按创建时间倒序排（最新的排在最前）
            .order_by(IngestionTask.created_at.desc())
            # 只取第一条
            .limit(1)
        )
        # 步骤 2：执行查询并用 scalar_one_or_none 提取对象（0 条返回 None，1 条返回解包后的对象）
        #
        # ⚠️ 这里【必须分两步写】：先把 execute 的结果 await 出来，再调用 .scalar_one_or_none()。
        #    写成一行 `await self.session.execute(stmt).scalar_one_or_none()` 是【错的】：
        #    Python 里 `.` 的优先级高于 `await`，那一行实际等价于
        #        await (self.session.execute(stmt).scalar_one_or_none())
        #    而 AsyncSession.execute() 是协程函数，返回的是一个【协程对象】，
        #    协程上根本没有 scalar_one_or_none 这个方法 → AttributeError。
        #    （这个坑在教程原文里就存在。它在第 12 期没被发现，唯一的原因是当时
        #      【没有任何调用方】—— 本方法到「增量索引」一节才第一次被真正调用。）
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def mark_running(self, task_id: UUID) -> None:
        """标记任务开始执行（状态流转为 RUNNING）。

        :param task_id: 目标任务的主键 UUID
        """
        # 步骤 1：通过主键快速获取任务实体（优先命中 session 内存缓存）
        task = await self.session.get(IngestionTask, task_id)
        if task is None:
            return
        # 步骤 2：更新状态为执行中，并记录当前标准 UTC 开始时间戳
        task.status = IngestionTaskStatus.RUNNING
        task.started_at = datetime.now(timezone.utc)

    async def mark_success(self, task_id: UUID) -> None:
        """标记任务执行圆满成功（状态流转为 SUCCESS）。

        :param task_id: 目标任务的主键 UUID
        """
        task = await self.session.get(IngestionTask, task_id)
        if task is None:
            return
        # 状态置为成功，记录结束时间，并清空历史错误信息（如果有）
        task.status = IngestionTaskStatus.SUCCESS
        task.finished_at = datetime.now(timezone.utc)
        task.error_message = None

    async def mark_failed(self, task_id: UUID, error_message: str) -> None:
        """标记任务失败并记录报错简讯（状态流转为 FAILED）。

        :param task_id: 目标任务的主键 UUID
        :param error_message: 异常捕获到的具体报错文本
        """
        task = await self.session.get(IngestionTask, task_id)
        if task is None:
            return
        # 状态置为失败，打上结束时间戳
        task.status = IngestionTaskStatus.FAILED
        task.finished_at = datetime.now(timezone.utc)
        # ⚠️ 截断到前 500 个字符。注意理由【与"数据库放不下"无关】：
        #   error_message 是 Text 类型，PostgreSQL 的 TEXT 没有长度上限（上限约 1GB），
        #   完整堆栈写进去既不会撑爆列、也不会导致写入报错。
        #   真正的理由是：这一列是要【展示给人看】的（前端详情卡片），
        #   完整堆栈动辄几十 KB，没人看得完，还会把响应体一起撑大；
        #   完整堆栈交给 logger.exception 落到日志里，数据库这列只留"简讯"。
        #   另：这里是【实际生效的那一道】—— pipeline._run_ingest 调用 _mark_task_failed 时
        #   传进来的是完整 message（只有写 documents.error_message 那一处才传了 [:500]）。
        task.error_message = error_message[:500]

    async def set_progress_total(self, task_id: UUID, total: int) -> None:
        """设置任务的总工作量（如总切片数 / 总分块数）。

        :param task_id: 目标任务的主键 UUID
        :param total: 本次切片划分出的总块数
        """
        task = await self.session.get(IngestionTask, task_id)
        if task is None:
            return
        # 记录切片总数，前端据此计算整体进度百分比（done / total）
        # ⚠️ 注意 total 可能是 0：模型上 progress_total 的默认值就是 0，
        #   而"文档不存在"之类的分支会在从未写过 total 的情况下直接把任务标成 failed。
        #   所以前端算百分比之前必须先判 total > 0，不能直接相除。
        task.progress_total = total

    async def increment_progress(self, task_id: UUID, delta: int) -> None:
        """增量推进任务完成进度（如向量化完成了 N 块）。

        :param task_id: 目标任务的主键 UUID
        :param delta: 本次批次增量完成的数量（如一批 10 个切片）
        """
        task = await self.session.get(IngestionTask, task_id)
        if task is None:
            return
        # 增量累加已完成的切片数
        task.progress_done += delta