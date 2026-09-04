"""
【模块职责说明】
本模块为 FastAPI 全局异常处理转换层（Exception Handlers），核心作用如下：

1. 统一异常转换与格式规范：
   拦截全系统抛出的异常，将非标准错误统一映射为结构化的标准 JSON 响应（包含 code 与 message 字段）。

2. 分级防御与脱敏兜底：
   - 业务异常分发：自动拦截继承自 AppException 的预期内业务错误（如 404、403 等），提取对应状态码并直接返回友好提示；
   - 未知异常兜底：底线捕获未经处理的系统原生崩溃或代码 Bug（Exception），在服务端后台完整记录调用堆栈，
     同时向客户端脱敏屏蔽内部实现细节，统一回退为 HTTP 500 错误，保障生产环境数据与架构安全。

3. 框架解耦与集中注册：
   通过 register_error_handlers 函数收敛所有异常监听器的挂载逻辑，便于在 main.py 启动生命周期中一键注册。
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.exceptions import AppException
from app.core.logging import get_logger

# 获取当前模块的专属 Logger 实例，便于追踪记录未捕获的严重系统错误
logger = get_logger(__name__)


async def _app_exception_handler(_: Request, exc: AppException) -> JSONResponse:
    """处理预期内的业务异常。

    提取 AppException 及其子类中约定的业务属性，
    将其转化为统一规范的 JSON 错误响应，避免产生冗余的服务端错误堆栈。
    """
    return JSONResponse(
        status_code=exc.http_status,
        content={
            "code": exc.code,
            "message": exc.message,
        },
    )


async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底捕获所有未预期的系统异常（如空指针、网络断开、代码语法逻辑错误等）。

    1. 在服务端日志中完整记录异常堆栈与发生该错误的 HTTP 请求动作及路径；
    2. 对客户端进行敏感信息脱敏，统一返回标准 HTTP 500 内部服务错误。
    """
    logger.exception("unhandled exception at %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "code": "internal_error",
            "message": "服务内部错误",
        },
    )


def register_error_handlers(app: FastAPI) -> None:
    """将全局异常处理器统一绑定注册到 FastAPI 实例上。

    在 main.py 应用初始化阶段调用一次即可。
    """
    # 捕获业务异常基类（其所有子类如 NotFoundError 均会自动命中此处理器）
    app.add_exception_handler(AppException, _app_exception_handler)  # type: ignore[arg-type]
    # 捕获 Python 通用根异常 Exception，实现系统级错误兜底
    app.add_exception_handler(Exception, _unhandled_exception_handler)