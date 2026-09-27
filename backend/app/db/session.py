"""异步数据库 engine / session 工厂。"""

import asyncio
from collections.abc import AsyncIterator, Coroutine
from typing import Any, TypeVar

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

# 语法（泛型类型变量）：让 run_worker_coro 的返回类型与传入协程保持一致
T = TypeVar("T")

# 1. 创建异步数据库引擎与底层连接池
engine: AsyncEngine = create_async_engine(
    settings.database_url,
    # 控制是否打印生成的原生 SQL 语句到终端，生产环境通常关闭以避免日志污染
    echo=False,
    # 启用连接池连接健康心跳检测：从池中借出连接前先行校验，自动剔除断开的死连接
    pool_pre_ping=True,
)

# 2. 构造异步 Session 会话工厂
# 类似于数据库会话模板，后续每次需要操作数据库时均通过此工厂实例化独立 Session
AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    # 绑定关联的异步数据库引擎
    bind=engine,
    # 事务提交（commit）后不使实例对象的属性过期失效。
    # 避免在异步上下文中因访问对象属性而触发未经 await 的懒加载隐式 I/O，规避 MissingGreenlet 报错
    expire_on_commit=False,
    # 关闭自动刷新（flush），仅在显式执行 commit 或 flush 时同步 SQL，提升操作可控性
    autoflush=False,
)


# 3. FastAPI 依赖注入生成器函数
async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖: 提供请求级 AsyncSession。

    通过上下文管理器实现会话生命周期与单个 HTTP 请求严格对齐：
    - 请求到来时建立异步会话；
    - 执行控制器业务逻辑；
    - 业务结束或抛出异常时自动触发清理并归还连接池。
    """
    async with AsyncSessionLocal() as session:
        yield session


# =============================================================================
# 4. Celery worker 专用：常驻事件循环
# =============================================================================
# 【这一节解决的是第 12 期实测撞到的真实 BUG，不是预防性写法】
#
# 现象（worker 日志原文）：
#     第 1 个任务 finalize_upload 跑完 → asyncio.run 关闭它的事件循环
#         ERROR: Exception terminating connection <asyncpg...>
#                RuntimeError: Event loop is closed
#     第 2 个任务 ingest_document 一上来就炸：
#         AttributeError: 'NoneType' object has no attribute 'send'
#     结果：ingestion_tasks 永远停在 pending、documents.status 永远停在 uploading、
#           一个 chunk 都没写进去 —— 而接口调用方全程看到的是"成功"。
#
# 根因：两种"生命周期"的长度对不齐。
#   * 事件循环的生命周期 = 一个 Celery 任务。
#     因为 Celery 的任务函数必须是同步的，教程的写法是每个任务 asyncio.run 一次，
#     而 asyncio.run 的语义是"新建循环 → 跑完 → 把循环关掉"；
#   * 但被缓存的 I/O 资源生命周期 = 整个 worker 进程：
#       - SQLAlchemy 连接池是进程级、跨任务复用的（用完只"归还池中"，并不关闭）；
#       - app/ingestion/embedder.py 的 OpenAIEmbeddings 是模块级单例，
#         它内部的 httpx 连接池同样只在第一次用时创建一次。
#   于是：loop A 里建的连接/客户端被缓存下来，loop A 关闭；loop B 复用它们时，
#   底层 socket / proactor transport 绑在【已关闭的 loop A】上 → 直接崩。
#
# 【为什么这个 BUG 特别难查】
#   它不在第一次任务时出现。第一次任务里一切都是新建的，完全正常；
#   要等到第二次任务复用到缓存资源时才炸。排查时极易误判成
#   "第 2 个文档有问题""Celery 配置不对"或"网络抽风"。
#   实测中它连"看起来像网络错误"的假象都会造出来（httpx 走代理时尤其像）。
#
# 【修法：让事件循环活得和进程一样久】
#   与其在每个任务结束时去"逐个清理所有被缓存的 I/O 资源"
#   （数据库连接池 + 每一个 LLM/embedding 客户端 + 将来新加的客户端，漏一个就复发），
#   不如反过来：让事件循环常驻，资源与它的绑定关系就永远成立。
#   代价是本项目实测这种"单任务串行"的场景下没有任何代价 —— 反而省掉了每次
#   重建 TCP 连接与 TLS 握手的开销。
#
# 【并发前提：一个进程同一时刻只能跑一个任务】
#   常驻循环只对"单任务串行"成立。因此 worker 必须用
#       --pool=solo                        （推荐，Windows 上唯一无坑的选择）
#     或 --pool=threads --concurrency=1     （线程池但只开一个线程）
#   若真要用 --pool=threads --concurrency=N 让多任务并发，
#   则还需要在 worker 侧改用 NullPool 与"每个线程一个循环"，
#   因为并发任务会互相取到对方循环里的连接 —— 光靠常驻循环堵不住。
#   本项目的入库任务是长任务，并发收益远小于排查成本，故不采用。
_worker_loop: asyncio.AbstractEventLoop | None = None


def run_worker_coro(coro: Coroutine[Any, Any, T]) -> T:
    """Celery worker 的任务函数专用入口：在常驻事件循环里跑协程。

    :param coro: 待执行的协程对象
    :return: 协程的返回值
    """
    # 语法（Python 全局变量声明）：global _worker_loop
    #   作用：允许在函数内为模块级变量重新赋值（不加它就是创建一个同名局部变量）。
    global _worker_loop

    # 语法（循环可用性判定）：is None or is_closed()
    #   第一次调用时是 None；循环因故被关闭时需要重建。
    if _worker_loop is None or _worker_loop.is_closed():
        _worker_loop = asyncio.new_event_loop()
        # set_event_loop 把新循环登记为当前线程的默认循环 ——
        # 项目内若有代码调用 asyncio.get_event_loop()（而非显式传入 loop），拿到的才是这一个。
        asyncio.set_event_loop(_worker_loop)

    # 语法（在指定循环上运行协程直到完成）：loop.run_until_complete(coro)
    #   它【不会关闭】循环，这正是与 asyncio.run 的关键区别。
    return _worker_loop.run_until_complete(coro)