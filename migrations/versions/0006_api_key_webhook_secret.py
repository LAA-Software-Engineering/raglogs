"""per-key webhook signing secret on api_keys

Revision ID: 0006_api_key_webhook_secret
Revises: 0005_ingest_modes
Create Date: 2026-08-17 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0006_api_key_webhook_secret"
down_revision: Union[str, None] = "0005_ingest_modes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "api_keys",
        sa.Column("webhook_secret", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("api_keys", "webhook_secret")
