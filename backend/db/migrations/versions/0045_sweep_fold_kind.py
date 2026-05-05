"""backtest_sweeps — fold_kind metadata + parent linkage (PR-A0)

Revision ID: 0045
Revises: 0044
Create Date: 2026-05-05

Adds the metadata layer the walk-forward orchestrator (PR-A1) needs:

  - `fold_kind` declares whether a sweep is the train half of a
    walk-forward fold, the test half, or a vanilla production
    sweep run by the operator / auto-schedule. Default is
    'production' so existing sweeps and direct API callers don't
    need to change.

  - `parent_sweep_id` lets a `test` fold point back at its `train`
    fold. The aggregate UI uses this to render train-vs-test KPIs
    side by side; the walk-forward worker uses it to look up
    which weights to freeze before spawning the test sweep.

A check constraint pins fold_kind to the 3 valid values. The FK
is `ON DELETE SET NULL` rather than CASCADE — losing the parent
shouldn't wipe the child's results, the dangling pointer is
informational only.

This is a metadata-only migration: PR-A0 doesn't change worker
behavior. PR-A1's walk-forward orchestrator will flip
`fold_kind == 'test'` on the test sweep and skip the in-sample
weight learner that currently runs in Phase 3.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "backtest_sweeps",
        sa.Column(
            "fold_kind", sa.String(length=16),
            nullable=False, server_default="production",
        ),
    )
    op.add_column(
        "backtest_sweeps",
        sa.Column(
            "parent_sweep_id", UUID(as_uuid=True),
            sa.ForeignKey("backtest_sweeps.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_backtest_sweeps_fold_kind",
        "backtest_sweeps",
        "fold_kind IN ('train', 'test', 'production')",
    )
    op.create_index(
        "ix_backtest_sweeps_parent_sweep_id",
        "backtest_sweeps",
        ["parent_sweep_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_backtest_sweeps_parent_sweep_id",
        table_name="backtest_sweeps",
    )
    op.drop_constraint(
        "ck_backtest_sweeps_fold_kind",
        "backtest_sweeps",
        type_="check",
    )
    op.drop_column("backtest_sweeps", "parent_sweep_id")
    op.drop_column("backtest_sweeps", "fold_kind")
