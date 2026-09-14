"""文档直传接口的数据传输对象。"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


UploadSessionStatusValue = Literal[
    "initiated", "uploaded", "finalizing", "completed", "expired", "failed", "aborted"
]


class InitUploadRequest(BaseModel):
    """初始化文档直传会话时提交的文件元数据。"""

    file_name: str = Field(min_length=1, max_length=512)
    size: int = Field(gt=0)
    mime_type: str = Field(min_length=1, max_length=128)
    permission_tags: list[str] = Field(default_factory=list)


class UploadSessionRead(BaseModel):
    """对外返回的上传会话安全 DTO。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    object_key: str
    original_name: str
    mime_type: str
    expected_size: int
    status: UploadSessionStatusValue
    expires_at: datetime
    completed_at: datetime | None = None
    error_message: str | None = None


class InitUploadResponse(UploadSessionRead):
    """初始化接口响应，额外携带临时预签名 PUT URL。"""

    presigned_url: str
