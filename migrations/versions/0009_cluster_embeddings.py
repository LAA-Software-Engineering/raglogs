"""cluster embeddings table and ANN indexes on vector columns

Revision ID: 0009_cluster_embeddings
Revises: 0008_scope_isolation
Create Date: 2026-08-17 00:00:00.000000

Adds ``cluster_embeddings`` keyed by ``(scope, fingerprint)`` so similar-incident
search can look up historical cluster templates. Creates an ANN index on
``log_embeddings.embedding`` (missing since 0001) and on the new cluster
vector column. Prefers HNSW (safe on empty tables); IVFFlat ``lists=1`` is
the fallback when HNSW is unavailable.
"""

from typing import Sequence, Union

import pgvector.sqlalchemy
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import context, op

revision: str = "0009_cluster_embeddings"
down_revision: Union[str, None] = "0008_scope_isolation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

LOG_EMBEDDING_ANN_INDEX = "ix_log_embeddings_embedding_ann"
CLUSTER_EMBEDDING_ANN_INDEX = "ix_cluster_embeddings_embedding_ann"


def _create_ann_index(table: str, column: str, index_name: str) -> None:
    """Create HNSW if the pgvector build supports it; else IVFFlat lists=1."""
    hnsw_sql = (
        f"CREATE INDEX IF NOT EXISTS {index_name} ON {table} "
        f"USING hnsw ({column} vector_cosine_ops)"
    )
    ivf_sql = (
        f"CREATE INDEX IF NOT EXISTS {index_name} ON {table} "
        f"USING ivfflat ({column} vector_cosine_ops) WITH (lists = 1)"
    )
    if context.is_offline_mode():
        # Offline SQL cannot probe the server; HNSW is the documented default
        # (pgvector 0.5+, including the pgvector/pgvector:pg16 image).
        op.execute(sa.text(hnsw_sql))
        return

    conn = op.get_bind()
    try:
        with conn.begin_nested():
            conn.execute(sa.text(hnsw_sql))
        return
    except Exception:
        pass
    conn.execute(sa.text(ivf_sql))


def upgrade() -> None:
    op.create_table(
        "cluster_embeddings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("scope", sa.String(255), nullable=False, server_default="default"),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("template", sa.Text, nullable=True),
        sa.Column("embedding", pgvector.sqlalchemy.Vector(1536), nullable=False),
        sa.Column("model_name", sa.String(255), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column("count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint(
            "scope",
            "fingerprint",
            name="ux_cluster_embeddings_scope_fingerprint",
        ),
    )
    op.create_index(
        "ix_cluster_embeddings_fingerprint",
        "cluster_embeddings",
        ["fingerprint"],
    )
    op.create_index(
        "ix_cluster_embeddings_scope",
        "cluster_embeddings",
        ["scope"],
    )
    _create_ann_index("log_embeddings", "embedding", LOG_EMBEDDING_ANN_INDEX)
    _create_ann_index("cluster_embeddings", "embedding", CLUSTER_EMBEDDING_ANN_INDEX)


def downgrade() -> None:
    op.drop_index(CLUSTER_EMBEDDING_ANN_INDEX, table_name="cluster_embeddings")
    op.drop_index(LOG_EMBEDDING_ANN_INDEX, table_name="log_embeddings")
    op.drop_index("ix_cluster_embeddings_scope", table_name="cluster_embeddings")
    op.drop_index("ix_cluster_embeddings_fingerprint", table_name="cluster_embeddings")
    op.drop_table("cluster_embeddings")
