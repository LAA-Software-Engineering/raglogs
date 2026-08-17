"""ingest idempotency keys and log-entry content dedup

Revision ID: 0007_ingest_idempotency
Revises: 0006_api_key_webhook_secret
Create Date: 2026-08-17 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0007_ingest_idempotency"
down_revision: Union[str, None] = "0006_api_key_webhook_secret"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "log_entries",
        sa.Column("original_line_hash", sa.String(64), nullable=True),
    )
    op.add_column(
        "log_entries",
        sa.Column("scope", sa.String(255), nullable=False, server_default="default"),
    )
    op.create_index(
        "ux_log_entries_dedup",
        "log_entries",
        ["scope", "source_ref", "original_line_hash", "timestamp"],
        unique=True,
        postgresql_where=sa.text(
            "original_line_hash IS NOT NULL AND timestamp IS NOT NULL "
            "AND source_ref IS NOT NULL"
        ),
    )
    op.create_table(
        "ingest_idempotency_keys",
        sa.Column("key", sa.String(256), primary_key=True),
        sa.Column(
            "worker_job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("worker_jobs.id"),
            nullable=True,
        ),
        sa.Column(
            "ingestion_job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ingestion_jobs.id"),
            nullable=True,
        ),
        sa.Column("mode", sa.String(20), nullable=False, server_default="batch"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_ingest_idempotency_keys_expires_at",
        "ingest_idempotency_keys",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ingest_idempotency_keys_expires_at",
        table_name="ingest_idempotency_keys",
    )
    op.drop_table("ingest_idempotency_keys")
    op.drop_index("ux_log_entries_dedup", table_name="log_entries")
    op.drop_column("log_entries", "scope")
    op.drop_column("log_entries", "original_line_hash")
