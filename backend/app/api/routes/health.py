"""
【模块职责说明】
本模块为系统健康检查（Health Check）路由接口层，核心作用如下：

1. 多维度分级探活：
   提供三级监控端点，分别针对不同系统层面进行可用性探测：
   - /health/：基础应用存活性探测（Liveness），验证 Python/FastAPI 进程正常运行。
   - /health/db：数据库连通性检测，执行高频极轻量的 "SELECT 1" 探测连接池与事务就绪状态。
   - /health/cos：对象存储可达性检测，结合 settings.cos_configured 状态，若未配置返回明确标识，
     已配置则通过轻量级 head_bucket 验证网络通信与鉴权凭据有效性。

2. 类型收敛与前端工程化适配：
   - 响应数据模型统一封装为 HealthStatus，采用 Literal 枚举限定 status 状态字符串，
     确保 FastAPI 自动生成的 OpenAPI (Swagger) Schema 包含严谨的类型定义。
   - 显式声明 operation_id（如 healthApp、healthDb、healthCos），为后续前端根据 OpenAPI 规范
     自动生成强类型 TypeScript SDK 客户端函数提供规范统一的语义化方法命名。
"""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text

from app.api.deps import DbSession
from app.core.config import settings
from app.core.logging import get_logger
from app.storage.cos_client import get_cos_client

# 获取当前模块的专属 Logger 实例，用于记录健康检测失败时的调用异常
logger = get_logger(__name__)

# 定义健康检测专用路由组
router = APIRouter(prefix="/health", tags=["health"])

# 健康状态枚举联合类型：通过 Literal 约束前端生成的 TS 类型推断
HealthStatusValue = Literal["ok", "error", "not_configured"]


class HealthStatus(BaseModel):
    """健康检查标准统一响应模型。"""

    status: HealthStatusValue  # 当前组件状态：正常、异常或未配置
    detail: str | None = None  # 状态异常时的具体排查报错描述信息


@router.get("", response_model=HealthStatus, operation_id="healthApp")
async def health() -> HealthStatus:
    """应用进程存活性探测端点。

    快速返回 ok，供负载均衡器、反向代理（如 Nginx）或 K8s 验证主程序事件循环存活。
    """
    return HealthStatus(status="ok")


@router.get("/db", response_model=HealthStatus, operation_id="healthDb")
async def health_db(session: DbSession) -> HealthStatus:
    """PostgreSQL 数据库连通性检测端点。

    利用依赖注入获取异步数据库会话，执行 'SELECT 1' 纯内存极速查询。
    若连接失败或池耗尽，则捕获异常记录堆栈并返回具体的错误明细。
    """
    try:
        result = await session.execute(text("SELECT 1"))
        result.scalar_one()
        return HealthStatus(status="ok")
    except Exception as exc:
        logger.exception("db health check failed")
        return HealthStatus(status="error", detail=str(exc))


@router.get("/cos", response_model=HealthStatus, operation_id="healthCos")
async def health_cos() -> HealthStatus:
    """腾讯云 COS 对象存储连通性与凭据检测端点。

    若未填写 COS 凭据变量则状态回退为 'not_configured'；
    若已配置则调用 ping() 方法向腾讯云发送 head_bucket 请求验证鉴权与桶状态。
    """
    # 凭据前置自检
    if not settings.cos_configured:
        return HealthStatus(
            status="not_configured",
            detail="COS 凭据未在 .env 中配置",
        )

    try:
        ok = await get_cos_client().ping()
    except Exception as exc:
        logger.exception("cos health check failed")
        return HealthStatus(status="error", detail=str(exc))

    return (
        HealthStatus(status="ok")
        if ok
        else HealthStatus(status="error", detail="head_bucket check failed")
    )