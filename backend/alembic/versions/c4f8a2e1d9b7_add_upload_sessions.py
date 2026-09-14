"""add upload sessions

【迁移职责说明】：
本迁移为 COS 预签名直传链路增加上传会话表。该表只记录上传过程，
不改变现有 documents、document_chunks 以及旧 multipart 上传表结构。

Revision ID: c4f8a2e1d9b7
Revises: b113db5ee4e1
Create Date: 2026-09-10 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c4f8a2e1d9b7"
down_revision: Union[str, Sequence[str], None] = "b113db5ee4e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """创建浏览器直传会话表。

    【字段设计】：
    - object_key：临时对象的完整 COS 路径，并设置唯一约束；
    - original_name / mime_type / suffix / expected_size：init 阶段确认的文件元数据；
    - permission_tags：直传阶段暂存的权限标签 JSON 数组；
    - status / expires_at：支持 complete、失败重试和过期清理；
    - completed_at / error_message：保存最终结果和失败上下文。

    【索引设计】：
    status + expires_at 联合索引服务于定时清理任务，避免扫描所有历史会话。
    """
    # 创建上传过程表；Document 只有在后台 finalize 成功后才会新增。
    op.create_table(
        "upload_sessions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("original_name", sa.String(length=512), nullable=False),
        sa.Column("mime_type", sa.String(length=128), nullable=False),
        sa.Column("suffix", sa.String(length=16), nullable=False),
        sa.Column("expected_size", sa.BigInteger(), nullable=False),
        sa.Column(
            "permission_tags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("object_key"),
    )
    # 清理命令按状态和过期时间筛选，这个联合索引覆盖其主要查询条件。
    op.create_index(
        op.f("ix_upload_sessions_status_expires_at"),
        "upload_sessions",
        ["status", "expires_at"],
        unique=False,
    )


def downgrade() -> None:
    """删除浏览器直传会话表。

    回滚只删除直传过程记录，不触碰已有 Document 和 COS 正式对象。
    """
    op.drop_index(op.f("ix_upload_sessions_status_expires_at"), table_name="upload_sessions")
    op.drop_table("upload_sessions")
