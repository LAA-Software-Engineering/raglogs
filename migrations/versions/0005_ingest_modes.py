"""ingestion job modes for batch / push / tail

Revision ID: 0005_ingest_modes
Revises: 0004_api_keys
Create Date: 2026-08-17 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0005_ingest_modes"
down_revision: Union[str, None] = "0004_api_keys"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ingestion_jobs",
        sa.Column("mode", sa.String(20), nullable=False, server_default="batch"),
    )
    op.add_column("ingestion_jobs", sa.Column("cursor", sa.Text(), nullable=True))
    op.add_column(
        "ingestion_jobs",
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "ingestion_jobs",
        sa.Column("consecutive_errors", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("ingestion_jobs", "consecutive_errors")
    op.drop_column("ingestion_jobs", "last_polled_at")
    op.drop_column("ingestion_jobs", "cursor")
    op.drop_column("ingestion_jobs", "mode")
