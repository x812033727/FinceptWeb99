"""add market_provider_keys table

Revision ID: 0010
Revises: 0009
Create Date: 2026-04-27

Mirrors `llm_provider_keys` but for market-data API keys (Finnhub today,
Polygon / FRED / FinMind tomorrow). One row per provider, Fernet-encrypted
key blob, validation metadata. Admin UI writes here so the operator
doesn't have to redeploy the container to rotate a key.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "market_provider_keys",
        sa.Column("provider", sa.String(32), primary_key=True),
        sa.Column("encrypted_key", sa.Text(), nullable=False),
        sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_validation_ok", sa.Boolean(), nullable=True),
        sa.Column("last_validation_message", sa.String(500), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column(
            "updated_by_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_table("market_provider_keys")
