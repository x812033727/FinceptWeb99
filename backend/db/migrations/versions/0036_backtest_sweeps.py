"""backtest_sweeps — automated multi-day backtest runner

Revision ID: 0036
Revises: 0035
Create Date: 2026-05-03

Manual backtest replays were previously one-at-a-time: pick a
date, hit Run Round, hit Conclude, repeat. Operators wanted to
sweep e.g. 30 trading days from 2026-01-01 with 1 round each in
the background. This table tracks those sweep jobs.

Schema:
  - id (uuid PK)
  - owner_id (FK to users) — sweeps are per-user just like
    discussions
  - status: pending / running / completed / cancelled / failed
  - anchor_date: first trading day to backtest (Date)
  - trading_days_count: how many trading days from anchor (int)
  - rounds_per_discussion: rounds to run on each (int)
  - concurrency: 1-3 — how many discussions to run in parallel
    (default 1 = serial, recommended for cost control)
  - topic / rules / persona_ids: template applied to every
    spawned discussion
  - market: TW / US / GLOBAL
  - resolved_dates: JSON array of the actual trading dates the
    worker resolved at start time. Empty until /start runs.
  - completed_dates: JSON array of dates whose discussions ran
    to conclusion successfully.
  - failed_dates: JSON array of {date, error} entries for dates
    whose discussion crashed mid-flight (kept so the operator
    can retry just the broken ones).
  - error_message: last fatal error if status=failed.
  - created_at, started_at, completed_at, cancelled_at — usual
    lifecycle timestamps.

The worker is a background asyncio task fired on /start.
Persistence here means a server restart mid-sweep can be picked
up by an admin (manual /start to re-resolve from
`completed_dates` and resume).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "backtest_sweeps",
        sa.Column(
            "id", sa.UUID(as_uuid=True),
            primary_key=True, nullable=False,
        ),
        sa.Column(
            "owner_id", sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False, index=True,
        ),
        sa.Column(
            "status", sa.String(length=16),
            nullable=False, server_default="pending",
        ),
        sa.Column("anchor_date", sa.Date(), nullable=False),
        sa.Column(
            "trading_days_count", sa.Integer(),
            nullable=False, server_default="5",
        ),
        sa.Column(
            "rounds_per_discussion", sa.Integer(),
            nullable=False, server_default="1",
        ),
        sa.Column(
            "concurrency", sa.Integer(),
            nullable=False, server_default="1",
        ),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("rules", sa.Text(), nullable=False),
        sa.Column("market", sa.String(length=12), nullable=False),
        sa.Column(
            "persona_ids", sa.JSON(),
            nullable=False, server_default="[]",
        ),
        sa.Column(
            "resolved_dates", sa.JSON(),
            nullable=False, server_default="[]",
        ),
        sa.Column(
            "completed_dates", sa.JSON(),
            nullable=False, server_default="[]",
        ),
        sa.Column(
            "failed_dates", sa.JSON(),
            nullable=False, server_default="[]",
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=True,
        ),
        sa.Column(
            "completed_at", sa.DateTime(timezone=True), nullable=True,
        ),
        sa.Column(
            "cancelled_at", sa.DateTime(timezone=True), nullable=True,
        ),
    )
    op.create_index(
        "ix_backtest_sweeps_owner_status",
        "backtest_sweeps",
        ["owner_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_backtest_sweeps_owner_status",
        table_name="backtest_sweeps",
    )
    op.drop_table("backtest_sweeps")
