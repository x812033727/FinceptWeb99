"""discussions.market + discussion_auto_run_configs.market

Revision ID: 0029
Revises: 0028
Create Date: 2026-05-02

Until this migration `gather_market_context` was hard-coded to TW from
two call sites (`run_round`, `synthesize_conclusion`) regardless of the
discussion's actual subject matter — a topic naming NVDA / TSLA still
got TAIEX 漲跌榜 + 法人籌碼. The Discussion entity didn't even carry a
market column to disambiguate.

This migration:
  - adds `discussions.market` (NOT NULL, default 'TW' for back-compat)
  - adds `discussion_auto_run_configs.market` (same defaulting)

Server default is set on the column so legacy rows fill cleanly without
a separate UPDATE; the application layer enforces the value comes from
{TW, US, GLOBAL} on insert/update.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "discussions",
        sa.Column(
            "market",
            sa.String(length=8),
            nullable=False,
            server_default="TW",
        ),
    )
    op.add_column(
        "discussion_auto_run_configs",
        sa.Column(
            "market",
            sa.String(length=8),
            nullable=False,
            server_default="TW",
        ),
    )


def downgrade() -> None:
    op.drop_column("discussion_auto_run_configs", "market")
    op.drop_column("discussions", "market")
