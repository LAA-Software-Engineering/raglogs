"""phase2: worker_jobs table, explanation cache, ingestion_jobs error_message

Revision ID: 0002_phase2
Revises: 0001_initial
Create Date: 2026-03-12 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision: str = "0002_phase2"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Worker job queue — decouples API from ingestion runtime
    op.create_table(
        "worker_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("job_type", sa.String(50), nullable=False),          # "ingest"
        sa.Column("status", sa.String(50), nullable=False, server_default="pending"),  # pending|running|done|failed
        sa.Column("payload_json", postgresql.JSONB, nullable=False),   # ingest params
        sa.Column("result_json", postgresql.JSONB, nullable=True),     # on completion
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("ingestion_job_id", postgresql.UUID(as_uuid=True),   # set after job created
                  sa.ForeignKey("ingestion_jobs.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_worker_jobs_status", "worker_jobs", ["status"])
    op.create_index("ix_worker_jobs_created_at", "worker_jobs", ["created_at"])

    # Add error_message to ingestion_jobs for richer failure reporting
    op.add_column("ingestion_jobs", sa.Column("error_message", sa.Text, nullable=True))

    # Composite index on explanations for cache lookups
    op.create_index(
        "ix_explanations_window_filters",
        "explanations",
        ["window_start", "window_end", "service_filter", "environment_filter"],
    )


def downgrade() -> None:
    op.drop_index("ix_explanations_window_filters", table_name="explanations")
    op.drop_column("ingestion_jobs", "error_message")
    op.drop_index("ix_worker_jobs_created_at", table_name="worker_jobs")
    op.drop_index("ix_worker_jobs_status", table_name="worker_jobs")
    op.drop_table("worker_jobs")
