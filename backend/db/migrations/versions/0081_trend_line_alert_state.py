"""add persisted runtime state for crossing alert rules

Revision ID: 0081
Revises: 0080
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0081"
down_revision: str | None = "0080"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "price_alerts",
        sa.Column("runtime_state", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("price_alerts", "runtime_state")
