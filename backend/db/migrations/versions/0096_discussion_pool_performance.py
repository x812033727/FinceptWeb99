"""add discussions.pool_performance

Revision ID: 0096
Revises: 0095

Verify-time snapshot of how the (sequence-1) candidate pool performed
over the same 5-trading-day window the verdict is graded on:
{"avg_return_pct": float, "resolved": int, "pool_size": int}. Lets the
public scoreboard compare the AI's picked symbols against the whole
deterministic screener pool without re-fetching OHLCV. NULL for rows
verified before this feature or without a stored pool.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0096"
down_revision: str | None = "0095"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "discussions",
        sa.Column("pool_performance", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("discussions", "pool_performance")
