"""trace_spans (scope, start_time) covering index for silent-service evidence (#184)

Silent-service evidence (Phase F) aggregates ``count() GROUP BY service`` over a
``(scope, [baseline_start, window_end])`` range with **no** service predicate, so
the existing ``(scope, service, start_time)`` index can't serve it. This additive
index gives the range scan a supported access path, and INCLUDEs ``service`` so the
aggregate is index-only (no heap fetch) rather than reading raw span rows.

Revision ID: 0014_span_scope_start_idx
Revises: 0013_metric_type
Create Date: 2026-09-21
"""
from typing import Union

from alembic import op

revision: str = "0014_span_scope_start_idx"
down_revision: Union[str, None] = "0013_metric_type"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.create_index(
        "ix_trace_spans_scope_start", "trace_spans", ["scope", "start_time"], unique=False,
        postgresql_include=["service"],
    )


def downgrade() -> None:
    op.drop_index("ix_trace_spans_scope_start", table_name="trace_spans")
