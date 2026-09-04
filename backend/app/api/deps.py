"""FastAPI 依赖项汇总。"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session

# ===== 依赖项类型别名（FastAPI 推荐的 Annotated 语法糖） =====
#
# 【设计目的】：
# 将传统的参数依赖声明（如 session: AsyncSession = Depends(get_session)）
# 打包为一个全局可复用的类型别名，实现“类型提示”与“框架注入”的解耦与复用。
#
# 【语法糖拆解 - Annotated[T, Metadata]】：
# 1. 第一个参数 (AsyncSession)：
#    提供真实的静态类型信息，IDE（如 PyCharm）和 mypy 据此提供代码自动补全与类型检查。
# 2. 第二个参数 (Depends(get_session))：
#    作为元数据挂载。FastAPI 运行时会解析该元数据，自动触发 get_session 获取连接、
#    执行请求级注入并在请求结束后关闭连接。
#
# 【使用收益】：
# - 统一依赖源：若底层 Session 获取方式变更，仅需在此处修改一次即可全局生效；
# - 代码极其简洁：后续路由只需书写 `async def api_name(db: DbSession):`，避免重复声明长串依赖。
DbSession = Annotated[AsyncSession, Depends(get_session)]