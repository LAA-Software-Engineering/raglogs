"""telemetry tables: trace_spans + metric_samples (#118 multi-modal RCA)

Revision ID: 0012_telemetry_tables
Revises: 0011_api_key_config
Create Date: 2026-09-09 00:00:00.000000

Additive telemetry tables for multi-modal root-cause analysis (#118). Both are
independent of ``log_entries`` (joined only by scope + service + time window),
carry an optional ``attributes`` JSONB escape hatch for real span/metric
dimensions, and are scoped like every other table (G8). No existing table or
behaviour changes; logs remain the default/fallback signal.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0012_telemetry_tables"
down_revision: Union[str, None] = "0011_api_key_config"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "trace_spans",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "ingestion_job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ingestion_jobs.id"),
            nullable=True,
        ),
        sa.Column("trace_id", sa.String(255), nullable=True),
        sa.Column("span_id", sa.String(255), nullable=True),
        sa.Column("parent_span_id", sa.String(255), nullable=True),
        sa.Column("service", sa.String(255), nullable=True),
        sa.Column("operation", sa.Text, nullable=True),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Float, nullable=True),
        sa.Column("status_code", sa.String(50), nullable=True),
        sa.Column("attributes", postgresql.JSONB, nullable=True),
        sa.Column("scope", sa.String(255), nullable=False, server_default="default"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_trace_spans_scope_service_start",
        "trace_spans",
        ["scope", "service", "start_time"],
    )
    op.create_index("ix_trace_spans_scope_trace", "trace_spans", ["scope", "trace_id"])

    op.create_table(
        "metric_samples",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "ingestion_job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ingestion_jobs.id"),
            nullable=True,
        ),
        sa.Column("service", sa.String(255), nullable=True),
        sa.Column("metric", sa.String(255), nullable=False),
        sa.Column("value", sa.Float, nullable=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attributes", postgresql.JSONB, nullable=True),
        sa.Column("scope", sa.String(255), nullable=False, server_default="default"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_metric_samples_scope_service_metric_ts",
        "metric_samples",
        ["scope", "service", "metric", "ts"],
    )


def downgrade() -> None:
    op.drop_index("ix_metric_samples_scope_service_metric_ts", table_name="metric_samples")
    op.drop_table("metric_samples")
    op.drop_index("ix_trace_spans_scope_trace", table_name="trace_spans")
    op.drop_index("ix_trace_spans_scope_service_start", table_name="trace_spans")
    op.drop_table("trace_spans")
