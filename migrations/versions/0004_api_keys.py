"""api_keys table for hashed HTTP API credentials

Revision ID: 0004_api_keys
Revises: 0003_source_provenance
Create Date: 2026-08-17 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004_api_keys"
down_revision: Union[str, None] = "0003_source_provenance"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("key_prefix", sa.String(32), nullable=False),
        sa.Column("key_hash", sa.Text, nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("scope", sa.String(255), nullable=False, server_default="default"),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_api_keys_key_prefix", "api_keys", ["key_prefix"])


def downgrade() -> None:
    op.drop_index("ix_api_keys_key_prefix", table_name="api_keys")
    op.drop_table("api_keys")
