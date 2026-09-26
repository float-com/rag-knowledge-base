"""
【模块职责说明】
本模块为 FastAPI 应用的顶层主入口（Application Factory & Assembly）。
它的本质是整个后端系统的“总装配车间”，核心任务是按序加载各个独立组件并输出一个可运行的 ASGI 应用。

核心作用如下：
1. 采用工厂模式（create_app）：
   避免在导入模块时把所有依赖“写死”，方便在测试用例中按需多次创建独立隔离的 app 实例。
2. 串联系统启动流水线：
   日志配置 -> 跨域防护 -> 异常拦截 -> 业务路由挂载，确保底层基础设施工具有序就绪。
3. 暴露顶层 ASGI 实例：
   将实例化好的 app 对象提供给 Uvicorn/Gunicorn 服务器作为执行入口。
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# =============================================================================
# 第一步（必须最先执行）：注入 Hugging Face 运行环境变量
# =============================================================================
# huggingface_hub 在 import 阶段就会读取 HF_ENDPOINT / HF_HOME 并固化为模块常量，
# 之后再设置无效。因此这里必须排在 langchain / docling 等任何可能间接引入
# huggingface_hub 的导入之前 —— 哪怕只是把 import 语句挪到下面几行，都可能让镜像配置失效。
from app.core.hf_env import setup_hf_environment

setup_hf_environment()

# 导入全局异常捕获注册函数（负责捕获代码里的 AppException 和系统崩溃）
from app.api.error_handlers import register_error_handlers

# 语法（路由模块汇聚导入）：from app.api.routes import chat, documents, health
#   特性：从 API 路由层导入各业务子路由模块；原有 health 健康检查路由保留，增量引入 documents 与 chat 模块
#   通俗来讲：把刚写好的文档业务接待员（documents）与问答业务接待员（chat）请到总服务台前报到。
#   第 10 期增量：把评测业务接待员（evaluations）也请过来。
#   第 11 期增量：把认证业务接待员（auth）与用户 / 角色管理接待员（users / roles）也请过来。
from app.api.routes import (
    auth,
    chat,
    document_uploads,
    documents,
    evaluations,
    health,
    roles,
    users,
)

# 导入应用配置单例（包含从 .env 读取的应用名、CORS 允许源等）
from app.core.config import settings

# 导入日志系统：configure_logging 用于全系统日志格式化，get_logger 用于获取本模块打印器
from app.core.logging import configure_logging, get_logger

# 导入可观测性初始化：把 Settings 里的 LangSmith 配置同步写入 os.environ
from app.core.observability import configure_observability

# 导入种子数据初始化（第 11 期）：库内无用户时建好内置角色与默认管理员
from app.db.seed import seed_default_admin

# 应用生命周期钩子所需的类型与装饰器
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期钩子：启动时做种子初始化。

    【为什么种子初始化要放在 lifespan，而不是放在 create_app() 里】
    它是**异步**的（要查库、要写入），而 `create_app()` 是同步函数 ——
    放进去只能靠 asyncio.run 硬跑，会在"恰好在事件循环里调用 create_app"时直接报错。
    lifespan 天生运行在事件循环里，`await` 是自然的。

    【顺带解决了第 1 步留下的一个悬案】
    第 1 步加的 `settings.warn_if_jwt_unconfigured()` 一直**没有任何调用点**（死代码）。
    当时的问题是"该在哪调用它"：写在 `create_app()` 里不合适，因为那时全局日志
    还没配置完（configure_logging 在 create_app 内部才执行），告警会以默认格式输出。
    放在 lifespan 里就对了 —— 此时日志系统早已就绪，且服务还没开始接请求。

    【为什么初始化失败只记日志、不阻断启动】
    种子初始化失败只意味着"可能登不进去"，而不该让整个服务起不来 ——
    那样连 /docs 都看不了，反而更难排查。记下异常堆栈，修好后重启即可重试
    （本函数幂等，重启是安全的重试方式）。
    """
    logger = get_logger(__name__)

    # 密钥自检：缺失时给出响亮告警，但同样不阻断启动（详见 config.py 的取舍说明）
    settings.warn_if_jwt_unconfigured()

    try:
        await seed_default_admin()
    except Exception:
        logger.exception("种子初始化失败；后续可重新启动重试")

    # yield 之前是"启动完成前"的逻辑，之后是"关闭时"的逻辑。
    # 本项目没有需要优雅关闭的资源（连接池由 SQLAlchemy 自行管理），因此 yield 后为空。
    yield


def create_app() -> FastAPI:
    """应用工厂函数（Application Factory）。

    为什么不直接写 `app = FastAPI(...)`？
    - 解耦与灵活性：如果直接在顶层写死 app 变量，其他模块一旦 import 这个文件就会立即执行所有初始化代码；
    - 便于自动化测试：单元测试时，每个测试用例可以独立调用 create_app() 创建一个干净的环境，避免状态污染；
    - 统一管理装配顺序：集中控制日志、中间件、拦截器、路由的加载步骤。
    """

    # 第一步：初始化日志系统
    # 必须最先执行！因为后续的组件在启动报错时，需要依赖已经配置好的标准日志格式输出到控制台或文件
    configure_logging()

    # 第一步之二：初始化可观测性（第 9 期）
    # 【为什么必须紧跟在 configure_logging 之后、且早于任何业务模块被调用】：
    # LangSmith SDK 只读 os.environ（不读我们的 Settings 对象），而它内部对
    # 环境变量做了 lru_cache；一旦有业务代码先触发了 trace，再写 env 就晚了。
    # 放在这里，可保证进程内第一次 trace 之前环境变量已经就位。
    configure_observability()

    logger = get_logger(__name__)

    # 第二步：实例化 FastAPI 核心框架
    # title 会直接显示在自动生成的 Swagger API 文档（/docs）的页面正上方
    # lifespan：注册应用生命周期钩子（第 11 期新增），启动时执行种子数据初始化
    #   —— 库内无用户时自动建好内置角色与默认管理员，避免"没人能登录"的死锁。
    app = FastAPI(title=settings.app_name, lifespan=lifespan)

    # 第三步：装配跨域资源共享（CORS）中间件
    # 浏览器出于同源策略安全限制，会阻止前端（如 localhost:5173）向不同端口的后端发起 API 请求。
    # 挂载该中间件，FastAPI 会在每次请求的 HTTP 响应头中自动追加 Access-Control-Allow-* 标识。
    app.add_middleware(
        CORSMiddleware,
        # 允许跨域访问的前端域名白名单（在 config.py 中通过逗号分割解析出来的列表）
        allow_origins=settings.cors_origin_list,
        # 是否允许前端跨域携带 Cookie 或认证凭证（Token/Session），前后端分离登录必备
        allow_credentials=True,
        # 允许的 HTTP 方法：["*"] 代表允许 GET, POST, PUT, DELETE, OPTIONS 等所有请求
        allow_methods=["*"],
        # 允许前端在请求头中携带的自定义 Header 字段：["*"] 代表无限制（如 Authorization、Content-Type）
        allow_headers=["*"],
    )

    # 第四步：注册全局异常处理器（Exception Handlers）
    # 统一捕获代码中抛出的自定义 AppException（转成业务 JSON）和未知的 Python 语法/运行时原生 Bug（脱敏转 500）
    register_error_handlers(app)

    # 第五步：聚合挂载具体业务路由（Router）
    # health.router 内部声明的路径原本是 /health、/health/db、/health/cos
    # 这里加上 prefix="/api" 后，对外实际暴露的 URL 统一变为：
    # - GET /api/health
    # - GET /api/health/db
    # - GET /api/health/cos
    # 方便后续统一规划版本号与反向代理（如 Nginx 将所有 /api/* 转发给后端）
    app.include_router(health.router, prefix="/api")

    # 新增（业务路由级联挂载）：app.include_router(documents.router, prefix="/api")
    #   特性：将 documents.router 动态接入主应用。
    #   路径拼接公式：全局前缀 [/api] + 模块前缀 [/documents] + 接口子路径 [/{id} /file /chunks 等]
    #   核心收益：
    #     - 统一为接口加上版本/网关前缀 `/api`，方便 Nginx 反向代理与前后端分离部署；
    #     - 自动向 Swagger UI (/docs) 与 Redoc (/redoc) 注入文档模块的 8 大标准接口定义。
    #   通俗来讲：把文档接待窗口挂到总大厅的“/api”综合服务牌下，客人访问 /api/documents 就能办业务了。
    # 预签名上传路由必须先于 documents.router 注册，避免 /documents/{document_id}
    # 抢先匹配 /documents/uploads/... 并把 uploads 误当成 UUID 参数。
    app.include_router(document_uploads.router, prefix="/api")
    app.include_router(documents.router, prefix="/api")

    # 新增（问答路由级联挂载）：app.include_router(chat.router, prefix="/api")
    #   特性：将 chat.router 动态接入主应用。
    #   路径拼接公式：全局前缀 [/api] + 模块前缀 [/conversations] + 接口子路径 [/ /{id} /{id}/chat]
    #   核心收益：
    #     - 会话与流式问答统一挂在 /api/conversations 之下，便于 Nginx 反向代理统一转发；
    #     - 自动向 Swagger UI (/docs) 注入「创建会话 / 会话详情 / SSE 流式问答」三个端点。
    #   通俗来讲：把问答接待窗口挂到总大厅的“/api”综合服务牌下，客人访问 /api/conversations 就能办业务了。
    app.include_router(chat.router, prefix="/api")

    # 新增（评测路由级联挂载，第 10 期）：app.include_router(evaluations.router, prefix="/api")
    #   特性：将 evaluations.router 动态接入主应用。
    #   路径拼接公式：全局前缀 [/api] + 模块前缀 [/evaluations] + 接口子路径 [/datasets /runs ...]
    #   核心收益：
    #     - 评测批次与用例明细统一挂在 /api/evaluations 之下，便于 Nginx 反向代理统一转发；
    #     - 自动向 Swagger UI (/docs) 注入「评测集列表 / 创建 run / run 列表·详情·删除 /
    #       case 列表·详情 / 人工覆盖归因」8 个端点，并单独成组显示为 evaluations。
    #   通俗来讲：把评测接待窗口挂到总大厅的“/api”综合服务牌下，前端访问 /api/evaluations 就能办业务了。
    app.include_router(evaluations.router, prefix="/api")

    # 新增（认证路由级联挂载，第 11 期）：app.include_router(auth.router, prefix="/api")
    #   特性：将 auth.router 动态接入主应用。
    #   路径拼接公式：全局前缀 [/api] + 模块前缀 [/auth] + 接口子路径 [/login /me]
    #   核心收益：
    #     - 登录与「当前用户」统一挂在 /api/auth 之下；
    #     - 自动向 Swagger UI (/docs) 注入这两个端点，并单独成组显示为 auth。
    #   通俗来讲：把认证接待窗口挂到总大厅的“/api”综合服务牌下，前端访问 /api/auth/login 就能换令牌了。
    app.include_router(auth.router, prefix="/api")

    # 新增（用户与角色管理路由级联挂载，第 11 期第 7 章）：
    #   路径拼接公式：全局前缀 [/api] + 模块前缀 [/users 或 /roles] + 接口子路径
    #   核心收益：
    #     - 自动向 Swagger UI (/docs) 注入用户管理 5 个端点 + 角色管理 4 个端点，
    #       并分别单独成组显示为 users / roles；
    #     - 至此前端 sdk.gen.ts 里已写死的 12 个 operationId 全部兑现。
    #   注意：这两组路由**全部要求 CurrentAdmin**（管理面），普通用户访问会得到 403。
    app.include_router(users.router, prefix="/api")
    app.include_router(roles.router, prefix="/api")

    # 打印一条成功初始化的就绪日志，通知运维人员或开发者服务已装配完毕
    logger.info("app initialized: %s", settings.app_name)

    return app


# 第六步：正式创建暴露给外部服务器的 app 变量
# 当你在终端执行命令：uv run uvicorn app.main:app 时，
# Uvicorn 就会寻找此处的 app 变量作为 ASGI 入口进行启动监听
app = create_app()