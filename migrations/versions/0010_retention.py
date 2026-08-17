"""retention: CASCADE FKs, scope_retention, scope on cluster_runs/explanations

Revision ID: 0010_retention
Revises: 0009_cluster_embeddings
Create Date: 2026-08-17 00:00:00.000000

Raw purge deletes ``log_entries``; ``log_embeddings`` and ``cluster_members``
follow via ON DELETE CASCADE. Cluster summaries/embeddings stay until the
summary TTL. Per-scope overrides live in ``scope_retention``.
"""

from typing import Optional, Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010_retention"
down_revision: Union[str, None] = "0009_cluster_embeddings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CASCADE_FKS: tuple[tuple[str, str, str, str], ...] = (
    ("log_embeddings", "log_entry_id", "log_entries", "id"),
    ("cluster_members", "log_entry_id", "log_entries", "id"),
    ("cluster_members", "cluster_id", "clusters", "id"),
    ("clusters", "cluster_run_id", "cluster_runs", "id"),
)


def _fk_name(table: str, column: str) -> Optional[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for fk in inspector.get_foreign_keys(table):
        if fk.get("constrained_columns") == [column]:
            name = fk.get("name")
            if isinstance(name, str) and name:
                return name
    return f"{table}_{column}_fkey"


def _recreate_fk(
    table: str,
    column: str,
    referent_table: str,
    referent_column: str,
    *,
    ondelete: Optional[str],
) -> None:
    name = _fk_name(table, column)
    op.drop_constraint(name, table, type_="foreignkey")
    op.create_foreign_key(
        name,
        table,
        referent_table,
        [column],
        [referent_column],
        ondelete=ondelete,
    )


def upgrade() -> None:
    for table, column, referent_table, referent_column in _CASCADE_FKS:
        _recreate_fk(table, column, referent_table, referent_column, ondelete="CASCADE")

    op.add_column(
        "cluster_runs",
        sa.Column("scope", sa.String(255), nullable=False, server_default="default"),
    )
    op.create_index("ix_cluster_runs_scope", "cluster_runs", ["scope"])
    op.add_column(
        "explanations",
        sa.Column("scope", sa.String(255), nullable=False, server_default="default"),
    )
    op.create_index("ix_explanations_scope", "explanations", ["scope"])

    # Majority scope from remaining cluster membership (G8). Rows with no
    # members stay at the server default ``default``.
    op.execute(
        sa.text(
            """
            UPDATE cluster_runs AS cr
            SET scope = src.scope
            FROM (
                SELECT DISTINCT ON (counted.cluster_run_id)
                    counted.cluster_run_id,
                    counted.scope
                FROM (
                    SELECT c.cluster_run_id, le.scope, COUNT(*) AS n
                    FROM clusters c
                    JOIN cluster_members cm ON cm.cluster_id = c.id
                    JOIN log_entries le ON le.id = cm.log_entry_id
                    GROUP BY c.cluster_run_id, le.scope
                ) AS counted
                ORDER BY counted.cluster_run_id, counted.n DESC, counted.scope
            ) AS src
            WHERE cr.id = src.cluster_run_id
            """
        )
    )
    # Best-effort: explanations inherit the majority log_entries.scope in the
    # cached window (service/env filters when present).
    op.execute(
        sa.text(
            """
            UPDATE explanations AS e
            SET scope = src.scope
            FROM (
                SELECT DISTINCT ON (counted.explanation_id)
                    counted.explanation_id,
                    counted.scope
                FROM (
                    SELECT e2.id AS explanation_id, le.scope, COUNT(*) AS n
                    FROM explanations e2
                    JOIN log_entries le
                      ON le.timestamp >= e2.window_start
                     AND le.timestamp <= e2.window_end
                     AND (e2.service_filter IS NULL OR le.service = e2.service_filter)
                     AND (
                        e2.environment_filter IS NULL
                        OR le.environment = e2.environment_filter
                     )
                    GROUP BY e2.id, le.scope
                ) AS counted
                ORDER BY counted.explanation_id, counted.n DESC, counted.scope
            ) AS src
            WHERE e.id = src.explanation_id
            """
        )
    )

    op.create_table(
        "scope_retention",
        sa.Column("scope", sa.String(255), primary_key=True),
        sa.Column("raw_interval", sa.String(32), nullable=True),
        sa.Column("summary_interval", sa.String(32), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    op.drop_table("scope_retention")
    op.drop_index("ix_explanations_scope", table_name="explanations")
    op.drop_column("explanations", "scope")
    op.drop_index("ix_cluster_runs_scope", table_name="cluster_runs")
    op.drop_column("cluster_runs", "scope")
    for table, column, referent_table, referent_column in reversed(_CASCADE_FKS):
        _recreate_fk(table, column, referent_table, referent_column, ondelete=None)
