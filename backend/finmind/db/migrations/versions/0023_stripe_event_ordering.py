"""track the latest Stripe event applied to each subscription

Revision ID: 0023
Revises: 0022
Create Date: 2026-07-16

Stripe does not guarantee webhook delivery order. Persisting Event.created
prevents a delayed older snapshot from regressing access after a newer
payment or subscription-state event has already been applied.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column("last_external_event_created_at", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("subscriptions", "last_external_event_created_at")
