"""
【模块职责说明】
本模块为应用全局配置中心（Configuration Management），核心作用如下：

1. 声明式环境配置加载：
   基于 Pydantic Settings 实现类型安全的配置管理，按优先级（系统环境变量 > 根目录 .env 文件 > 代码默认缺省值）
   统一加载应用服务、PostgreSQL 数据库、腾讯云 COS 对象存储及跨域（CORS）策略等核心配置。

2. 路径自适应解析：
   通过 Pathlib 动态推导并定位项目全局根目录 PROJECT_ROOT，确保在各种运行路径或容器化部署环境下均能精准读取 .env。

3. 派生配置计算与就绪断言：
   利用 @property 动态生成运行时配置（如将逗号分隔的 CORS 字符串拆解为标准源地址列表、检测 COS 三要素凭据完整性）。

4. 单例高效访问：
   借助 lru_cache 装饰器构建进程级配置单例（Settings），避免重复扫描磁盘 I/O，全局直接导出 settings 实例。
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 计算并获取项目根目录绝对路径
# __file__ 为当前文件 (backend/app/core/config.py)
# .parents[0] -> backend/app/core/
# .parents[1] -> backend/app/
# .parents[2] -> backend/
# .parents[3] -> 项目根目录 (即放置 .env 与 backend 文件夹的同级根目录)
PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """全局应用配置类，继承自 BaseSettings。

    自动按优先级加载配置：系统环境变量 > .env 文件 > 代码中的默认值。
    """

    # Pydantic Settings 配置项模型参数
    model_config = SettingsConfigDict(
        # 指定读取的 .env 环境变量文件路径
        env_file=PROJECT_ROOT / ".env",
        # 设定环境变量文件的文本编码
        env_file_encoding="utf-8",
        # 忽略 .env 文件中未在类属性中显式声明的多余字段，避免抛出 ValidationError
        extra="ignore",
        # 环境变量名称大小写不敏感（例如 APP_NAME 与 app_name 均可正确映射）
        case_sensitive=False,
    )

    # 应用基础配置（带默认缺省值）
    app_name: str = "rag-knowledge-base"  # 应用名称
    log_level: str = "INFO"  # 日志级别 (如 DEBUG, INFO, WARNING, ERROR)

    # ===== 数据库配置 =====
    # 异步数据库连接 URI（DSN）：
    # 格式规范：postgresql+asyncpg://<用户名>:<密码>@<主机地址>:<端口>/<数据库名>
    # 默认值优先被 .env 文件或环境变量中的 DATABASE_URL 所覆盖
    database_url: str = "postgresql+asyncpg://rag:rag@localhost:5432/rag_kb"

    # ===== 腾讯云 COS 对象存储配置 =====
    # 默认值均置为空字符串或缺省地域，实际值统一由 .env 文件动态注入覆盖，切勿在此硬编码真实密钥
    cos_secret_id: str = ""  # 腾讯云 SecretId 身份标识
    cos_secret_key: str = ""  # 腾讯云 SecretKey 签名私钥
    cos_region: str = "ap-guangzhou"  # COS 机房地域代码
    cos_bucket: str = ""  # 持久化存储知识库原始文件的桶名称

    @property
    def cos_configured(self) -> bool:
        """动态检测腾讯云 COS 是否已完成基础必要配置。

        SecretId、SecretKey 和 Bucket 名称缺一不可。
        当三者均不为空字符串时返回 True；若任一项缺失则返回 False，
        用于系统在健康检查（Health Check）或启动自检时智能跳过 COS 连通性测试。
        """
        return bool(self.cos_secret_id and self.cos_secret_key and self.cos_bucket)

    # ===== CORS 跨域资源共享配置 =====
    # 允许的前端源地址，多项用英文逗号分隔（如 "http://localhost:5173,http://127.0.0.1:5173"）
    cors_origins: str = "http://localhost:5173"

    @property
    def cors_origin_list(self) -> list[str]:
        """将逗号分隔的 CORS 源地址字符串解析为标准字符串列表。

        FastAPI 的 CORSMiddleware 要求传入 list[str] 类型。
        通过 strip() 去除首尾空白并剔除空元素，避免多余逗号造成非法匹配。
        """
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    # ===== 文本向量化配置（DashScope OpenAI 兼容协议） =====
    # 阿里云百炼 API-Key，用于向量化接口鉴权
    embedding_api_key: str = ""
    # DashScope 提供的 OpenAI 兼容模式 API 基础请求路径
    embedding_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    # 使用的文本向量化模型名称
    embedding_model: str = "text-embedding-v3"
    # 向量维度大小（注意：必须与 Alembic 迁移脚本中 Vector(N) 保持一致；改动此维度需重建表）
    embedding_dim: int = 1024
    # 单次请求批量向量化的最大文本切片数，避免超出模型单次请求上限
    embedding_batch_size: int = 10

    # ===== 文档上传与切分规则配置 =====
    # 单个上传文档允许的最大文件大小（单位：MB）
    upload_max_size_mb: int = 50
    # 长文本拆分时的单个切片大小（字符数/Token数）
    chunk_size: int = 600
    # 相邻文本切片之间的重叠字符数，防止切分边界处截断关键语义
    chunk_overlap: int = 60

    # ===== Hugging Face 模型下载配置（供 Docling 解析 PDF 使用） =====
    # 【重要】这些字段必须在 Settings 中显式声明：
    #   model_config 里设了 extra="ignore"，未声明的键会被静默丢弃，
    #   之前 .env 中的 HF_ENDPOINT 就是因此从未生效的。
    # 字段名 hf_* 与别名 HF_* 的映射由 pydantic-settings 默认的大小写不敏感规则完成，
    # 真正把它们写进 os.environ 的动作在 app/core/hf_env.py 中完成。
    #
    # 模型下载源：留空则使用 huggingface_hub 默认的官方地址。
    # 国内网络直连 huggingface.co 会超时或 TLS 中断（SSLError / ConnectTimeout），
    # 推荐配置为 https://hf-mirror.com。
    hf_endpoint: str | None = None
    # 模型缓存根目录：所有 Docling 权重统一落在项目自己的 models 目录下，
    # 便于备份、迁移，以及在离线环境下复用（预热一次后可长期断网使用）。
    hf_home: str = str(PROJECT_ROOT / "models")
    # 是否强制离线：置 true 时 huggingface_hub 不再发起任何网络请求，
    # 只从 hf_home 读取已预热好的模型。模型预热完成后建议开启。
    hf_hub_offline: bool = False

    # ===== Chat 对话大模型配置（DashScope OpenAI 兼容协议） =====
    # 阿里云百炼 API-Key，用于大模型对话接口鉴权；若为空则可能复用 EMBEDDING_API_KEY
    chat_api_key: str = ""
    # DashScope 提供的 OpenAI 兼容协议服务基础请求地址，默认与 embedding 保持一致
    chat_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    # 对话生成所使用的主力大语言模型（如 qwen-plus, qwen-max 等）
    chat_model: str = "qwen-plus"

    # ===== 知识检索与多轮问答控制策略 =====
    # 检索候选数（Top-K）：单次向向量数据库召回并最终喂给 LLM 上下文的最大 Chunk 数量
    retrieval_top_k: int = 5
    # 拒答相似度阈值（Cosine Similarity = 1 - Cosine Distance）：
    # 设定判定下限（默认 0.6 属于相对严格的标准）。
    # 若召回的 Top-K 候选中最高相似度得分仍低于此阈值，则判定无相关知识依据，直接触发拒答，不再调用 LLM
    retrieval_min_score: float = 0.6
    # 多轮会话记忆窗口大小：在 load_context 阶段截取当前会话最近 N 条历史消息塞入 Prompt，平衡长程记忆与上下文长度限制
    chat_history_window: int = 5

    # ===== Query 优化配置（第 5 期） =====
    # 策略路由总开关：
    #   置 false 时 route_query 节点固定输出 original，方便对「有/无路由」的效果做对比。
    #   关闭后整条链路退化回上一期的单路检索行为，是排查"优化是否真的有效"的第一手段。
    query_route_enabled: bool = True
    # 多查询策略生成的子查询数量（默认 3）：
    #   数量越大召回面越广，但每条子查询都要单独算一次向量（成本与耗时随 N 线性增长）。
    multi_query_count: int = 3

    # ===== 混合检索配置（第 6 期） =====
    # 每路（向量 / 关键词）召回数量；设计文档建议候选 20-50。
    # 取 20 兼顾召回率与 RRF 融合开销：
    #   recall_top_k 是「召回率 vs 融合开销」的取舍——召回数量越多，融合越能纠正
    #   单路的不足，但 SQL 排序、网络 I/O、Python 侧 dict 迭代都会变慢。
    #   且它必须【大于】retrieval_top_k，否则两路没有足够候选参与融合，
    #   RRF 会退化成"对同一批结果重排"。
    retrieval_recall_top_k: int = 20
    # RRF 平滑常数，业界一般用 60；越小越偏向高排名条目
    #   （公式 score(d) = Σ 1/(k + rank_i(d))；k 越大越平滑、越依赖"两路都命中"）
    rrf_k: int = 60

    # ===== Agentic RAG =====
    # 关掉后图退化为单轮检索，作为单轮 vs agent 循环的对比开关。
    #   agent_loop_enabled=False 时整个图退化成「跑一轮就出图」，
    #   方便对比开 / 关 agent 循环的效果（与第 5 期 query_route_enabled 同一思路：
    #   给一条"退化回上一期行为"的退路，是排查"优化是否真的有效"的第一手段）。
    agent_loop_enabled: bool = True
    # 最大检索轮次（含首轮）。LLM 决策最多触发 max_rounds-1 次再检索，避免循环调用。
    #   【为什么必须是硬上限】：
    #   没有它，一个"永远觉得召回不够好"的决策器会让循环无限转下去，
    #   每轮一次 LLM 调用 + 两路检索，成本与延迟都会失控。
    #   有了它，最坏情况也有界：3 轮意味着最多 1 次首轮 + 2 次 LLM 决策。
    agent_max_rounds: int = 3

    # ===== Reranker（DashScope qwen3-rerank，第 8 章）=====
    # 关掉后 rerank 节点直接透传，作为「有 / 无精排」的对比开关。
    #   与 agent_loop_enabled / query_route_enabled 同一思路：
    #   给一条"退化回上一期行为"的退路，才能量化"精排到底带来了多少提升"。
    rerank_enabled: bool = True
    # DashScope rerank 端点：不是标准 OpenAI API。
    #   【为什么端点路径不同】OpenAI 兼容协议里没有 rerank 这一类接口，
    #   百炼为精排单独提供了 /api/v1/services/rerank/... 形态的端点，
    #   因此不能复用 embedding_base_url / chat_base_url 那两个 compatible-mode 地址。
    rerank_base_url: str = (
        "https://dashscope.aliyuncs.com/compatible-api/v1/reranks"
    )
    rerank_model: str = "qwen3-rerank"
    # 留空时复用 chat_api_key（同一份 DashScope key，避免重复配置）。
    #   实际取值请用下面的 effective_rerank_api_key 属性，不要直接用本字段。
    rerank_api_key: str = ""
    # rerank Top1 相关度阈值：低于此值视为"上下文不足"由 judge_context 触发拒答。
    #   【⚠️ 为什么必须与 retrieval_min_score 分开】：
    #   qwen3-rerank 输出的是 relevance_score ∈ [0, 1]（精排模型给出的成对相关性），
    #   而 retrieval_min_score 比较的是 embedding 余弦相似度 —— 两者【量纲不同、不可比】。
    #   0.3 是经验值：精排分整体偏高，若沿用 0.6 会导致拒答几乎不触发。
    rerank_min_score: float = 0.3
    # 请求超时（秒）：rerank 是同步调用主链路，超时短路比拖慢回答更好。
    rerank_timeout: float = 8.0

    # ===== 答案校验（第 8 章）=====
    # 关掉后跳过 verify_answer 调用，方便对比"有 / 无引用支撑校验"的效果。
    verify_answer_enabled: bool = True

    # 将方法伪装成属性调用（getter），允许通过 obj.effective_rerank_api_key 形式只读访问，无需加括号 ()
    @property
    def effective_rerank_api_key(self) -> str:
        """rerank_api_key 留空时回落到 chat_api_key。二者本来就是同一份 DashScope key。"""
        return self.rerank_api_key or self.chat_api_key


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取配置单例实例。

    使用 lru_cache 装饰器缓存首次实例化的对象，
    避免在应用运行期间多次重复读取磁盘上的 .env 文件与解析环境变量。
    """
    return Settings()


# 实例化并暴露全局可直接调用的 settings 单例对象
settings = get_settings()