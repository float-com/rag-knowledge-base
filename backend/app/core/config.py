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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取配置单例实例。

    使用 lru_cache 装饰器缓存首次实例化的对象，
    避免在应用运行期间多次重复读取磁盘上的 .env 文件与解析环境变量。
    """
    return Settings()


# 实例化并暴露全局可直接调用的 settings 单例对象
settings = get_settings()