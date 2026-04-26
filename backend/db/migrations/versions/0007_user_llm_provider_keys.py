"""add user_llm_provider_keys table

Revision ID: 0007
Revises: 0006
Create Date: 2026-04-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_llm_provider_keys",
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True,
        ),
        sa.Column("provider", sa.String(32), primary_key=True),
        sa.Column("encrypted_key", sa.Text(), nullable=False),
        sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_validation_ok", sa.Boolean(), nullable=True),
        sa.Column("last_validation_message", sa.String(500), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("user_llm_provider_keys")
