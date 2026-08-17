"""api_keys.config_json for per-key query override defaults (G14)

Revision ID: 0011_api_key_config
Revises: 0010_retention
Create Date: 2026-08-17 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0011_api_key_config"
down_revision: Union[str, None] = "0010_retention"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "api_keys",
        sa.Column("config_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("api_keys", "config_json")
