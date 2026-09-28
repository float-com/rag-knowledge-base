# 后端镜像（第 13 期 · 生产部署用）
#
# 【为什么要多阶段构建】
# 本项目依赖里有 torch / docling（PDF 版面分析、表格识别），它们只用于【构建期解析依赖】，
# 运行期真正需要的是已下载的模型权重与 python 包；把构建工具链留在最终镜像里会白涨几百 MB。
# 阶段一用官方 uv 镜像装依赖（解析快、带 uv 二进制），阶段二只拷贝装好的虚拟环境。
#
# 【为什么基础镜像选 slim 而不是 alpine】
# torch / psycopg / docling 的二进制轮子对 glibc 有依赖，alpine 的 musl 上需要现场编译，
# 构建时间与失败率都会飙升。slim（debian）能直接用轮子。
#
# 【⚠️ 镜像会比较大（torch + docling，通常 1.5~3 GB）】
# 这是解析 PDF / DOCX 的代价。若部署机磁盘紧张，可选两条优化路线（本项目未做）：
#   ① 只让 worker 装 docling，api 用不装 docling 的精简镜像；
#   ② torch 换成 CPU-only 源（--extra-index-url https://download.pytorch.org/whl/cpu）。

# =============================================================================
# 阶段一：解析依赖并安装到 /app/.venv
# =============================================================================
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /app

# uv 的两条构建期约定：
#   UV_COMPILE_BYTECODE=1  安装时预编译 .pyc，容器启动略快
#   UV_LINK_MODE=copy      禁用硬链接（跨文件系统/分层时硬链接不可用，会报警告）
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# 先只拷贝依赖清单 —— 这样改业务代码时不会击穿依赖层缓存
COPY pyproject.toml uv.lock ./

# --frozen：严格按 uv.lock 安装，保证构建可复现（不允许解析出 lock 里没有的版本）
# --no-install-project：本阶段先不装本项目自身（业务代码还没拷进来）
RUN uv sync --frozen --no-install-project --no-dev

# 再把项目源码与迁移脚本拷进来，安装项目自身
COPY . .
RUN uv sync --frozen --no-dev

# =============================================================================
# 阶段二：运行镜像
# =============================================================================
FROM python:3.12-slim-bookworm

# 运行期需要的系统依赖：
#   libgomp1   torch / onnxruntime 的 OpenMP 运行时（缺了会 ImportError）
#   libgl1     opencv 系（docling 处理图片页时用到）
#   libglib2.0-0  同上，glib 依赖
#   curl       健康检查探针
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 libgl1 libglib2.0-0 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 直接从 builder 拷贝装好的虚拟环境（不带 uv、不带构建缓存）
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app /app

# 把虚拟环境的 bin 放到 PATH 最前，于是 alembic / uvicorn / celery 都能直接用
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # HF_HOME 在容器内必须是【Linux 路径】：
    # 根目录 .env 里写的是 Windows 路径 D:\code\...\models，
    # 直接用会变成容器里一个名字古怪的相对目录。生产靠这个命名卷持久化模型。
    HF_HOME=/models

# 非 root 运行：容器被攻破时降低影响面（文件属主在构建期就改好，运行期不再需要 root）
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /models \
    && chown -R appuser:appuser /app /models
USER appuser

EXPOSE 8000

# 注意：真正的启动命令由 compose 覆盖（api 先 alembic upgrade head 再 uvicorn；
# worker 走 celery）。这里放一个默认值，方便单独 docker run 调试。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
