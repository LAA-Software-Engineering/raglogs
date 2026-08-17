"""scope isolation: ingestion_jobs.scope, log_entries indexes, key override flag

Revision ID: 0008_scope_isolation
Revises: 0007_ingest_idempotency
Create Date: 2026-08-17 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0008_scope_isolation"
down_revision: Union[str, None] = "0007_ingest_idempotency"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ingestion_jobs",
        sa.Column("scope", sa.String(255), nullable=False, server_default="default"),
    )
    op.create_index(
        "ix_log_entries_scope_timestamp",
        "log_entries",
        ["scope", "timestamp"],
    )
    op.create_index(
        "ix_log_entries_scope_service_environment_fingerprint",
        "log_entries",
        ["scope", "service", "environment", "fingerprint"],
    )
    op.add_column(
        "api_keys",
        sa.Column(
            "allow_scope_override",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("api_keys", "allow_scope_override")
    op.drop_index(
        "ix_log_entries_scope_service_environment_fingerprint",
        table_name="log_entries",
    )
    op.drop_index("ix_log_entries_scope_timestamp", table_name="log_entries")
    op.drop_column("ingestion_jobs", "scope")
