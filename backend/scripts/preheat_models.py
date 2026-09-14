"""Docling 模型预热脚本。

【解决的问题】：
Docling 解析 PDF / 图片时，会在首次运行时从 Hugging Face Hub 下载版面分析
（layout）与表格识别（TableFormer）模型权重。若下载源不可达，解析任务会失败，
文档状态变为 FAILED 并报出 SSL / ConnectError 之类的网络错误。

本脚本把「首次运行才下载」变成「部署时一次性预热」：
- 从 Docling 自身的配置推导出需要哪些模型仓库，不硬编码仓库名，
  Docling 升级换代默认模型时脚本无需修改；
- 支持断点续传（已存在的文件不会重复下载）；
- 只下载缺失部分，重复执行是幂等的。

【使用方式】（在 backend 目录执行）：
    uv run python scripts/preheat_models.py            # 下载缺失的模型
    uv run python scripts/preheat_models.py --dry-run  # 只列出将要下载什么
    uv run python scripts/preheat_models.py --repo docling-project/docling-models

【预热完成后】：
在 .env 中设置 HF_HUB_OFFLINE=true，即可让后端完全离线加载模型，不再依赖外网。
"""

import argparse
import inspect
import os
import re
import sys
from pathlib import Path

# =============================================================================
# 关键顺序：注入 HF 环境变量必须发生在任何 huggingface_hub / docling 导入之前
# =============================================================================
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.hf_env import setup_hf_environment

setup_hf_environment()

from app.core.logging import configure_logging, get_logger  # noqa: E402

configure_logging()
logger = get_logger("preheat_models")

# 注意：以下导入必须在 setup_hf_environment() 之后，否则镜像配置不会生效
from huggingface_hub import constants as hf_constants  # noqa: E402
from huggingface_hub import snapshot_download  # noqa: E402
from huggingface_hub.errors import LocalEntryNotFoundError  # noqa: E402

# 递归扫描模型配置时的深度上限，防止异常对象结构造成无限递归
_MAX_SCAN_DEPTH = 4


def _known_component_classes() -> list[type]:
    """返回默认管线下会从 Hugging Face Hub 拉取权重的组件类。

    【为什么需要显式列出】：
    部分模型仓库不是通过 pydantic 的 model_spec.repo_id 声明的，而是硬编码在组件类的
    download_models() 里（例如 TableFormer 的 docling-project/docling-models@v2.3.0），
    只靠遍历管线配置会漏掉。这里把组件类找出来，再从它们的 __init__ 签名 / 源码中
    反查仓库信息，从而不把仓库名硬编码在本脚本里。
    """
    components: list[type] = []
    try:
        from docling.models.stages.layout.layout_object_detection_model import (
            LayoutObjectDetectionModel,
        )
        from docling.models.stages.picture_classifier.document_picture_classifier import (
            DocumentPictureClassifier,
        )
        from docling.models.stages.table_structure.table_structure_model import (
            TableStructureModel,
        )

        components = [
            LayoutObjectDetectionModel,
            TableStructureModel,
            DocumentPictureClassifier,
        ]
    except Exception as exc:  # 组件路径随 Docling 版本变化，找不到就退回纯配置扫描
        print(f"[warn] component introspection unavailable: {type(exc).__name__}: {exc}")

    return components


def _repos_from_component(component: type) -> dict[str, str | None]:
    """从组件类的 __init__ 签名与 download_models 源码中抽取模型仓库。"""
    found: dict[str, str | None] = {}

    try:
        params = inspect.signature(component).parameters
        repo_param = params.get("repo_id")
        repo_id = getattr(repo_param, "default", None) if repo_param else None
        if isinstance(repo_id, str) and "/" in repo_id:
            rev_param = params.get("revision")
            revision = getattr(rev_param, "default", None) if rev_param else None
            found[repo_id] = revision if isinstance(revision, str) else "main"
    except (TypeError, ValueError):
        pass

    # 兜底：download_models() 里通常直接写着 repo_id="..." / revision="..."
    try:
        source = inspect.getsource(component.download_models)
    except (AttributeError, OSError, TypeError):
        return found

    repo_match = re.search(r"repo_id\s*=\s*[\"']([^\"']+)[\"']", source)
    if repo_match and "/" in repo_match.group(1):
        rev_match = re.search(r"revision\s*=\s*[\"']([^\"']+)[\"']", source)
        found[repo_match.group(1)] = rev_match.group(1) if rev_match else "main"

    return found


def _collect_repo_ids(node: object, found: dict[str, str | None], depth: int = 0) -> None:
    """从 Docling 配置对象中递归抽取 model_spec.repo_id 与 revision。

    只按属性名精确匹配，避免把 revision 之类的无关字段误当成仓库名。
    """
    if depth > _MAX_SCAN_DEPTH or node is None:
        return
    if isinstance(node, (str, bytes, int, float, bool)):
        return
    if isinstance(node, dict):
        for value in node.values():
            _collect_repo_ids(value, found, depth + 1)
        return
    if isinstance(node, (list, tuple, set)):
        for value in node:
            _collect_repo_ids(value, found, depth + 1)
        return

    spec = getattr(node, "model_spec", None)
    if spec is not None:
        repo_id = getattr(spec, "repo_id", None)
        if isinstance(repo_id, str) and "/" in repo_id:
            found.setdefault(repo_id, getattr(spec, "revision", None) or "main")

    for value in vars(node).values() if hasattr(node, "__dict__") else []:
        _collect_repo_ids(value, found, depth + 1)


def discover_required_repos() -> dict[str, str | None]:
    """通过实例化 DocumentConverter 反查出 Docling 真正会用到的模型仓库。

    DocumentConverter 的构造过程只加载配置、不下载权重，因此这一步不需要网络。
    """
    from docling.document_converter import DocumentConverter

    converter = DocumentConverter()
    found: dict[str, str | None] = {}

    # 遍历所有输入格式（PDF / IMAGE 等）的管线配置，把其中引用的模型仓库全部收集起来
    for options in converter.format_to_options.values():
        _collect_repo_ids(options, found)

    # 再补充硬编码在组件类里的仓库（如 TableFormer 的 docling-models）
    for component in _known_component_classes():
        for repo_id, revision in _repos_from_component(component).items():
            found.setdefault(repo_id, revision)

    return dict(sorted(found.items()))


def _download_repo(repo_id: str, revision: str | None, offline: bool) -> tuple[bool, str]:
    """下载（或校验）单个模型仓库，返回 (是否成功, 说明文本)。"""
    if offline:
        # 离线模式下只检查本地缓存是否齐备，绝不发起网络请求
        try:
            path = snapshot_download(repo_id=repo_id, revision=revision, local_files_only=True)
            return True, f"cached at {path}"
        except LocalEntryNotFoundError:
            return False, "NOT in local cache"
        except Exception as exc:  # 缓存损坏等异常统一降级为失败说明
            return False, f"{type(exc).__name__}: {str(exc)[:160]}"

    try:
        path = snapshot_download(repo_id=repo_id, revision=revision)
        return True, f"ready at {path}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:160]}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pre-download the Hugging Face models required by Docling.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only list the required repositories, do not download anything.",
    )
    parser.add_argument(
        "--repo",
        action="append",
        default=None,
        metavar="REPO_ID",
        help="Extra repository to preheat; can be repeated.",
    )
    args = parser.parse_args()

    offline = os.environ.get("HF_HUB_OFFLINE") == "1"

    print("=" * 70)
    print("Docling model preheat")
    print(f"  HF_ENDPOINT   : {hf_constants.ENDPOINT}")
    print(f"  HF_HOME       : {os.environ.get('HF_HOME')}")
    print(f"  OFFLINE       : {offline}")
    print("=" * 70)

    repos = discover_required_repos()
    for extra in args.repo or []:
        repos.setdefault(extra, "main")

    if not repos:
        print("[FAIL] No model repository discovered from Docling configuration.")
        return 1

    print(f"Required repositories ({len(repos)}):")
    for repo_id, revision in repos.items():
        print(f"  - {repo_id} @ {revision}")

    if args.dry_run:
        print("\n[dry-run] Nothing downloaded.")
        return 0

    print("\nDownloading ...")
    failed: list[str] = []
    for repo_id, revision in repos.items():
        ok, detail = _download_repo(repo_id, revision, offline)
        status = "OK  " if ok else "FAIL"
        print(f"  [{status}] {repo_id} -> {detail}")
        if not ok:
            failed.append(repo_id)

    print("\n" + "=" * 70)
    if failed:
        print(f"FAILED: {len(failed)}/{len(repos)} repositories could not be prepared.")
        if offline:
            print("Hint: unset HF_HUB_OFFLINE and re-run while the network is available.")
        else:
            print("Hint: check HF_ENDPOINT, network connectivity and disk space.")
        for repo_id in failed:
            print(f"  - {repo_id}")
        return 1

    print(f"SUCCESS: all {len(repos)} repositories are ready.")
    print("You can now set HF_HUB_OFFLINE=true in .env for fully offline parsing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
