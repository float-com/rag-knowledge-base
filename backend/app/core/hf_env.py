"""Hugging Face 运行环境变量桥接模块。

【为什么需要这个模块】：
Docling 解析 PDF / 图片时，会通过 huggingface_hub 下载版面分析（layout）与表格识别
（TableFormer）等模型权重。huggingface_hub 是在 **import 阶段** 读取 HF_ENDPOINT 等环境
变量并固化为模块级常量的，之后再改就无效了。

而 pydantic-settings 只会把 .env 读成 Settings 对象，**不会**写回 os.environ。
配置项如果没在 Settings 里声明，还会被 `extra="ignore"` 静默丢弃 —— 这正是
「.env 里明明写了 HF_ENDPOINT，Docling 却仍然去连不可达的 huggingface.co」的根因。

因此本模块的职责是：把 Settings 中的 HF 配置翻译成真正的进程环境变量，
并且必须在任何 huggingface_hub / docling 导入之前执行。

【使用方式】：
- 应用入口：`app/main.py` 顶部第一件事就是 `setup_hf_environment()`；
- 独立脚本：脚本里同样要先 `setup_hf_environment()`，再去 import docling。

【优先级】：
外部已经存在的同名环境变量优先，本模块只做兜底填充（setdefault 语义），
方便在 CI 或临时排障时用命令行环境变量覆盖 .env。
"""

import os
import sys
from pathlib import Path

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def setup_hf_environment() -> dict[str, str]:
    """把 Hugging Face 相关配置注入 os.environ，返回本次实际生效的键值。

    【注入内容】：
    - HF_ENDPOINT：模型下载源。默认官方地址在国内不可达，必须指向镜像站；
    - HF_HOME：模型缓存根目录。统一落在项目可管理的位置，便于备份与离线复用；
    - HF_HUB_OFFLINE：置为 "1" 时彻底禁用联网，全部从本地缓存加载（预热完成后使用）；
    - HF_HUB_DISABLE_TELEMETRY：关闭匿名统计上报，避免解析时的额外外网请求。
    """
    cache_dir = Path(settings.hf_home).expanduser()

    # 缓存目录无法创建时不阻断启动：仅告警并回退到 huggingface_hub 默认缓存位置，
    # 否则一个磁盘权限问题会让整个后端起不来，得不偿失。
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("HF cache dir unavailable, fallback to default: %s (%s)", cache_dir, exc)
        cache_dir = None

    desired: dict[str, str] = {
        "HF_ENDPOINT": settings.hf_endpoint,
        "HF_HUB_OFFLINE": "1" if settings.hf_hub_offline else "0",
        "HF_HUB_DISABLE_TELEMETRY": "1",
    }
    if cache_dir is not None:
        desired["HF_HOME"] = str(cache_dir)

    applied: dict[str, str] = {}
    for key, value in desired.items():
        if not value:
            continue
        # 外部显式设置过的环境变量优先级更高，这里不覆盖
        if os.environ.get(key):
            applied[key] = os.environ[key]
            continue
        os.environ[key] = value
        applied[key] = value

    _warn_if_too_late()
    return applied


def _warn_if_too_late() -> None:
    """检测本函数是否被调用得太晚。

    huggingface_hub 在 import 阶段就会把 HF_ENDPOINT 固化成模块级常量，一旦它已经
    进入 sys.modules，再改 os.environ 就不会生效，镜像配置会静默失灵。
    这里做一次显式告警，把这种"改错位置"的隐患暴露出来，而不是让它在运行时才表现为
    莫名其妙的下载超时。
    """
    if "huggingface_hub" not in sys.modules:
        return
    try:
        from huggingface_hub import constants as hf_constants

        if settings.hf_endpoint and hf_constants.ENDPOINT != settings.hf_endpoint:
            logger.warning(
                "HF_ENDPOINT not applied: huggingface_hub was imported before "
                "setup_hf_environment() (effective endpoint=%s, expected=%s). "
                "Move the call earlier in the startup path.",
                hf_constants.ENDPOINT,
                settings.hf_endpoint,
            )
    except ImportError:  # pragma: no cover - 环境未安装 huggingface_hub 时无需告警
        return
