"""add persona_overrides table

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "persona_overrides",
        sa.Column("persona_id", sa.String(64), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column(
            "updated_by_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_table("persona_overrides")
