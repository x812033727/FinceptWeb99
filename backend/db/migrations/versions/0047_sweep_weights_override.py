"""backtest_sweeps — weights_override for walk-forward (PR-A1)

Revision ID: 0047
Revises: 0046
Create Date: 2026-05-05

Adds the `weights_override` JSON column so the walk-forward
orchestrator (PR-A1) can inject frozen persona weights into a
test fold without writing them back to the parent strategy
template. The column is consulted in `synthesize_conclusion`
ahead of the template-level `persona_weights` so the override
takes priority when present.

Format: `{persona_id: weight_float}`. NULL on every existing row
and on production sweeps — only walk-forward test folds set it.

This migration is purely additive; rolling back is safe.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "backtest_sweeps",
        sa.Column("weights_override", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("backtest_sweeps", "weights_override")
