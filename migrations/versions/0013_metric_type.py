"""metric_samples.metric_type: OTLP instrument semantics (#79)

Additive, nullable column recording the OTLP instrument type
("gauge" | "counter" | "sum" | "histogram"). Null for sources that don't carry
type (e.g. RCAEval's melted gauge-like columns). Lets the anomaly layer
rate-normalize cumulative counters instead of comparing raw cumulative means.

Revision ID: 0013_metric_type
Revises: 0012_telemetry_tables
Create Date: 2026-09-12
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013_metric_type"
down_revision: Union[str, None] = "0012_telemetry_tables"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.add_column("metric_samples", sa.Column("metric_type", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("metric_samples", "metric_type")
