"""link strategy templates to daily auto-run strategies

Revision ID: 0098
Revises: 0097

The health / calibration / maturity stack samples discussions through
`discussion_strategy_templates -> backtest_sweeps -> discussions.sweep_id`.
The daily roundtable uses none of that chain -- it keys off the three
strings in `discussion_auto_run_configs.strategy_run_counts` and leaves
`sweep_id` NULL -- so `monitor_strategy_health` walked an empty template
table every day and wrote nothing while reporting healthy.

This adds the nullable link column. Seeding the three rows is left to
`scripts/seed_daily_strategy_templates` rather than done here: it needs
an owner_id resolved from the enabled auto-run config, which is
deployment data, not schema.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0098"
down_revision: str | None = "0097"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "discussion_strategy_templates",
        sa.Column("auto_run_strategy", sa.String(length=32), nullable=True),
    )
    # Partial index: only the handful of auto-run-linked rows are ever
    # looked up by this column, and the sweep templates (the majority
    # in a mature deployment) would otherwise bloat it with NULLs.
    op.create_index(
        "ix_strategy_templates_auto_run",
        "discussion_strategy_templates",
        ["auto_run_strategy"],
        unique=False,
        postgresql_where=sa.text("auto_run_strategy IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_strategy_templates_auto_run",
        table_name="discussion_strategy_templates",
    )
    op.drop_column("discussion_strategy_templates", "auto_run_strategy")
