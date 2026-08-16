"""source provenance: source_adapter/source_ref on ingestion_jobs + log_entries

Revision ID: 0003_source_provenance
Revises: 0002_phase2
Create Date: 2026-08-16 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_source_provenance"
down_revision: Union[str, None] = "0002_phase2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Provenance — which adapter/stream a job or row came from. server_default backfills
    # existing rows as "file" since every prior ingest was file-based.
    op.add_column(
        "ingestion_jobs",
        sa.Column("source_adapter", sa.String(50), nullable=False, server_default="file"),
    )
    op.add_column("ingestion_jobs", sa.Column("source_ref", sa.Text, nullable=True))

    op.add_column(
        "log_entries",
        sa.Column("source_adapter", sa.String(50), nullable=False, server_default="file"),
    )
    op.add_column("log_entries", sa.Column("source_ref", sa.Text, nullable=True))
    op.create_index("ix_log_entries_source_adapter", "log_entries", ["source_adapter"])


def downgrade() -> None:
    op.drop_index("ix_log_entries_source_adapter", table_name="log_entries")
    op.drop_column("log_entries", "source_ref")
    op.drop_column("log_entries", "source_adapter")
    op.drop_column("ingestion_jobs", "source_ref")
    op.drop_column("ingestion_jobs", "source_adapter")
