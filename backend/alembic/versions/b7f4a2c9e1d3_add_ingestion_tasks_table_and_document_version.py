"""add ingestion_tasks table and document version

Revision ID: b7f4a2c9e1d3
Revises: 4f198960f636
Create Date: 2026-09-27 17:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'b7f4a2c9e1d3'
down_revision: Union[str, Sequence[str], None] = '4f198960f636'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # --------------------------------------------------------------------------
    # 1. documents 新增 version 列
    # --------------------------------------------------------------------------
    # 【为什么必须带 server_default='1'】
    # 这一列是 NOT NULL，而 documents 表里已经有 11 行存量数据。
    # 不给数据库侧默认值时，PostgreSQL 会因为"已有行无法满足非空约束"直接拒绝 ALTER TABLE。
    # 给了之后，存量行会被一次性回填为 1。
    # （第 11 期给 users 补 updated_at 时踩过同一个坑，这里直接照方抓药。）
    op.add_column(
        'documents',
        sa.Column(
            'version',
            sa.Integer(),
            server_default='1',
            nullable=False,
            comment='文档内容版本号，每次重建索引成功后 +1',
        ),
    )

    # --------------------------------------------------------------------------
    # 2. 新建 ingestion_tasks 任务台账表
    # --------------------------------------------------------------------------
    # 【为什么没有 drop_index('ix_documents_permission_tags')】
    # 教程的 autogenerate 产物里有这一行，原因是教程侧的这个 GIN 索引只写在迁移脚本里、
    # 模型侧没声明，于是 Alembic 的自动比对看不见它，把它当成"数据库里多出来的东西"要删掉。
    # 本项目的第 11 期已经把 ix_documents_permission_tags 显式声明进了
    # Document.__table_args__，所以本次 autogenerate 不会生成那一行，这里也不需要手工删。
    op.create_table(
        'ingestion_tasks',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('document_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('task_type', sa.String(length=16), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('retry_count', sa.Integer(), nullable=False),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('progress_total', sa.Integer(), nullable=False),
        sa.Column('progress_done', sa.Integer(), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    # 单列索引：来自模型里的 index=True
    op.create_index(
        'ix_ingestion_tasks_document_id',
        'ingestion_tasks',
        ['document_id'],
        unique=False,
    )
    # 复合索引 (document_id, created_at DESC)：
    # 作用是优化「每个文档获取最新 ingestion 任务」的查询速度 ——
    # 也就是仓储层 get_latest_by_document 那条 ORDER BY created_at DESC LIMIT 1。
    # DESC 必须显式写出来：B-Tree 默认升序，升序索引上做 DESC 扫描虽然也能用，
    # 但排序方向不一致时无法直接走"索引即有序"的快路径。
    op.create_index(
        'ix_ingestion_tasks_document_created',
        'ingestion_tasks',
        ['document_id', sa.text('created_at DESC')],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_ingestion_tasks_document_created', table_name='ingestion_tasks')
    op.drop_index('ix_ingestion_tasks_document_id', table_name='ingestion_tasks')
    op.drop_table('ingestion_tasks')
    op.drop_column('documents', 'version')
