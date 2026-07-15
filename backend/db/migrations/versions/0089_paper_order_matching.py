"""add paper-order time in force and expiry

Revision ID: 0089
Revises: 0088
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0089"
down_revision: str | None = "0088"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "paper_orders",
        sa.Column("time_in_force", sa.String(length=3), server_default="day", nullable=False),
    )
    op.add_column("paper_orders", sa.Column("expires_at", sa.DateTime(timezone=True)))
    op.add_column("paper_orders", sa.Column("expired_at", sa.DateTime(timezone=True)))
    op.create_check_constraint(
        "ck_paper_orders_time_in_force", "paper_orders", "time_in_force IN ('day', 'gtc')"
    )
    op.drop_constraint("ck_paper_orders_status", "paper_orders", type_="check")
    op.create_check_constraint(
        "ck_paper_orders_status",
        "paper_orders",
        "status IN ('pending', 'partially_filled', 'filled', 'cancelled', 'expired')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_paper_orders_status", "paper_orders", type_="check")
    op.create_check_constraint(
        "ck_paper_orders_status",
        "paper_orders",
        "status IN ('pending', 'partially_filled', 'filled', 'cancelled')",
    )
    op.drop_constraint("ck_paper_orders_time_in_force", "paper_orders", type_="check")
    op.drop_column("paper_orders", "expired_at")
    op.drop_column("paper_orders", "expires_at")
    op.drop_column("paper_orders", "time_in_force")
