"""进程重启后的「孤儿任务」自愈模块。

【为什么需要它 —— 一天之内踩了三次的同一个坑】
本项目有三类长任务，而它们的「状态」与「执行者」分居两地：

    执行者                          状态载体（数据库）
    ────────────────────────────    ──────────────────────────────────────
    FastAPI 后台任务（评测）         evaluation_runs.status = running
    Celery worker（文档入库）        documents.status = parsing/uploading/indexing
                                    ingestion_tasks.status = pending/running

只要执行者所在的进程消失（`systemctl restart` / kill -9 / 服务器强制重启 / OOM 被杀），
数据库里那一行状态就会**永远停在「进行中」**：

    · 前端一直显示「执行中 50/50」「处理中」，永远不结束；
    · 「重试」按钮因为状态不是 failed 而不可用（仅失败态可重试）；
    · 用户唯一的出路是手工敲 SQL 改状态 —— 2026-09-29 那天为同一件事敲了三次。

本模块把这个动作变成**启动时的自动行为**。

【判据为什么是「启动时」而不是「超过 N 分钟」】
要判断一行是不是孤儿，本质是问「它的执行者还活着吗」。
进程刚启动的那一刻，这个问题有 100% 确定的答案：**执行者就是我自己，我刚起来，
所以任何仍处于进行中的行，它的执行者必然已经不存在了。**
反过来，如果做成"超过 30 分钟未更新就判失败"的定时清理，就必须猜一个超时阈值 ——
猜短了会误杀正在跑的长任务（本项目一次 50 题评测本来就要几十分钟），
猜长了又救不了急。启动时判定没有这个两难。

【代价与边界（必须说清楚）】
· 它假设「执行者 = 本进程」，这对评测（BackgroundTasks）完全成立；
· 对 Celery 入库任务，worker 是**独立进程**：如果只是 API 重启而 worker 仍在跑，
  这里会把正在执行的任务误判为失败。当前部署形态下 API 与 worker 由同一套
  systemd + 同一份代码管理、通常一起重启，且入库任务本身有 300 秒级的解析超时兜底，
  所以这个误判窗口很小、代价是"这份文档需要重传一次"。
  ⚠️ 将来若把评测也搬进 Celery（独立 worker），本模块需要改成按 worker 存活情况判定。
"""

from datetime import datetime, timezone

from sqlalchemy import update

from app.core.logging import get_logger
from app.db.models import (
    Document,
    DocumentStatus,
    EvaluationRun,
    EvaluationRunStatus,
    IngestionTask,
    IngestionTaskStatus,
)
from app.db.session import AsyncSessionLocal

logger = get_logger(__name__)

# 统一的失败原因文案：写进 error_message，让用户在界面上就知道"为什么失败了、要不要重试"
_ORPHAN_REASON = "服务重启导致任务中断（启动自愈已自动收敛，可直接重试）"


async def recover_orphans() -> dict[str, int]:
    """把上一个进程遗留的「进行中」记录收敛为失败态。

    :return: 各类记录的实际影响行数，形如
             {"evaluation_runs": 1, "ingestion_tasks": 2, "documents": 1}
    """
    now = datetime.now(timezone.utc)
    recovered: dict[str, int] = {}

    # 用独立短会话，一次性提交：三类状态要么一起收敛、要么一起不动，
    # 避免出现"评测改了、文档没改"的半吊子中间态。
    async with AsyncSessionLocal() as session:
        # ① 评测批次：执行者是 FastAPI 的 BackgroundTasks，随 API 进程一起消失
        result = await session.execute(
            update(EvaluationRun)
            .where(EvaluationRun.status == EvaluationRunStatus.RUNNING)
            .values(
                status=EvaluationRunStatus.FAILED,
                error_message=_ORPHAN_REASON,
                finished_at=now,
            )
        )
        recovered["evaluation_runs"] = result.rowcount or 0

        # ② 入库任务台账：worker 被强杀时来不及回写终态
        result = await session.execute(
            update(IngestionTask)
            .where(
                IngestionTask.status.in_(
                    [IngestionTaskStatus.PENDING, IngestionTaskStatus.RUNNING]
                )
            )
            .values(
                status=IngestionTaskStatus.FAILED,
                error_message=_ORPHAN_REASON,
                finished_at=now,
            )
        )
        recovered["ingestion_tasks"] = result.rowcount or 0

        # ③ 文档状态：必须一起收敛，否则前端永远显示"处理中"且「重试」按钮不可点
        result = await session.execute(
            update(Document)
            .where(
                Document.status.in_(
                    [
                        DocumentStatus.UPLOADING,
                        DocumentStatus.PARSING,
                        DocumentStatus.INDEXING,
                    ]
                )
            )
            .values(status=DocumentStatus.FAILED, error_message=_ORPHAN_REASON)
        )
        recovered["documents"] = result.rowcount or 0

        await session.commit()

    return recovered
