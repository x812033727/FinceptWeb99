"""multi-strategy daily auto-runs

Revision ID: 0073
Revises: 0072
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0073"
down_revision: str | None = "0072"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "discussion_auto_run_configs", sa.Column("strategy_run_counts", sa.JSON(), nullable=True)
    )
    op.execute(
        """UPDATE discussion_auto_run_configs SET strategy_run_counts = CASE WHEN enabled THEN '{\"general\":1,\"chip_momentum\":0,\"quality_growth\":0,\"breakout\":0,\"oversold_reversal\":0}' ELSE '{\"general\":0,\"chip_momentum\":0,\"quality_growth\":0,\"breakout\":0,\"oversold_reversal\":0}' END"""
    )
    op.alter_column("discussion_auto_run_configs", "strategy_run_counts", nullable=False)
    op.add_column("discussions", sa.Column("auto_run_strategy", sa.String(32), nullable=True))
    op.add_column("discussions", sa.Column("auto_run_sequence", sa.Integer(), nullable=True))
    op.add_column("discussions", sa.Column("auto_run_date", sa.Date(), nullable=True))
    op.add_column("discussions", sa.Column("candidate_snapshot", sa.JSON(), nullable=True))
    op.create_index(
        "uq_discussions_auto_run_slot",
        "discussions",
        ["owner_id", "auto_run_date", "auto_run_strategy", "auto_run_sequence"],
        unique=True,
        postgresql_where=sa.text("auto_run_strategy IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_discussions_auto_run_slot", table_name="discussions")
    for name in ("candidate_snapshot", "auto_run_date", "auto_run_sequence", "auto_run_strategy"):
        op.drop_column("discussions", name)
    op.drop_column("discussion_auto_run_configs", "strategy_run_counts")
