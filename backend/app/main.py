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

# 导入全局异常捕获注册函数（负责捕获代码里的 AppException 和系统崩溃）
from app.api.error_handlers import register_error_handlers

# 导入拆分好的各个业务路由模块（这里先导入系统健康检查路由）
from app.api.routes import health

# 导入应用配置单例（包含从 .env 读取的应用名、CORS 允许源等）
from app.core.config import settings

# 导入日志系统：configure_logging 用于全系统日志格式化，get_logger 用于获取本模块打印器
from app.core.logging import configure_logging, get_logger


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
    logger = get_logger(__name__)

    # 第二步：实例化 FastAPI 核心框架
    # title 会直接显示在自动生成的 Swagger API 文档（/docs）的页面正上方
    app = FastAPI(title=settings.app_name)

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

    # 打印一条成功初始化的就绪日志，通知运维人员或开发者服务已装配完毕
    logger.info("app initialized: %s", settings.app_name)

    return app


# 第六步：正式创建暴露给外部服务器的 app 变量
# 当你在终端执行命令：uv run uvicorn app.main:app 时，
# Uvicorn 就会寻找此处的 app 变量作为 ASGI 入口进行启动监听
app = create_app()