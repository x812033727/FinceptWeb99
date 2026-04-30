"""discussions.daily_close_prices — D1-D5 daily close array

Revision ID: 0024
Revises: 0023
Create Date: 2026-04-30

Extends the existing day1_open_prices / day5_close_prices pair (PRs
#18, #19) with a per-day series so the discussion detail page can
render an "對答案" view: for each recommended symbol, day-1 open vs
each of the next 5 trading days' closes (with change %).

Schema:
  daily_close_prices JSON nullable
  Shape: {symbol: [d1_close, d2_close, d3_close, d4_close, d5_close]}
         Slots are NULL when a day hasn't resolved yet (cron retries
         the next morning until the window fills).

Plain JSON column, no index — reads always go through the parent
discussion's PK lookup. The existing day5_close_prices stays
populated for backward compat with `formatDiscussionTitle` in the
frontend sidebar.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "discussions",
        sa.Column("daily_close_prices", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("discussions", "daily_close_prices")
