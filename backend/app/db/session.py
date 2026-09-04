"""异步数据库 engine / session 工厂。"""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

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