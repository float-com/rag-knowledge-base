"""add hybrid retrieval support

Revision ID: 8c44f95568ad
Revises: 960400bbd4f4
Create Date: 2026-09-20 10:00:00.000000

【本期目标】：为混合检索准备数据库侧能力——中文全文检索 + 检索调试元数据。

【前置依赖（重要）】：
本迁移的 `CREATE EXTENSION zhparser` 要求 PostgreSQL 镜像里【已经带 zhparser 二进制】。
默认的 pgvector/pgvector:pg16 不含该扩展，直接升级会报：
    ERROR: could not open extension control file .../zhparser.control
因此需要先把 docker-compose 的 postgres 镜像换成自带 zhparser 的镜像，
或自建镜像（多阶段构建 SCWS + zhparser）。详见 Day06 归档文档。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '8c44f95568ad'
down_revision: Union[str, Sequence[str], None] = '960400bbd4f4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # ==============================================================================
    # 1. 装载 zhparser 扩展并创建中文文本检索配置
    # ==============================================================================
    # 【为什么必须先 CREATE EXTENSION】：
    # 镜像里存在的是【扩展文件】（zhparser.so / .control / .sql），
    # 但数据库集群（pgdata 数据卷内）并未装载该扩展，二者是两回事：
    #   - pg_available_extensions 能看到它  → 说明文件在位
    #   - pg_extension 里没有它             → 说明未装载
    # 未装载时 CREATE TEXT SEARCH CONFIGURATION 会直接报错：
    #   ERROR: text search parser "zhparser" does not exist
    # “装载”这个动作只能在数据库内用 SQL 完成，也就是本迁移这一步。
    op.execute("CREATE EXTENSION IF NOT EXISTS zhparser")

    # 【为什么用 DO $$ ... EXCEPTION 包一层】：
    # PostgreSQL 的 CREATE TEXT SEARCH CONFIGURATION 【不支持 IF NOT EXISTS】，
    # 重复执行会报 duplicate_object 并中断整个迁移。
    # 用异常捕获实现幂等，便于回滚重跑或换镜像后重试。
    op.execute(
        """
        DO $$ BEGIN
            CREATE TEXT SEARCH CONFIGURATION chinese_zh (PARSER = zhparser);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
        """
    )

    # 【ADD MAPPING FOR n,v,a,i,e,l WITH simple 的含义】：
    # zhparser 会给每个切出的词标注词性（名词/动词/副词/形容词/习用语/动词等），
    # 这里把其中的 n(名词) v(动词) a(形容词) i(习用语) e(叹词) l(习用语) 六类
    # 映射到 simple 词典，其余词性默认被丢弃。
    # 教程选择这种映射，是因为它能让后续 tsquery 对中文关键词的匹配更稳定。
    op.execute(
        """
        DO $$ BEGIN
            ALTER TEXT SEARCH CONFIGURATION chinese_zh
                ADD MAPPING FOR n,v,a,i,e,l WITH simple;
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
        """
    )

    # ==============================================================================
    # 2. 给 document_chunks 增加全文检索向量列与 GIN 索引
    # ==============================================================================
    # 【GENERATED ALWAYS AS ... STORED 的两个细节】：
    # 1. GENERATED ALWAYS：由数据库自动维护的生成列。
    #    PostgreSQL 会根据 content 列自动计算它，应用【无需在 INSERT 时显式写入】，
    #    从根上杜绝“写入新 chunk 时忘记维护 tsvector 列”导致的检索结果错乱。
    # 2. STORED：物化存储，占磁盘但不重复计算，查询更快（VIRTUAL 类型不存储，
    #    每次查询都要重算，无法建 GIN 索引）。
    op.execute(
        "ALTER TABLE document_chunks "
        "ADD COLUMN content_tsv tsvector "
        "GENERATED ALWAYS AS (to_tsvector('chinese_zh', content)) STORED"
    )

    # 【GIN 索引】：倒排索引，把 tsvector 里的每个 token 反向映射到文档列表，
    # 是全文检索的标准索引类型（原理类似书籍末尾的“索引页”）。
    op.execute(
        "CREATE INDEX ix_document_chunks_content_tsv "
        "ON document_chunks USING GIN (content_tsv)"
    )

    # ==============================================================================
    # 3. 增加检索调试元数据列（存混合检索的调试信息）
    # ==============================================================================
    # 用于记录每条引用“到底是哪条路召回的、各路排名如何、RRF 融合分是多少”，
    # 供线上排查 Bad Case 与前端调试面板使用。
    op.add_column(
        "answer_citations",
        sa.Column(
            "retrieval_meta",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="混合检索调试元数据（sources / 各路排名与分数 / rrf_score）",
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    # 回滚顺序与 upgrade 严格相反，保证回滚干净。
    # 一般情况下用不到。

    # 3. 先删调试元数据列
    op.drop_column("answer_citations", "retrieval_meta")

    # 2. 再删生成列与 GIN 索引
    #    注意：DROP COLUMN 会自动级联删除依赖它的索引，
    #    这里显式先删索引是为了回滚语义更明确、错误更易定位。
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_content_tsv")
    op.execute("ALTER TABLE document_chunks DROP COLUMN IF EXISTS content_tsv")

    # 1. 最后删中文检索配置与扩展
    op.execute("DROP TEXT SEARCH CONFIGURATION IF EXISTS chinese_zh")
    op.execute("DROP EXTENSION IF EXISTS zhparser")
