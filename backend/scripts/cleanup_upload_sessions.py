"""清理过期的文档直传会话与临时 COS 对象。

【运行方式】：
python -m scripts.cleanup_upload_sessions

【脚本边界】：
脚本只负责创建数据库会话并调用业务服务，具体查询、状态更新和 COS 删除逻辑
统一收敛在 UploadSessionRepository 与 DocumentUploadService，避免维护脚本复制业务规则。
"""

import asyncio

from app.db.session import AsyncSessionLocal
from app.services.document_upload_service import DocumentUploadService


async def cleanup_upload_sessions() -> int:
    """调用文档直传服务清理过期会话，并返回处理数量。"""
    async with AsyncSessionLocal() as session:
        return await DocumentUploadService(session).cleanup_expired()


def main() -> None:
    """维护命令入口，输出本次实际处理的会话数量。"""
    print(f"cleaned upload sessions: {asyncio.run(cleanup_upload_sessions())}")


if __name__ == "__main__":
    main()
