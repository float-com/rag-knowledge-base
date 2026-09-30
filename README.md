# rag-knowledge-base

> 一个「可评测、可观测、可权限隔离」的生产级 RAG 知识库系统
> FastAPI + LangGraph + pgvector + Celery + React 19 + 腾讯云 COS + Redis Stack

[![Python](https://img.shields.io/badge/Python-3.12+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.141+-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=white)](https://react.dev/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16%20%2B%20pgvector%20%2B%20zhparser-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Redis](https://img.shields.io/badge/Redis-Stack%207.4-DC382D?logo=redis&logoColor=white)](https://redis.io/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](#-许可证)

---

## 📖 项目简介

**rag-knowledge-base** 是一套前后端分离、容器化部署的企业级 RAG（Retrieval-Augmented Generation，检索增强生成）知识库系统。它把「文档上传 → 解析切分 → 向量化入库 → 混合检索 → 精排校验 → 带引用生成」这条完整链路做成了可直接上线的产品，而不是一段 demo 脚本：文档经 Docling 解析、按 `chunk_hash` 做增量索引，检索侧同时跑 pgvector 向量召回与 zhparser 中文全文召回并用 RRF 融合，再由 DashScope Reranker 精排、由答案校验器与上下文裁判决定「回答还是拒答」。

在这条主链路之外，项目补齐了真实业务必需的工程配套：**JWT + RBAC 权限标签**让检索 SQL 层就完成越权过滤（无权分块进不了召回），**RAGAS 四维自动指标 + 人工指标 + BadCase 漏斗归因**让效果可量化、可定位到具体环节，**LangSmith 全链路 trace** 让每次问答可回溯，**语义缓存 + 滑动窗口限流 + Celery 异步任务**把成本与稳定性管住，**MCP Server** 则让 Cursor / Inspector 等外部 Agent 能直接调用同一个知识库。

**适用场景**：企业内部规章制度与差旅报销问答、产品手册与技术文档检索、客服知识助手、需要权限隔离的多租户知识库、以及作为学习 RAG 全链路工程化的参考实现。

> 💡 项目采用**分阶段演进**（Day 系列）方式构建，每一期都留下了需求分析、方案设计与排障记录，归档在 [`项目阶段性总结/`](项目阶段性总结) 目录中——这是本项目最有价值的副产品之一。

---

## ✨ 核心特性

### 检索与生成

| 特性 | 说明 |
| --- | --- |
| **Agentic RAG 工作流** | 基于 LangGraph 的 `StateGraph` 编排：`normalize_query → route_query → plan_retrieval → retrieve → observe_context`（可循环）`→ rerank → judge_context →（或 refuse）→ END`，由 Agent Planner 在执行中决定 `proceed / rewrite_query / switch_route / refuse` |
| **Query 优化四策略** | 策略路由：`original`（原样检索）/ `rewrite`（改写）/ `hyde`（假设性文档嵌入）/ `multi_query`（多子查询展开，条数可配），并被 Agentic 循环二次调度 |
| **混合检索 + RRF 融合** | PostgreSQL `pgvector` 向量召回 与 `zhparser` 中文全文检索并行，用 RRF（Reciprocal Rank Fusion）融合多路排序 |
| **Reranker 精排** | DashScope `qwen3-rerank` 精排 Top-N，`RERANK_MIN_SCORE` 阈值不达标直接拒答；`RERANK_ENABLED=false` 时节点透传，可做「有/无精排」A/B 对比 |
| **答案可信度校验** | `AnswerVerifier` 校验回答是否被引用证据支撑，配合 `judge_context` 最终闸门与统一 `refuse` 出口，把幻觉收敛到「宁可不答」 |
| **多轮上下文化改写** | 首问自动生成会话标题，后续轮次结合 `CHAT_HISTORY_WINDOW` 历史轮做指代消解改写 |

### 文档入库

| 特性 | 说明 |
| --- | --- |
| **预签名直传（Presigned URL Direct Upload）** | 三步链路 `POST /api/documents/uploads/init` → 浏览器 `PUT` 直传腾讯云 COS → `POST /api/documents/uploads/{upload_id}/complete`，文件字节不过后端，彻底绕开 multipart 代理上传的带宽与内存瓶颈 |
| **SHA-256 内容幂等** | 上传即算 SHA-256，相同内容复用既有文档，不重复建档、不重复烧 embedding |
| **按 `chunk_hash` 的增量重建索引** | 以内容寻址对齐新旧切片：旧有新无 → `DELETE`；新旧皆有 → 仅 `UPDATE` 位置与元数据（不重算向量）；新有新无 → `INSERT` 并只对新增切片算 embedding。文档级「重试 / 重建索引」入口分别对应全量与增量 |
| **Celery 异步任务 + 进度台账** | 入库迁移为异步任务（`ingest_document` / `reindex_document` / `finalize_upload`），`ingestion_tasks` 表记录 `pending → running → success/failed` 与 `progress_done / progress_total`，前端可轮询进度并展示失败原因 |
| **Docling 解析 + 开销可配** | 支持 PDF / DOCX / Markdown / HTML；OCR 与表格结构识别可独立开关（小内存服务器按需关闭），解析超时显式判定为失败（Docling 超时**不抛异常**，会返回残缺结果，必须拦截） |
| **模型预热与离线加载** | `scripts/preheat_models.py` 从 Docling 配置反查所需仓库、支持断点续传、`--dry-run` 预览；预热后设 `HF_HUB_OFFLINE=true` 完全离线，不再依赖外网 |

### 工程能力

| 特性 | 说明 |
| --- | --- |
| **认证与安全检索** | JWT（HS256）登录 + `roles` / `users` RBAC；用户有效权限 = 各角色 `permission_tags` 并集，检索 SQL 层用 PostgreSQL 数组重叠（`&&`，GIN 索引）过滤，无权分块不进入召回；同名 `permission_filter_error` 归因分类用于定位越权问题 |
| **语义缓存** | Redis Stack + RediSearch 向量索引（经 RedisVL 做 KNN），query 与历史问题余弦相似度 ≥ `SEMANTIC_CACHE_MIN_SIMILARITY`（默认 0.75）即命中，TTL 默认 1 小时；`SEMANTIC_CACHE_ENABLED=false` 可做「有/无缓存」对比 |
| **滑动窗口限流** | 基于 Redis 的滑块窗口限流，按用户维度挂载到问答 / 上传 / 重建索引等重型写接口（默认 60 次/分钟），MCP 工具通道同样接入，避免绕过 |
| **全链路可观测性** | LangSmith `@traceable` 打点覆盖检索、重排、生成等环节，`trace_id` 一路透传到前端消息与引用，前端可一键跳转 LangSmith 查看完整调用树 |
| **RAGAS 评测与 BadCase 归因** | 四维自动指标 + 三项人工指标 + 13 类漏斗式归因分类，评测在后台执行、前台轮询进度，支持人工覆盖机评结论 |
| **MCP Server** | FastMCP（Streamable HTTP）挂载于 `/mcp`，暴露 5 个知识库工具供外部 Agent 调用，鉴权复用同一套 `Authorization: Bearer <JWT>`，权限口径与网页端一致 |
| **启动自愈** | 进程被杀后残留的「进行中」评测 / 入库记录在启动时自动收敛为失败态，避免前端卡死、重试按钮失效 |
| **一键生产部署** | `deploy/deploy.sh` 串起端口收回校验、容器启动、数据库迁移、systemd 常驻、Nginx 反代与前端构建，且每步「先校验再执行」、可幂等重跑 |

---

## 🧱 技术栈

### 后端

| 分类 | 技术选型 |
| --- | --- |
| 语言 / 运行时 | Python `>= 3.12`，依赖管理 [uv](https://github.com/astral-sh/uv)（`pyproject.toml` + `uv.lock`） |
| Web 框架 | FastAPI `>= 0.141`（应用工厂 + lifespan）、Uvicorn `>= 0.52` |
| 工作流编排 | LangGraph / LangChain `>= 1.4`（`StateGraph` 状态机）、langchain-openai、langchain-text-splitters |
| 数据访问 | SQLAlchemy 2.x（asyncio）+ asyncpg、Alembic 迁移、pgvector 扩展 |
| 文档解析 | Docling `>= 2.126`（版面分析 / 表格识别 / OCR）、Hugging Face Hub 模型缓存 |
| 异步任务 | Celery `>= 5.6`（broker / result backend 均为 Redis） |
| 缓存与限流 | redis-py `>= 6.4`、RedisVL `>= 0.27`（语义缓存 KNN 索引） |
| 评测 | RAGAS `>= 0.4.3` |
| 认证 | PyJWT `>= 2.10`（HS256）+ bcrypt |
| 对象存储 | 腾讯云 COS（`cos-python-sdk-v5`） |
| MCP | FastMCP `>= 2.14,<3`、mcp `>= 1.30,<2` |
| 可观测性 | LangSmith SDK |
| 文本模型 | DashScope（阿里云百炼）OpenAI 兼容协议：`text-embedding-v3`（1024 维）、`qwen-plus`、`qwen3-rerank` |

### 前端

| 分类 | 技术选型 |
| --- | --- |
| 框架 | React `19` + TypeScript `~6.0` + Vite `8` |
| UI 组件 | Ant Design `6` + `@ant-design/icons` |
| 路由 | React Router `7` |
| 状态管理 | Zustand `5` + TanStack Query `5` |
| 流式问答 | `@microsoft/fetch-event-source`（SSE）+ `react-markdown` + `remark-gfm` |
| API 客户端 | `@hey-api/openapi-ts` 从后端 OpenAPI 契约生成 SDK（`npm run gen:api`） |
| 代码质量 | ESLint 10 + typescript-eslint |

### 基础设施

| 分类 | 技术选型 |
| --- | --- |
| 关系库 + 向量库 + 全文检索 | PostgreSQL 16，镜像 `leikooo/postgres-pgvector-zhparser`（同时集成 **pgvector** 与 **zhparser** 中文解析器） |
| 缓存 / 限流 / 消息队列 | Redis Stack `redis/redis-stack-server:7.4.0-v0`（含 RediSearch，**普通 Redis 镜像会因缺少 `FT.CREATE` 而无法建语义缓存索引**） |
| 容器编排 | Docker Compose v2（开发 `docker-compose.yml` / 生产 `docker-compose.prod.yml`） |
| 生产进程管理 | systemd（`rag-kb-api.service` / `rag-kb-celery.service`）+ Nginx 反向代理 + 宝塔面板 |

---

## 🏗️ 架构与模块说明

### 请求链路总览

```text
                        ┌──────────────────────────────────────────────┐
   浏览器 / 外部 Agent   │  Nginx  :80                                  │
   (React SPA)          │   /api/  → 127.0.0.1:8100  (SSE 关闭缓冲)    │
        │               │   /mcp   → 127.0.0.1:8100  (Streamable HTTP) │
        │               │   /      → frontend/dist   (SPA try_files)  │
        │               └──────────────────────────────────────────────┘
        │                                   │
        ▼                                   ▼
┌───────────────────────────────────────────────────────────────────────┐
│  FastAPI 主应用 (app.main:app)                                        │
│   CORS 中间件 · 全局异常处理 · JWT/RBAC 依赖注入 · 限流依赖           │
│                                                                       │
│   REST 路由                     MCP 子应用 (挂载 /mcp)                │
│   /api/auth      认证           ask_knowledge_base                    │
│   /api/users     用户管理       upload_document                       │
│   /api/roles     角色管理       list_documents                        │
│   /api/documents 文档管理       get_document_status                   │
│   /api/documents/uploads  直传  get_knowledge_base_stats              │
│   /api/conversations      问答                                        │
│   /api/evaluations        评测                                        │
│   /api/health             健康检查                                    │
└───────────────┬───────────────────────────────┬───────────────────────┘
                │                               │
                ▼                               ▼
   ┌────────────────────────┐      ┌──────────────────────────────┐
   │  服务层 (services/)    │      │  Celery Worker               │
   │  ChatService           │      │  ingest_document             │
   │  DocumentService       │      │  reindex_document            │
   │  DocumentUploadService │      │  finalize_upload             │
   │  EvaluationService     │      └───────────┬──────────────────┘
   │  Auth / User / Role    │                  │
   │  SemanticCacheService  │                  ▼
   └───────┬────────────────┘      ┌──────────────────────────────┐
           │                       │  ingestion/ 解析流水线       │
           ▼                       │  parser (Docling)            │
   ┌────────────────────────┐      │  splitter (递归切分)         │
   │ workflows/ LangGraph   │      │  embedder (DashScope)        │
   │ normalize → route →    │      │  pipeline (ingest/reindex)   │
   │ plan → retrieve →      │      └───────────┬──────────────────┘
   │ observe ↔ (循环)       │                  │
   │ → rerank → judge → END │                  │
   └───────┬────────────────┘                  │
           ▼                                   │
   ┌────────────────────────────────────────┐  │
   │ retrieval/  HybridRetriever            │  │
   │  vector_retriever (pgvector)           │  │
   │  keyword_retriever (zhparser)          │  │
   │  → RRF 融合 → llm/reranker 精排        │  │
   └───────┬────────────────────────────────┘  │
           │                                   │
           ▼                                   ▼
   ┌────────────────────────┐   ┌───────────────────────────────┐
   │  Redis Stack           │   │  PostgreSQL 16                │
   │  db0 语义缓存 + 限流   │   │  pgvector: document_chunks    │
   │  db1 Celery broker     │   │  zhparser: 中文全文索引       │
   │  db2 Celery result     │   │  业务表 / 权限 / 评测 / 任务  │
   └────────────────────────┘   └───────────────────────────────┘
               │
               ▼
   ┌────────────────────────┐   ┌───────────────────────────────┐
   │ 腾讯云 COS             │   │ LangSmith                     │
   │ 原始文档对象存储       │   │ 全链路 trace                  │
   └────────────────────────┘   └───────────────────────────────┘
```

### 分层职责

| 层 | 目录 | 职责 |
| --- | --- | --- |
| **接入层** | `backend/app/api/` | 路由定义（`routes/`）、请求响应契约（`schemas/`）、依赖注入与鉴权（`deps.py`）、全局异常处理器（`error_handlers.py`）。**不含业务逻辑**，只做参数校验与协议翻译 |
| **服务层** | `backend/app/services/` | 业务编排的唯一入口：`ChatService`（问答主链路，含流式 / 非流式 / 评测 / MCP 四个出口）、`DocumentService`、`DocumentUploadService`（直传三步）、`EvaluationService`、`AuthService`、`UserService`、`RoleService`、`PermissionService`、`SemanticCacheService` |
| **工作流层** | `backend/app/workflows/` | LangGraph 状态机（`graph.py`）+ 共享状态契约（`rag_state.py`）+ 各节点（`nodes/`）：`normalize_query`、`route_query`、`plan_retrieval`、`retrieve`、`observe_context`、`rerank`、`judge_context`、`refuse`、`generate`、`load_context` |
| **检索层** | `backend/app/retrieval/` | `VectorRetriever`（pgvector 语义召回）、`KeywordRetriever`（zhparser 全文召回）、`HybridRetriever`（双路并发 + RRF 融合） |
| **模型层** | `backend/app/llm/` | `AgentPlanner`（决策）、`QueryRewriter`（改写 / HyDE / 多子查询）、`Reranker`（精排）、`AnswerVerifier`（答案校验）、`models.py`（模型客户端封装）、`prompts.py`（提示词集中管理） |
| **入库层** | `backend/app/ingestion/` | `parser.py`（Docling 解析与超时兜底）、`splitter.py`（切分与 `chunk_hash` 计算）、`embedder.py`（向量化）、`pipeline.py`（`ingest` / `reindex` 两条流水线）、`tasks.py`（Celery 任务入口） |
| **评测层** | `backend/app/evaluation/` | `dataset.py`（JSONL 评测集加载与校验）、`scoring.py`（人工指标 + BadCase 归因）、`ragas_runner.py`（RAGAS 四维自动指标）、`datasets/`（内置评测集） |
| **MCP 层** | `backend/app/mcp_server/` | `server.py`（FastMCP 实例）、`auth.py`（Bearer → 用户）、`tools.py`（5 个工具），`schemas.py`（工具入参出参契约）、`limits.py`（工具级限流） |
| **数据层** | `backend/app/db/` | `models.py`（ORM 模型与状态枚举）、`repositories/`（仓储层，含权限过滤 SQL）、`session.py`（异步会话与常驻事件循环）、`seed.py`（种子管理员）、`recovery.py`（启动自愈） |
| **核心层** | `backend/app/core/` | `config.py`（Pydantic Settings 与生产硬校验）、`hf_env.py`（HF 环境变量注入）、`observability.py`、`security.py`（JWT / bcrypt）、`permissions.py`、`rate_limiter.py`、`redis.py`、`logging.py`、`tags.py` |
| **存储层** | `backend/app/storage/` | `cos_client.py`（COS 客户端与预签名 URL）、`file_service.py`（对象读写封装） |
| **前端层** | `frontend/src/` | `pages/`（页面）、`components/`（业务组件）、`api/`（手写 API 封装与 SSE）、`client/`（OpenAPI 自动生成 SDK）、`stores/`（Zustand 状态）、`routes/`（路由与鉴权守卫） |
| **部署层** | `deploy/` | `deploy.sh`（一键部署）、`docker-compose.override.yml`（端口收回回环）、`rag-kb-api.service` / `rag-kb-celery.service`（systemd）、`nginx.conf`（反代与 SPA 回落） |

---

## 🚀 快速开始

### 1. 环境要求

| 依赖 | 版本 / 说明 |
| --- | --- |
| Docker Desktop | 需 Docker Compose **v2.24+**（`deploy/docker-compose.override.yml` 使用了 `!override` 端口覆盖语法） |
| Python | `>= 3.12` |
| uv | 最新版（`pip install uv` 或官方脚本安装） |
| Node.js | 18+（Vite 8 / React 19） |
| 外部服务 | 腾讯云 COS 存储桶（含跨域规则）、DashScope（阿里云百炼）API Key |
| 磁盘 | 后端镜像含 torch + docling，通常 **1.5~3 GB**；Docling 模型权重数百 MB |

```powershell
docker --version
docker compose version
node --version
python --version
uv --version
```

### 2. 配置 `.env`

项目**只认一个变量文件**：仓库根目录的 `.env`。复制模板后按注释逐项修改：

```bash
cp .env.example .env
```

**最小可运行配置**（本地开发）：

```dotenv
# ---- 应用 ----
APP_NAME=rag-knowledge-base
LOG_LEVEL=INFO
ENVIRONMENT=development          # production 会开启 JWT 硬校验

# ---- 数据库 ----
POSTGRES_USER=rag
POSTGRES_PASSWORD=rag
POSTGRES_DB=rag_kb
POSTGRES_PORT=5432
DATABASE_URL=postgresql+asyncpg://rag:rag@localhost:5432/rag_kb

# ---- 腾讯云 COS（文档存储，必填）----
COS_SECRET_ID=你的SecretId
COS_SECRET_KEY=你的SecretKey
COS_REGION=ap-guangzhou
COS_BUCKET=你的存储桶名-APPID

# ---- 向量模型（百炼）----
EMBEDDING_API_KEY=你的Key
EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
EMBEDDING_MODEL=text-embedding-v3
EMBEDDING_DIM=1024
EMBEDDING_BATCH_SIZE=10          # ⚠️ text-embedding-v3 兼容模式上限为 10，填大直接 400

# ---- 对话模型（百炼）----
CHAT_API_KEY=你的Key
CHAT_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
CHAT_MODEL=qwen-plus

# ---- 缓存 / 队列 ----
REDIS_URL=redis://localhost:6379/0
CELERY_BROKER_URL=redis://localhost:6379/1
CELERY_RESULT_BACKEND=redis://localhost:6379/2
```

**常用可选配置**：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CORS_ORIGINS` | `http://localhost:5173` | 允许跨域的前端源，逗号分隔；生产必须改成实际域名 |
| `RETRIEVAL_TOP_K` | `5` | 向量召回数量 |
| `RETRIEVAL_MIN_SCORE` | `0.6` | embedding 余弦相似度门槛，低于则拒答 |
| `CHAT_HISTORY_WINDOW` | `5` | 注入上下文的历史轮数 |
| `QUERY_ROUTE_ENABLED` | `true` | Query 优化总开关（关闭后退化回原样检索） |
| `MULTI_QUERY_COUNT` | `3` | `multi_query` 策略每轮展开的子查询条数 |
| `RERANK_ENABLED` / `RERANK_MODEL` | `true` / `qwen3-rerank` | 精排开关与模型；`RERANK_MIN_SCORE=0.3` 为拒答阈值 |
| `VERIFY_ANSWER_ENABLED` | `true` | 答案可信度校验开关 |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | `600` / `60` | 切块大小与重叠长度 |
| `UPLOAD_MAX_SIZE_MB` | `50` | 单文件上传上限 |
| `HF_ENDPOINT` | `https://hf-mirror.com` | Hugging Face 镜像（Docling 模型下载加速） |
| `DOCLING_DO_OCR` | `true` | 关闭可省约 260 MB 内存；扫描件将因抽不到文字而失败 |
| `DOCLING_DO_TABLE_STRUCTURE` | `true` | 关闭后表格会被拉平为普通文本 |
| `DOCLING_DOCUMENT_TIMEOUT_SECONDS` | `120` | 单篇解析超时；2 核小服务器建议 300 |
| `RAGAS_TIMEOUT_SECONDS` / `RAGAS_MAX_WORKERS` | 代码默认 `420` / `4`（`.env.example` 给 `600`） | RAGAS 单作业墙钟超时与并发；并发过高会触发裁判模型限流，反而整体更慢 |
| `RAGAS_MAX_RETRIES` / `RAGAS_MAX_WAIT_SECONDS` | `3` / `15` | 单次调用重试次数与退避上限（RAGAS 官方默认 10 / 60，会让最坏耗时爆炸） |
| `RAGAS_REQUEST_TIMEOUT_SECONDS` / `RAGAS_REQUEST_MAX_RETRIES` | `45.0` / `1` | **请求级**超时与裁判客户端 SDK 重试次数——这是「整轮评测跑一小时不结束」的真正解药：问答客户端若用 openai SDK 默认的 600 秒超时，一个挂住的请求就能烧光整个作业预算 |
| `SEMANTIC_CACHE_ENABLED` / `_TTL_SECONDS` / `_MIN_SIMILARITY` | `true` / `3600` / `0.75` | 语义缓存开关、TTL 与命中阈值 |
| `RATE_LIMIT_ENABLED` / `RATE_LIMIT_PER_MINUTE` | `true` / `60` | 滑动窗口限流开关与额度 |
| `LANGSMITH_TRACING` / `LANGSMITH_API_KEY` / `LANGSMITH_PROJECT` | `false` / — / `rag-knowledge-base` | 可观测性；**只开开关不填 Key 视为未启用** |
| `JWT_SECRET` | `change-me-...` | **必须替换**：`openssl rand -hex 32` |
| `DEFAULT_ADMIN_USERNAME` / `_PASSWORD` | `admin` / `CHANGE_ME_BEFORE_FIRST_START` | 种子管理员，**仅库内无用户时生效** |

> ⚠️ **安全提示**
> `.env`、COS 密钥、模型 API Key、`JWT_SECRET` **一律不得提交到仓库或出现在截图中**。
> `ENVIRONMENT=production` 时，`JWT_SECRET` 为空或短于 32 字符会**直接拒绝启动**，默认管理员弱口令会打 ERROR 告警。

#### 预热 Docling 模型（首次解析前必做）

Docling 解析 PDF / 图片时需要从 Hugging Face 下载版面分析、表格识别等权重（数百 MB，首次需数分钟）：

```powershell
cd backend
uv run python scripts/preheat_models.py --dry-run   # 先看会下载什么
uv run python scripts/preheat_models.py             # 正式预热（支持断点续传）
```

脚本从 Docling 自身配置反查所需仓库（不硬编码仓库名，Docling 升级无需改脚本），正常结束输出 `SUCCESS: all 3 repositories are ready.`。预热完成后建议在 `.env` 中改为离线模式：

```dotenv
HF_HUB_OFFLINE=true
```

> 💡 `HF_ENDPOINT` / `HF_HOME` 必须由 `app/core/config.py` 声明字段、`app/core/hf_env.py` 注入 `os.environ`、`app/main.py` 在**所有业务导入之前**调用——因为 `huggingface_hub` 只在 import 阶段读取一次这些变量。
> 验证镜像是否真正生效：
> ```powershell
> uv run python -c "import app.main; from huggingface_hub import constants; print(constants.ENDPOINT)"
> # 预期输出：https://hf-mirror.com
> ```

### 3. 开发环境启动

开发形态：**PG / Redis 跑容器，api / worker 跑宿主机**。

```bash
# ① 起数据库与缓存
docker compose up -d postgres redis
docker ps                      # 应看到 rag-kb-postgres 与 rag-kb-redis

# ② 后端：安装依赖 + 迁移 + 启动
cd backend
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

启动后可用地址：

| 地址 | 用途 |
| --- | --- |
| <http://localhost:8000> | 后端服务 |
| <http://localhost:8000/docs> | Swagger UI（在线调试） |
| <http://localhost:8000/api/health> | 应用健康检查 |
| <http://localhost:8000/api/health/db> | 数据库健康检查 |
| <http://localhost:8000/api/health/cos> | COS 健康检查 |
| <http://localhost:8000/mcp> | MCP Server（Streamable HTTP） |

```bash
# ③ Celery worker（另开一个终端，负责解析 / 切分 / 向量化）
cd backend
uv run celery -A app.celery_app worker -l info --pool=solo   # Windows 必须加 --pool=solo
# Linux / macOS 去掉 --pool=solo，或用默认 prefork 池

# ④ 前端（再开一个终端）
cd frontend
npm install
npm run dev            # http://localhost:5173，/api 请求由 Vite 代理到 :8000
```

默认种子管理员账号：`admin` / `.env` 中 `DEFAULT_ADMIN_PASSWORD` 的值（初始库内无用户时自动创建）。

上传一个文档验证全链路：登录 → `/documents` 页「上传文档」→ 观察文档状态 `uploading → parsing → indexing → ready`。

### 4. 生产环境部署

**形态 A：全容器（Compose）** —— 适合单机快速上线：

```bash
cp .env.example .env
vi .env      # 至少改 5 项，见下方清单

docker compose --env-file .env -f docker-compose.prod.yml up -d --build

# 首次部署必须预热模型（否则 PDF 解析必失败）
docker compose --env-file .env -f docker-compose.prod.yml \
  run --rm api python scripts/preheat_models.py
```

生产编排的三个关键差异：**① 数据库与 Redis 不发布端口到宿主机**（`expose` 而非 `ports`，只留在 Compose 内网）；**② 连接串走服务名** `postgres` / `redis`，不再依赖 `localhost`；**③ `ENVIRONMENT=production`** 触发 JWT 硬校验。api 容器启动即执行 `alembic upgrade head && uvicorn ... --workers 2`，迁移失败直接退出，不让服务带病启动。

**形态 B：宝塔面板 + systemd + Nginx（推荐，`deploy/` 目录）**：

```bash
# ① 拿到代码（宝塔面板上传压缩包到 /www/wwwroot/rag-kb 亦可）
cd /www/wwwroot
git clone https://github.com/float-com/rag-knowledge-base.git rag-kb

# ② 准备生产配置
cd rag-kb
cp .env.example .env
vi .env
#   必改：POSTGRES_PASSWORD / JWT_SECRET(openssl rand -hex 32)
#         DEFAULT_ADMIN_PASSWORD / CORS_ORIGINS / ENVIRONMENT=production
#   端口对齐：POSTGRES_PORT=55432、REDIS_PORT=56379，
#             并同步改 DATABASE_URL / REDIS_URL / CELERY_* 里的端口

# ③ 一键部署
bash deploy/deploy.sh
#   若 uv 不在 PATH：UV_BIN=$(which uv) bash deploy/deploy.sh
```

`deploy.sh` 的执行顺序为 **8 步**：前置检查 → `.env` 关键项自检 → 端口收回回环校验 → 启动容器 → 数据库迁移 → 安装 systemd unit → 配置 Nginx → 构建前端。其中两道安全闸门值得单独说明：

- **闸门 1（端口回环校验）**：`docker-compose.yml` 里的 `"${POSTGRES_PORT}:5432"` 不写 IP 即绑定 `0.0.0.0`，等于**数据库公网可直连**；而 Compose 合并文件时 `ports` 默认是**追加**而非替换。脚本因此强制检查 `docker compose ps` 输出，一旦出现 `0.0.0.0:55432` / `0.0.0.0:56379` 立即报错退出（通常是 Compose < 2.24 导致 `!override` 失效）。
- **闸门 2（`.env` 五项自检）**：`ENVIRONMENT=production`、`JWT_SECRET` 长度 ≥ 32 **且不是模板里的公开值**、`DEFAULT_ADMIN_PASSWORD` 未留占位符、连接串端口与 override 对齐、`CORS_ORIGINS` 未留 `localhost`。

脚本跑完还剩三件手工事：**① 预热 Docling 模型；② 重置已存在库的 admin 口令（改 `.env` 对已建号无效）；③ 在宝塔面板申请 HTTPS 证书**。

生产端口与进程布局：

| 组件 | 位置 | 端口 / 入口 |
| --- | --- | --- |
| Nginx | 宿主机 | `:80`（`/api/` 与 `/mcp` 反代、其余回落 `frontend/dist`） |
| API（uvicorn） | systemd `rag-kb-api.service` | `127.0.0.1:8100`（只监听回环，由 Nginx 对外） |
| Celery worker | systemd `rag-kb-celery.service` | `--concurrency=2` |
| PostgreSQL | Docker | `127.0.0.1:55432` → 容器 `5432` |
| Redis Stack | Docker | `127.0.0.1:56379` → 容器 `6379` |

> ⚠️ Nginx 配置中 `/api/` 段的 `proxy_buffering off` + `chunked_transfer_encoding on` + `proxy_read_timeout 600s` 是 **SSE 流式问答的必要条件**，缺少会导致「逐字输出」消失或长回答被提前掐断。

---

## 📁 项目结构

```text
rag-knowledge-base/
├── backend/                          # 后端服务（FastAPI + Celery + LangGraph）
│   ├── app/
│   │   ├── api/                      # 接入层
│   │   │   ├── routes/               #   auth / users / roles / documents /
│   │   │   │                         #   document_uploads / chat / evaluations / health
│   │   │   ├── schemas/              #   请求响应契约（Pydantic）
│   │   │   ├── deps.py               #   依赖注入（CurrentUser / CurrentAdmin / 限流）
│   │   │   └── error_handlers.py     #   全局异常处理器
│   │   ├── core/                     # 核心配置与横切能力
│   │   │   ├── config.py             #   Pydantic Settings（含生产硬校验）
│   │   │   ├── hf_env.py             #   Hugging Face 环境变量注入
│   │   │   ├── observability.py      #   LangSmith 接入
│   │   │   ├── security.py           #   JWT 签发验签 / 密码哈希
│   │   │   ├── permissions.py        #   权限标签与管理员判定
│   │   │   ├── rate_limiter.py       #   滑动窗口限流
│   │   │   ├── redis.py              #   Redis 客户端
│   │   │   └── logging.py / tags.py / exceptions.py
│   │   ├── db/                       # 数据层
│   │   │   ├── models.py             #   ORM 模型与状态枚举
│   │   │   ├── repositories/         #   仓储层（含权限过滤 SQL）
│   │   │   ├── session.py            #   异步会话 / 常驻事件循环
│   │   │   ├── seed.py               #   种子管理员与内置角色
│   │   │   └── recovery.py           #   启动自愈（收敛僵尸任务）
│   │   ├── services/                 # 服务层（业务编排唯一入口）
│   │   ├── workflows/                # LangGraph 工作流
│   │   │   ├── graph.py / rag_state.py
│   │   │   └── nodes/                #   normalize_query / route_query / plan_retrieval /
│   │   │                             #   retrieve / observe_context / rerank /
│   │   │                             #   judge_context / refuse / generate / load_context
│   │   ├── retrieval/                # 混合检索（vector + keyword + RRF）
│   │   ├── llm/                      # AgentPlanner / QueryRewriter / Reranker /
│   │   │                             # AnswerVerifier / models / prompts
│   │   ├── ingestion/                # 入库流水线（parser / splitter / embedder /
│   │   │                             # pipeline / tasks）
│   │   ├── evaluation/               # 评测（dataset / scoring / ragas_runner）
│   │   │   └── datasets/             #   内置评测集（seed.jsonl 50 条 / smoke.jsonl 5 条）
│   │   ├── mcp_server/               # MCP Server（server / auth / tools / schemas / limits）
│   │   ├── storage/                  # 腾讯云 COS 客户端与文件服务
│   │   ├── celery_app.py             # Celery 应用
│   │   └── main.py                   # FastAPI 应用工厂与装配
│   ├── alembic/                      # 数据库迁移脚本
│   ├── scripts/                      # preheat_models.py / cleanup_upload_sessions.py
│   ├── Dockerfile                    # 多阶段构建（uv + slim，非 root 运行）
│   ├── pyproject.toml / uv.lock      # 依赖声明与锁文件
│   └── alembic.ini
├── frontend/                         # 前端（React 19 + TS + Vite 8 + AntD 6）
│   ├── src/
│   │   ├── pages/                    #   Login / Home / Documents / DocumentDetail /
│   │   │                             #   Chat / EvaluationList / EvaluationDetail /
│   │   │                             #   Users / Roles
│   │   ├── components/               #   CitationList / AgentStepsPanel / MetricsCard /
│   │   │                             #   QueryRoutePanel / TraceIdPanel / RequireAuth 等
│   │   ├── api/                      #   手写 API 封装（含 SSE 流式问答、COS 直传）
│   │   ├── client/                   #   OpenAPI 自动生成 SDK（hey-api）
│   │   ├── stores/                   #   Zustand 状态（authStore）
│   │   ├── layouts/ routes/ utils/   #   布局、路由守卫、工具函数
│   │   └── main.tsx
│   ├── openapi-ts.config.ts          # SDK 生成配置
│   ├── vite.config.ts                # /api 开发代理
│   └── package.json
├── deploy/                           # 生产部署（宝塔 + systemd + Nginx）
│   ├── deploy.sh                     #   ★ 一键部署脚本（8 步，幂等可重跑）
│   ├── docker-compose.override.yml   #   端口收回到 127.0.0.1:55432/56379
│   ├── rag-kb-api.service            #   uvicorn systemd 常驻
│   ├── rag-kb-celery.service         #   Celery worker systemd 常驻
│   └── nginx.conf                    #   /api/ + /mcp 反代与 SPA 回落
├── 项目阶段性总结/                    # 分阶段归档（需求分析 / 方案设计 / 排障记录）
│   ├── Day3_文本向量化/               #   解析、切分、向量化
│   ├── Day3_文本向量化链路优化/        #   预签名直传链路改造
│   ├── Day04_知识库问答/              #   会话与流式问答
│   ├── Day05_Query优化/               #   改写 / HyDE / 多子查询
│   ├── Day06_全文检索、混合检索与 RRF/
│   ├── Day07_Agentic RAG/             #   LangGraph 规划-检索-观察循环
│   ├── Day08_检索链路优化与答案可信度/ #   精排与答案校验
│   ├── Day09_可观测性/                #   LangSmith trace
│   ├── Day10_评测与 Bad Case 分析/     #   RAGAS + 人工指标 + 归因
│   ├── Day11_认证、权限与安全检索/     #   JWT + RBAC + SQL 级权限过滤
│   ├── Day12_缓存、限流、异步任务与增量索引/
│   ├── Day13_MCP Server 集成/         #   MCP 工具 + 部署 + 上线排障
│   └── BUG发现与处理/
├── models/                           # Docling 模型权重缓存（HF_HOME，不入库）
├── docker-compose.yml                # 开发编排（PG + Redis，宿主端口映射）
├── docker-compose.prod.yml           # 生产编排（api + worker + PG + Redis，仅内网）
├── .env.example                      # 环境变量模板（逐项带注释说明）
├── 项目启动.md                        # 本地启动与直传链路验证手册
└── README.md
```

---

## 🧪 评测说明

项目内置一套「自动指标 + 人工指标 + 归因分析」三层评测体系，可通过 Web 前端（管理员）或 REST API 驱动。

### 评测集格式

评测集为 **JSONL**（每行一个 JSON 对象），内置两份：

| 文件 | 条数 | 用途 |
| --- | --- | --- |
| `backend/app/evaluation/datasets/seed.jsonl` | 50 | 完整回归评测 |
| `backend/app/evaluation/datasets/smoke.jsonl` | 5 | 冒烟 / 快速验证 |

单条用例字段：

```json
{
  "id": "case_001",
  "question": "出差住宿一线城市，经理级员工每晚住宿费用上限是多少？",
  "expected_answer": "根据差旅管理制度，经理级员工在一线城市的住宿费上限为每晚 600 元。",
  "expected_document_names": ["差旅管理制度.md"],
  "expected_keywords": ["600", "一线城市", "经理级"],
  "should_refuse": false,
  "tags": ["差旅", "事实问答"]
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | ✅ | 用例唯一标识 |
| `question` | ✅ | 提问 |
| `expected_answer` | ✅ | 标准答案（RAGAS 计算 context_recall / answer_relevancy 的依据） |
| `expected_document_names` | ✅ | 期望引用的文档名列表（引用命中的路径 A） |
| `expected_keywords` | ➖ | 期望论据关键词（引用命中的路径 B，兜底） |
| `should_refuse` | ➖ | 该题是否**应当拒答**（知识库中无答案时置 `true`） |
| `tags` | ➖ | 分类标签，便于分维度看指标 |

### RAGAS 四维自动指标

| 指标 | 阶段 | 含义 |
| --- | --- | --- |
| `faithfulness` | 生成 | 忠实度：回答能否由检索切片推导（幻觉检测） |
| `answer_relevancy` | 生成 | 回答相关性：回答是否切题 |
| `context_precision` | 检索 | 上下文精准率：金牌切片是否排在前面 |
| `context_recall` | 检索 | 上下文召回率：标答所依据的切片是否被召回 |

### 人工指标

| 指标 | 判定逻辑 |
| --- | --- |
| **引用命中率** `citation_hit` | 双路宽松判定：① 引用文档名命中期望文档名；② 期望关键词出现在引用摘录原文中。任一命中即为命中；完全无引用直接判负 |
| **拒答正确率** `refusal_correct` | 实际拒答状态与 `should_refuse` 标注一致 |
| **轨迹评分** | 人工对回答质量打分，用于机评与人评对照 |

### BadCase 漏斗式归因

归因引擎按**从链路最底层开始短路拦截**的顺序判定（避免「检索彻底丢了却去归因模型胡说」的误诊），共 13 类：

| 层级 | 分类 |
| --- | --- |
| 解析层 | `document_parse_failed`、`chunk_split_bad` |
| 检索层 | `embedding_recall_miss`、`keyword_recall_miss`、`rrf_fusion_error` |
| 排序层 | `rerank_order_error` |
| 决策层 | `context_judge_too_loose`、`context_judge_too_strict` |
| 生成层 | `prompt_constraint_weak`、`generation_off_context`、`citation_parse_failed` |
| 权限与兜底 | `permission_filter_error`、`other` |

低分阈值统一为 `0.5`（`_LOW_SCORE_THRESHOLD`）。机评结论不可变，人工可通过 `PATCH /api/evaluations/items/{item_id}` 覆盖并保留对比轨迹。

### 评测 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/evaluations/datasets` | 列出可用评测集（名称 + 条数） |
| `POST` | `/api/evaluations/runs` | 创建评测批次（后台执行，立即返回） |
| `GET` | `/api/evaluations/runs` | 评测批次列表 |
| `GET` | `/api/evaluations/runs/{run_id}` | 批次详情与聚合指标 |
| `DELETE` | `/api/evaluations/runs/{run_id}` | 删除批次 |
| `GET` | `/api/evaluations/runs/{run_id}/items` | 用例明细列表 |
| `GET` | `/api/evaluations/items/{item_id}` | 单条用例详情 |
| `PATCH` | `/api/evaluations/items/{item_id}` | 人工覆盖归因分类 |

### 使用建议

1. **先跑冒烟集**：`smoke.jsonl`（5 条）确认链路通畅，再跑 `seed.jsonl`（50 条）。
2. **预估耗时**：单次总耗时 ≈ 作业数 ÷ `RAGAS_MAX_WORKERS` × 单作业耗时。50 题 × 4 指标 = 200 个作业，并发 4 时需预留**几十分钟**。建议放在低峰期跑。
3. **指标出现大面积 `NaN`（前端显示 `—`）**：几乎总是 `RAGAS_TIMEOUT_SECONDS` 偏小导致作业被丢弃。2 核小服务器上 `faithfulness` 与 `context_precision`（每题裁判请求最多）最容易 100% 超时，而请求少的指标却正常——此时应调大超时或降低并发。
4. **评测跑完不结束 / 前端进度停更**：检查是否有僵尸任务残留；新版已在启动时自动收敛中断任务，并让裁判客户端独立配置。
5. **做对照实验**：通过 `QUERY_ROUTE_ENABLED`、`RERANK_ENABLED`、`VERIFY_ANSWER_ENABLED`、`SEMANTIC_CACHE_ENABLED` 等开关各跑一遍，用同一份评测集量化每个模块的边际收益。

> 💡 评测相关的完整设计文档见 [`项目阶段性总结/Day10_评测与 Bad Case 分析/`](项目阶段性总结/Day10_评测与%20Bad%20Case%20分析)。

---

## 🔌 MCP Server（外部 Agent 接入）

应用在根路径 `/mcp` 挂载了 FastMCP 子应用（Streamable HTTP transport），鉴权复用网页端同一套 `Authorization: Bearer <JWT>`，权限口径完全一致。

| 工具 | 入参 | 权限 | 说明 |
| --- | --- | --- | --- |
| `ask_knowledge_base` | `question` | 任意登录用户 | 向知识库提问，返回带引用的非流式答案；未命中阈值或校验不通过时 `refused=true` |
| `upload_document` | `filename`、`content_base64`、`mime_type?`、`permission_tags?` | **仅管理员** | 上传文档（内容 base64 编码），服务端按 SHA-256 幂等；解析与向量化由 Celery 异步执行 |
| `list_documents` | 分页参数 | 任意登录用户 | 列出当前用户**可见**的文档 |
| `get_document_status` | 文档标识 | 任意登录用户 | 查文档状态与最近一次入库任务进度 |
| `get_knowledge_base_stats` | — | 任意登录用户 | 知识库概览，**严格按调用者权限范围**统计 |

在 Cursor / Claude Desktop 等 MCP 客户端中配置（本地开发示例）：

```json
{
  "mcpServers": {
    "rag-knowledge-base": {
      "url": "http://localhost:8000/mcp",
      "headers": {
        "Authorization": "Bearer <你的 JWT>"
      }
    }
  }
}
```

> 💡 所有写操作与 LLM 调用都挂了独立 scope 的限流，MCP 通道无法绕过 `RATE_LIMIT_PER_MINUTE`。
> 生产环境经 Nginx `/mcp` location 反代，同样开启了 `proxy_buffering off` 与 `chunked_transfer_encoding on`。

---

## ❓ 常见问题

### 启动与依赖

**Q：`docker compose up` 后数据库连不上？**
A：确认 `.env` 中 `DATABASE_URL` 的端口与 `POSTGRES_PORT` 一致。若服务器上用了 `deploy/docker-compose.override.yml`（端口改为 `55432` / `56379`），连接串里的端口必须同步修改。

**Q：`HF_ENDPOINT` 写进 `.env` 了，为什么 Docling 还是去连 huggingface.co？**
A：`huggingface_hub` 只在 **import 阶段**读取一次该变量，而 pydantic-settings 不会把它写回进程环境。本项目由 `app/core/config.py` 声明字段 → `app/core/hf_env.py` 注入 `os.environ` → `app/main.py` 在所有业务导入**之前**调用 `setup_hf_environment()` 才真正生效。另外注意 `config.py` 使用 `extra="ignore"`，**未声明的键会被静默丢弃**。

**Q：上传成功，但文档一直不进入 `ready`？**
A：按顺序排查——① Celery worker 是否在运行（没有 worker 文档会永远停在 `uploading` / `parsing`）；② `EMBEDDING_API_KEY` 是否有效；③ `EMBEDDING_BATCH_SIZE` 是否被改大（`text-embedding-v3` 兼容模式上限为 10，填 16 会直接 400）；④ Docling 模型是否预热完成；⑤ PostgreSQL / pgvector 连接是否正常。
若文档详情显示 `Docling 解析失败: Got: ConnectError: [SSL: UNEXPECTED_EOF_WHILE_READING]`，说明模型权重未下载成功——先预热，再对该文档点「重试」。

**Q：COS 直传 PUT 失败或健康检查不过？**
A：依次核对 `COS_SECRET_ID` / `COS_SECRET_KEY` / `COS_REGION` / `COS_BUCKET`（需含 `-APPID` 后缀）；确认 COS 存储桶已配置允许 `http://localhost:5173` 的跨域规则；并用浏览器 Network 面板确认 PUT 请求的 `Content-Type` 与 init 阶段后端签名保存的 MIME 类型**完全一致**。

**Q：小内存服务器（2 核 4G）解析 PDF 时整机卡死甚至 SSH 连不上？**
A：Docling 默认全开 OCR + 表格识别时，单篇 PDF 解析峰值内存可达 1 GB 以上，叠加 worker 自身（torch + docling 已占约 500 MB）会把整机拖进 swap 抖动。处理方式：设 `DOCLING_DO_OCR=false`、`DOCLING_DO_TABLE_STRUCTURE=false`（纯文字 PDF 实测解析结果完全一致，耗时从 31.1s 降到 24.1s，峰值内存从 1087 MB 降到 828 MB），并调大 `DOCLING_DOCUMENT_TIMEOUT_SECONDS`（小服务器建议 300）。

**Q：语义缓存建索引时报「未知命令 `FT.CREATE`」？**
A：用错了镜像。必须用 `redis/redis-stack-server`（含 RediSearch），普通 `redis` 镜像没有该模块。

**Q：Windows 上 Celery worker 起不来？**
A：Windows 不支持 prefork 池，本地开发必须加 `--pool=solo`；Linux 生产环境用默认池即可。

### 部署问题归档

项目把首次上线与迭代过程中的真实排障记录全部归档在 [`项目阶段性总结/`](项目阶段性总结) 目录，遇到部署问题建议**优先按时间线查阅**：

| 归档位置 | 内容 |
| --- | --- |
| [`Day13_MCP Server 集成/03_2026.9.29/02_首次上线部署与跨域排障/`](项目阶段性总结/Day13_MCP%20Server%20集成/03_2026.9.29) | 首次上线部署问题记录（含跨域专项） |
| [`Day13_MCP Server 集成/03_2026.9.29/01_上线后排障_PDF解析链路/`](项目阶段性总结/Day13_MCP%20Server%20集成/03_2026.9.29) | 从整机冻死到 PDF 链路打通的完整排障过程 |
| [`Day13_MCP Server 集成/02_2026.9.28/07_上线前检查单/`](项目阶段性总结/Day13_MCP%20Server%20集成/02_2026.9.28) | 上线前检查单、部署前测试报告、可执行的冒烟测试脚本（5 个） |
| [`Day12_.../04_Celery 异步任务/`](项目阶段性总结/Day12_缓存、限流、异步任务与增量索引) | Celery 事件循环与连接池生命周期错配等典型问题 |
| [`BUG发现与处理/`](项目阶段性总结/BUG发现与处理) | 各类 BUG 的发现过程与修复记录 |
| [`deploy/README.md`](deploy/README.md) | 部署脚本的设计取舍、两道安全闸门的原理、已知局限 |

> ⚠️ **关于 `deploy.sh` 的诚实说明**：脚本已通过 `bash -n` 语法校验，也设计为「每步先校验再执行、可重复执行」，但**未在真实服务器上完整跑过一轮**。最可能需要现场调整的是 `UV_BIN` 路径（宝塔安装的 Python 环境路径因机器而异），脚本已支持 `UV_BIN=...` 覆盖。

---

## 🤝 贡献指南

欢迎提交 Issue 与 Pull Request。

### 分支与提交规范

- 从 `main` 切出功能分支：`feat/xxx`、`fix/xxx`、`docs/xxx`。
- 提交信息采用 **Conventional Commits**，并尽量使用中文描述，与本仓库历史保持一致：

  ```text
  <type>(<scope>): <简要描述>

  type:  feat | fix | docs | perf | test | refactor | chore
  scope: auth | chat | retrieval | ingestion | evaluation | cache | deploy | mcp | reindex ...
  ```

  示例：`feat(reindex): 新增按 chunk_hash 对齐的增量重建索引`

### 提交前自查

```bash
# 后端：迁移与启动自检
cd backend && uv run alembic upgrade head && uv run uvicorn app.main:app --port 8000

# 前端：类型检查 + 构建 + lint
cd frontend && npm run build && npm run lint

# 若改动涉及 API 契约，重新生成前端 SDK
cd frontend && npm run gen:api
```

### PR 要求

1. **不提交敏感信息**：`.env`、COS 密钥、模型 API Key、`JWT_SECRET` 一律不得入库（`.gitignore` 已覆盖 `.env`）。
2. **数据库结构变更必须附带 Alembic 迁移脚本**，且迁移需可从任意历史版本升级。
3. **新增配置项必须同步更新 `.env.example`**，并带上注释说明用途、默认值与影响面；若涉及生产容器，还需在 `docker-compose.prod.yml` 的 `environment:` 段补一行（该文件刻意逐项列出而非用 `env_file`，以便「容器拿到什么配置」一眼可查）。
4. **文档同步**：较大的功能变更请在 `项目阶段性总结/` 下补充需求分析与方案设计说明。
5. 保持代码风格与现有注释习惯一致——本项目注释以「**为什么这么做**」为主，而非复述代码在做什么。

### 报告问题

提交 Issue 时请附上：复现步骤、期望行为与实际行为、相关日志（注意脱敏密钥）、以及部署形态（本地开发 / Compose 生产 / 宝塔 + systemd）。

---

## 📄 许可证

本项目采用 **MIT License** 发布。你可以自由使用、修改、分发本项目代码，包括商业用途，但需保留原始版权声明与许可声明。

```text
MIT License

Copyright (c) 2026 rag-knowledge-base contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

> 💡 若仓库尚未添加 `LICENSE` 文件，请以仓库实际声明为准，并欢迎补一个 `LICENSE` 文件到根目录。

---

## 🙏 致谢

- [FastAPI](https://fastapi.tiangolo.com/) · [LangChain / LangGraph](https://github.com/langchain-ai/langgraph) · [pgvector](https://github.com/pgvector/pgvector) · [zhparser](https://github.com/amutu/zhparser)
- [Docling](https://github.com/docling-project/docling) · [RAGAS](https://github.com/explodinggradients/ragas) · [RedisVL](https://github.com/redis/redis-vl-python) · [FastMCP](https://github.com/jlowin/fastmcp)
- [Ant Design](https://ant.design/) · [TanStack Query](https://tanstack.com/query) · [Zustand](https://zustand-demo.pmnd.rs/) · [Vite](https://vite.dev/)

---

<div align="center">

**如果这个项目对你有帮助，欢迎点一个 ⭐ Star**

</div>
