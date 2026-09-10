"""
【模块职责说明】
本模块为后端业务自定义异常定义层（Domain Exceptions），核心作用如下：

1. 统一领域错误抽象：
   定义业务异常基类 AppException，所有业务流程中主动预判并抛出的异常（如未查到数据、无权访问等）
   均必须继承此类，严禁随意直接抛出原生通用 Exception。

2. 错误三要素标准化：
   将每一个具体错误与【业务错误码 code】、【用户提示语 message】及【HTTP 响应状态码 http_status】
   强绑定，保障接口对外契约的自解释性与一致性。

3. 支持细粒度动态覆盖：
   既可通过子类定义预置默认提示与状态码，又允许开发者在业务触发处（raise 时）按需动态重写错误文本或错误码。
"""

from http import HTTPStatus


class AppException(Exception):
    """业务异常基类。

    所有业务层主动抛出的预期内错误均应继承此类，
    配合全局异常处理器统一格式化转换为 JSON HTTP 响应。
    """

    # 机器可读的错误业务码（前端通常据此分支处理逻辑）
    code: str = "internal_error"
    # 面向调用方的友好提示信息
    message: str = "服务内部错误"
    # 对齐的标准 HTTP 响应状态码（默认为 500）
    http_status: int = HTTPStatus.INTERNAL_SERVER_ERROR

    def __init__(
            self,
            message: str | None = None,
            *,
            code: str | None = None,
    ) -> None:
        """初始化业务异常。

        参数支持局部动态覆盖默认定义的 message 和 code；
        通过单个 '*' 强制要求后续的 code 必须作为关键字参数显式传入。
        """
        if message is not None:
            self.message = message
        if code is not None:
            self.code = code
        # 将错误信息传递给 Python 标准 Exception 基类，保障日志堆栈可读
        super().__init__(self.message)


class NotFoundError(AppException):
    """资源不存在异常（对应 HTTP 404）。"""

    code = "not_found"
    message = "资源不存在"
    http_status = HTTPStatus.NOT_FOUND


class PermissionDeniedError(AppException):
    """权限不足/无权访问异常（对应 HTTP 403）。"""

    code = "permission_denied"
    message = "无权访问该资源"
    http_status = HTTPStatus.FORBIDDEN


class ConfigurationError(AppException):
    """服务配置缺失或不可用异常（对应 HTTP 503）。"""

    code = "configuration_error"
    message = "服务配置缺失"
    http_status = HTTPStatus.SERVICE_UNAVAILABLE

class ValidationError(AppException):
    """请求参数校验失败异常（对应 HTTP 400）。"""

    code = "validation_error"
    message = "参数校验失败"
    http_status = HTTPStatus.BAD_REQUEST