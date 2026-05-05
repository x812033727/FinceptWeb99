"""discussion_strategy_templates: auto-schedule fields

Revision ID: 0043
Revises: 0042
Create Date: 2026-05-05

PR-D closes the auto-backtest loop: once a template is trained
(PR-C learned weights), the operator wants future sweeps to
launch automatically without coming back to the UI. These fields
gate that:

- auto_schedule_enabled: opt-in (default false; explicit toggle)
- auto_schedule_cadence_hours: minimum gap between auto-launches
  (default 24 = once per day)
- auto_schedule_anchor_offset_days: anchor relative to today
  (default -1 = yesterday — one fully-resolved trading day ago)
- auto_schedule_trading_days_count: how many trading days each
  auto-sweep covers (default 1 — one verifying day per cadence)
- auto_schedule_last_run_at: when the dispatcher last fired for
  this template; the dispatcher's "due" predicate is
  `enabled AND (last IS NULL OR last < now() - cadence)`.

The dispatcher itself is an APScheduler job registered in
`tasks.scheduler.setup_jobs`, fires every 5 min, and is a no-op
unless at least one template is due. Skips templates that already
have a non-terminal sweep in flight to avoid pile-on if the
previous run is slow.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "discussion_strategy_templates",
        sa.Column(
            "auto_schedule_enabled", sa.Boolean(),
            nullable=False, server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "discussion_strategy_templates",
        sa.Column(
            "auto_schedule_cadence_hours", sa.Integer(),
            nullable=False, server_default="24",
        ),
    )
    op.add_column(
        "discussion_strategy_templates",
        sa.Column(
            "auto_schedule_anchor_offset_days", sa.Integer(),
            nullable=False, server_default="-1",
        ),
    )
    op.add_column(
        "discussion_strategy_templates",
        sa.Column(
            "auto_schedule_trading_days_count", sa.Integer(),
            nullable=False, server_default="1",
        ),
    )
    op.add_column(
        "discussion_strategy_templates",
        sa.Column(
            "auto_schedule_last_run_at", sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_strategy_templates_auto_schedule_due",
        "discussion_strategy_templates",
        ["auto_schedule_enabled", "auto_schedule_last_run_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_strategy_templates_auto_schedule_due",
        table_name="discussion_strategy_templates",
    )
    op.drop_column(
        "discussion_strategy_templates", "auto_schedule_last_run_at",
    )
    op.drop_column(
        "discussion_strategy_templates", "auto_schedule_trading_days_count",
    )
    op.drop_column(
        "discussion_strategy_templates", "auto_schedule_anchor_offset_days",
    )
    op.drop_column(
        "discussion_strategy_templates", "auto_schedule_cadence_hours",
    )
    op.drop_column(
        "discussion_strategy_templates", "auto_schedule_enabled",
    )
