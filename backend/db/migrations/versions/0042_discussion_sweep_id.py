"""discussions.sweep_id — back-pointer to the spawning sweep

Revision ID: 0042
Revises: 0041
Create Date: 2026-05-05

PR-B's aggregation dashboard groups child discussions by their
parent sweep. Without an explicit FK we'd have to guess the link
from {owner_id, topic, rules, market, as_of_date} which breaks
when two sweeps share a strategy template + overlapping anchor
windows.

ON DELETE SET NULL so deleting a sweep doesn't cascade-kill the
spawned discussions (operators may still want to inspect the
discussion threads after a stale sweep is purged).

The column is nullable: legacy discussions created before this
migration (and any live, non-sweep discussion) keep sweep_id=NULL.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "discussions",
        sa.Column(
            "sweep_id", UUID(as_uuid=True),
            sa.ForeignKey("backtest_sweeps.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_discussions_sweep_id",
        "discussions", ["sweep_id"], unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_discussions_sweep_id", table_name="discussions")
    op.drop_column("discussions", "sweep_id")
