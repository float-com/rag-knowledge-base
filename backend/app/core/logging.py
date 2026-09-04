"""统一日志配置：所有业务与框架模块通过 get_logger(__name__) 获取规范化 logger。"""

import logging
import sys

from app.core.config import settings

# 日志输出模板：时间 | 日志级别(固定占位8字符) | 模块名称 | 具体日志信息
_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
# 日志时间格式：年-月-日 时:分:秒
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 全局幂等标记位，防止日志系统被多次重复初始化导致日志重复打印
_configured = False


def configure_logging() -> None:
    """在应用启动时（如 FastAPI lifespan 事件）调用一次，具备幂等性。"""
    global _configured
    if _configured:
        return

    # 1. 创建控制台输出流处理器，输出至 sys.stdout（便于 Docker 容器日志驱动直接捕获）
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))

    # 2. 配置根日志记录器（Root Logger）
    root = logging.getLogger()
    # 清空可能存在的默认 handlers，避免重复输出
    root.handlers.clear()
    root.addHandler(handler)
    # 动态从配置中心读取日志级别（如 DEBUG, INFO, WARNING 等并转大写）
    root.setLevel(settings.log_level.upper())

    # 3. 统一接管 Uvicorn 内部日志器，收敛格式
    # uvicorn 自带独立的 handler 且格式不统一，清空其专属 handler 并设置向上冒泡传递
    for noisy in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(noisy).handlers.clear()
        logging.getLogger(noisy).propagate = True

    # 标记初始化完成
    _configured = True


def get_logger(name: str) -> logging.Logger:
    """获取指定模块命名的 Logger 实例。

    推荐在各业务模块顶部通过标准方式获取：
        logger = get_logger(__name__)
    """
    return logging.getLogger(name)