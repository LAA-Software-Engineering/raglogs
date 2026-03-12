"""initial schema

Revision ID: 0001_initial
Revises: 
Create Date: 2026-01-01 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op
import pgvector

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("type", sa.String(50), nullable=False, server_default="file"),
        sa.Column("config_json", postgresql.JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "ingestion_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("status", sa.String(50), nullable=False, server_default="pending"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("file_count", sa.Integer, server_default="0"),
        sa.Column("line_count", sa.Integer, server_default="0"),
        sa.Column("error_count", sa.Integer, server_default="0"),
        sa.Column("parsed_count", sa.Integer, server_default="0"),
        sa.Column("metadata_json", postgresql.JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "log_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sources.id"), nullable=True),
        sa.Column("ingestion_job_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("ingestion_jobs.id"), nullable=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("service", sa.String(255), nullable=True),
        sa.Column("environment", sa.String(100), nullable=True),
        sa.Column("level", sa.String(50), nullable=True),
        sa.Column("trace_id", sa.String(255), nullable=True),
        sa.Column("request_id", sa.String(255), nullable=True),
        sa.Column("host", sa.String(255), nullable=True),
        sa.Column("raw_message", sa.Text, nullable=True),
        sa.Column("normalized_message", sa.Text, nullable=True),
        sa.Column("fingerprint", sa.String(64), nullable=True),
        sa.Column("parser_type", sa.String(50), nullable=True),
        sa.Column("extra_json", postgresql.JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_log_entries_timestamp", "log_entries", ["timestamp"])
    op.create_index("ix_log_entries_service", "log_entries", ["service"])
    op.create_index("ix_log_entries_environment", "log_entries", ["environment"])
    op.create_index("ix_log_entries_level", "log_entries", ["level"])
    op.create_index("ix_log_entries_fingerprint", "log_entries", ["fingerprint"])
    op.create_index("ix_log_entries_trace_id", "log_entries", ["trace_id"])
    op.create_index("ix_log_entries_request_id", "log_entries", ["request_id"])
    op.create_index("ix_log_entries_timestamp_service", "log_entries", ["timestamp", "service"])

    op.create_table(
        "log_embeddings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("log_entry_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("log_entries.id"), nullable=False, unique=True),
        sa.Column("embedding", pgvector.sqlalchemy.Vector(1536), nullable=False),
        sa.Column("model_name", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "cluster_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("service_filter", sa.String(255), nullable=True),
        sa.Column("environment_filter", sa.String(100), nullable=True),
        sa.Column("algorithm", sa.String(50), server_default="fingerprint"),
        sa.Column("status", sa.String(50), server_default="completed"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "clusters",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("cluster_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("cluster_runs.id"), nullable=False),
        sa.Column("cluster_key", sa.String(64), nullable=False),
        sa.Column("representative_message", sa.Text, nullable=True),
        sa.Column("fingerprint", sa.String(64), nullable=True),
        sa.Column("count", sa.Integer, server_default="0"),
        sa.Column("services_json", postgresql.JSONB, nullable=True),
        sa.Column("levels_json", postgresql.JSONB, nullable=True),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=True),
        sa.Column("baseline_count", sa.Integer, server_default="0"),
        sa.Column("change_ratio", sa.Float, server_default="1.0"),
        sa.Column("importance_score", sa.Float, server_default="0.0"),
        sa.Column("cluster_summary", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "cluster_members",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("cluster_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("clusters.id"), nullable=False),
        sa.Column("log_entry_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("log_entries.id"), nullable=False),
    )

    op.create_table(
        "explanations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("service_filter", sa.String(255), nullable=True),
        sa.Column("environment_filter", sa.String(100), nullable=True),
        sa.Column("mode", sa.String(20), server_default="rules"),
        sa.Column("prompt_hash", sa.String(64), nullable=True),
        sa.Column("result_text", sa.Text, nullable=True),
        sa.Column("result_json", postgresql.JSONB, nullable=True),
        sa.Column("confidence", sa.String(50), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "app_config",
        sa.Column("key", sa.String(255), primary_key=True),
        sa.Column("value_json", postgresql.JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_table("app_config")
    op.drop_table("explanations")
    op.drop_table("cluster_members")
    op.drop_table("clusters")
    op.drop_table("cluster_runs")
    op.drop_table("log_embeddings")
    op.drop_table("log_entries")
    op.drop_table("ingestion_jobs")
    op.drop_table("sources")
