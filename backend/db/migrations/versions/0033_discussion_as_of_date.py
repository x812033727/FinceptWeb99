"""discussions.as_of_date — backtest mode anchor

Revision ID: 0033
Revises: 0032
Create Date: 2026-05-02

NULL = live mode (current behaviour, ctx + verdict use today's data).
A non-null date = "pretend it's that date":

  - `gather_market_context` filters every block to data with
    `ts <= as_of_date` / `published_at <= as_of_date`. So the
    personas reason on the same evidence they would have had on
    that day, not data that leaked back from the future.
  - The verifier computes verdict from the 5 trading days AFTER
    `as_of_date` instead of `created_at.date()` — so a discussion
    created today against `as_of_date='2025-01-15'` is verified
    against 2025-01-20 ~ 2025-01-22 closes.

Together this lets us replay arbitrary historical days through the
expert-discussion stack and grade the personas / context blocks /
prompt design against actual outcomes, instead of only the rolling
forward window the auto-run cron produces.

Plain Date column, no index — the verifier still picks rows up via
the `verify_after_date` index already on the table.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "discussions",
        sa.Column("as_of_date", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("discussions", "as_of_date")
