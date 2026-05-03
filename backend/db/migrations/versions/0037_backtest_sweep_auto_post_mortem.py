"""backtest_sweeps.auto_post_mortem — auto-trigger post-mortem after conclude

Revision ID: 0037
Revises: 0036
Create Date: 2026-05-03

PR #274 shipped the sweep with auto-conclude but NOT auto-post-mortem.
Operators kicking off 30-day sweeps overnight wanted the post-mortem
self-critique to run as part of each spawned discussion's lifecycle
so they wake up to fully-reviewed verdicts instead of having to
manually click 「事後檢討」 30 times.

Default TRUE for new sweeps — by the time you're running a
multi-day backtest you almost always want the self-critique
attached. Operator can toggle off for cost-sensitive runs (each
post-mortem ≈ N personas × 1 round + 1 synthesize = ~50% cost
overhead per sweeped date).

Per-date semantics:
  1. Worker creates Discussion, runs M rounds, synthesizes conclusion.
  2. If `auto_post_mortem=True` AND the post-mortem service finds
     ≥ 1 trading day past as_of_date in the archive: inject the
     critique prompt, run one more round, re-synthesize (routes
     to `post_mortem_conclusion` per PR #272). Failures here are
     logged but don't fail the date — the original conclusion is
     still valid.
  3. Date counts as completed.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "backtest_sweeps",
        sa.Column(
            "auto_post_mortem", sa.Boolean(),
            nullable=False, server_default=sa.text("true"),
        ),
    )


def downgrade() -> None:
    op.drop_column("backtest_sweeps", "auto_post_mortem")
