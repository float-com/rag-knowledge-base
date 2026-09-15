"""add chat tables and pgvector hnsw index

Revision ID: 960400bbd4f4
Revises: c4f8a2e1d9b7
Create Date: 2026-09-15 09:52:05.359846

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '960400bbd4f4'
down_revision: Union[str, Sequence[str], None] = 'c4f8a2e1d9b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # ==============================================================================
    # 1. 自动生成：创建会话、消息与知识引用溯源三张核心表
    # ==============================================================================
    op.create_table('conversations',
    sa.Column('id', sa.UUID(), nullable=False, comment='会话全局唯一UUID主键'),
    sa.Column('title', sa.String(length=256), nullable=False, comment='会话展示标题'),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='会话创建时间'),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='会话最近更新时间'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('messages',
    sa.Column('id', sa.UUID(), nullable=False, comment='消息全局唯一UUID主键'),
    sa.Column('conversation_id', sa.UUID(), nullable=False, comment='所属会话ID（外键级联删除）'),
    sa.Column('role', sa.String(length=16), nullable=False, comment='消息角色 (user / assistant / system)'),
    sa.Column('content', sa.Text(), nullable=False, comment='消息正文内容'),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=False, comment='扩展元数据（记录模型、Token开销、耗时等）'),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='消息创建时间'),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_messages_conversation_id'), 'messages', ['conversation_id'], unique=False)
    op.create_table('answer_citations',
    sa.Column('id', sa.UUID(), nullable=False, comment='引用全局唯一UUID主键'),
    sa.Column('message_id', sa.UUID(), nullable=False, comment='所属消息ID（级联删除）'),
    sa.Column('ordinal', sa.Integer(), nullable=False, comment='上下文注入序号(1..N)，对应大模型生成的[N]角标'),
    sa.Column('document_id', sa.UUID(), nullable=True, comment='关联文档ID（原文档被删时置空）'),
    sa.Column('chunk_id', sa.UUID(), nullable=True, comment='关联切片ID（原切片被删时置空）'),
    sa.Column('document_name', sa.String(length=512), nullable=False, comment='文档名称快照'),
    sa.Column('page_no', sa.Integer(), nullable=True, comment='当时所在物理页码快照'),
    sa.Column('quote', sa.Text(), nullable=False, comment='引用的原始文本片段快照'),
    sa.ForeignKeyConstraint(['chunk_id'], ['document_chunks.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['message_id'], ['messages.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_answer_citations_message_id'), 'answer_citations', ['message_id'], unique=False)
    op.drop_index(op.f('ix_upload_sessions_status_expires_at'), table_name='upload_sessions')

    # ==============================================================================
    # 2. 手动补充：创建 pgvector HNSW 向量近似最近邻索引
    # ==============================================================================
    # 【技术考量】：
    # 1. 算法选型：HNSW (Hierarchical Navigable Small World) 是 pgvector 推荐的高性能近邻图算法，
    #    在大规模向量检索场景下比 IVFFlat 拥有更高的召回率（Recall）和更平稳的查询延迟。
    # 2. 距离度量策略：使用 vector_cosine_ops（余弦相似度距离），完美契合 OpenAI/DashScope
    #    归一化文本嵌入向量的语义夹角检索计算。
    # 3. 参数策略：m（最大双向链路数）与 ef_construction（构造搜索深度）先使用 pgvector 默认参数，
    #    兼顾建索引吞吐与内存占用，后续根据数据量规模调参。
    op.execute(
        "CREATE INDEX ix_document_chunks_embedding_hnsw "
        "ON document_chunks USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    """Downgrade schema."""
    # ==============================================================================
    # 1. 优先回滚：物理移除 HNSW 向量索引（避免表删除过程中级联挂起或锁定）
    # ==============================================================================
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_embedding_hnsw")

    # ==============================================================================
    # 2. 回滚数据表与原有索引重建
    # ==============================================================================
    op.create_index(op.f('ix_upload_sessions_status_expires_at'), 'upload_sessions', ['status', 'expires_at'], unique=False)
    op.drop_index(op.f('ix_answer_citations_message_id'), table_name='answer_citations')
    op.drop_table('answer_citations')
    op.drop_index(op.f('ix_messages_conversation_id'), table_name='messages')
    op.drop_table('messages')
    op.drop_table('conversations')