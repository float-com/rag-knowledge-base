"""LangSmith 可观测性集成模块。

【设计思路：隔离底层细节】：
将所有与 LangSmith SDK 强相关的脏活（改写系统环境变量、从运行上下文抓 ID、拼接 URL 等）
全部收敛在当前文件中。业务代码只需引用 `@traceable` 装饰函数，或调用 `get_current_trace_id`、
`build_trace_url` 两个简单函数即可，无需在各处散落第三方 SDK 复杂的内部细节。

【为什么启动时要将 Settings 显式写回 os.environ】：
1. 机制不同：Pydantic Settings 只负责从 .env 加载并保存在 Python 对象内存中，默认不会写回操作系统。
2. SDK 限制：LangSmith SDK 内部完全通过 `utils.get_env_var()` 检索系统环境变量（如 LANGSMITH_*），
   它根本无法直接访问我们自定义的 Settings 实例。
   因此，应用启动时必须调用 `configure_observability()` 完成一次“从 Settings 到 os.environ”的同步。

【不可侵入原则（加分项不能变成阻断项）】：
可观测性属于辅助诊断能力，绝不能反客为主。未配置或即使上报挂掉，问答主链路也必须正常运行：
1. 若未开启，显式将 `LANGSMITH_TRACING` 设为 "false"，杜绝 SDK 隐式默认行为，让所有跟踪降级为无害透传（no-op）；
2. 内部所有异常一律兜底捕获并返回 None，杜绝因监控 SDK 的问题导致业务报错。
"""

import os

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def configure_observability() -> None:
    """在应用启动时同步环境变量，单次调用即可。

    【调用时序非常关键（避开 lru_cache 陷阱）】：
    LangSmith 底层用 `lru_cache` 缓存环境变量读取结果
    （见依赖源码 `langsmith/utils.py` 的 `get_env_var` 装饰器），
    而该缓存发生在**某个变量第一次被读取时**，并非模块导入时就一次性定死。
    也就是说：一旦有业务代码先触发了一次 trace，这个变量就被固定为旧值，
    之后再往 os.environ 里写也读不到了。
    因此该函数必须紧随日志初始化、并且在**任何会触发 trace 的业务代码之前**执行。
    """
    if settings.observability_enabled:
        # 开启状态：一次性灌入密钥、项目与网关。任一缺少都会导致无法鉴权(401)或上报落入默认未知项目
        os.environ["LANGSMITH_TRACING"] = "true"
        os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
        os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
        os.environ["LANGSMITH_ENDPOINT"] = settings.langsmith_endpoint
        logger.info(
            "LangSmith tracing enabled: project=%s endpoint=%s",
            settings.langsmith_project,
            settings.langsmith_endpoint,
        )
    else:
        # 关闭状态：显式赋值字符串 "false"，而不是维持未定义状态。
        # 目的是主动覆盖环境，防止因依赖包升级出现“默认静默开启”上报的情况。
        os.environ["LANGSMITH_TRACING"] = "false"
        logger.info(
            "LangSmith tracing disabled (no LANGSMITH_API_KEY or switch off)"
        )


def get_current_trace_id() -> str | None:
    """获取当前执行链路中的 trace_id。

    :return: 成功返回 trace_id 字符串；未开启、不在 @traceable 范围内或发生异常时均返回 None。

    【核心规则 - 调用时机】：
    底层 `get_current_run_tree()` 依赖协程/线程的局部运行上下文（ContextVar）。
    必须在带有 `@traceable` 的函数体执行过程中调用！一旦函数执行完毕返回，
    上下文就会被立刻销毁，外部再调用只能拿到 None。

    【核心规则 - 异常绝对兜底】：
    监控链路出问题不能拖垮正常接口。即便 SDK 内部报错，也只记录警告日志，绝不上抛异常。
    """
    if not settings.observability_enabled:
        return None

    try:
        # 延迟导入（Lazy Import）：
        # 1. 只有在确认开启时才导入 SDK，未启用时省去加载开销；
        # 2. 保证导入行为发生在此前已写完 os.environ 之后。
        from langsmith.run_helpers import get_current_run_tree

        run = get_current_run_tree()
        if run is None:
            return None
        return str(run.trace_id)
    except Exception:  # noqa: BLE001 —— 守住核心链路，不向外抛出任何监控异常
        logger.warning("get_current_trace_id 异常，返回 None", exc_info=True)
        return None


def build_trace_url(trace_id: str | None) -> str | None:
    """根据 trace_id 拼接出 LangSmith 网页端单次运行详情的完整可跳转链接。

    :param trace_id: 从 `get_trace_id()` 拿到的运行标识符
    :return: 完整的 HTTP URL 字符串；未配置前缀或无有效 trace_id 时返回 None。

    【容错设计】：
    1. 前缀选配：URL 前缀包含用户的租户组织 ID，无法在后端自动推导。若用户未配，
       直接返回 None 让前端展示纯文本/复制按钮，防止展示“点不开的死链接”。
    2. 路径清洗：使用 `rstrip("/")` 裁掉尾部斜杠，防止拼出类似 `//runs/` 的畸形地址。
    """
    if not trace_id or not settings.langsmith_run_url_prefix:
        return None

    return f"{settings.langsmith_run_url_prefix.rstrip('/')}/runs/{trace_id}"