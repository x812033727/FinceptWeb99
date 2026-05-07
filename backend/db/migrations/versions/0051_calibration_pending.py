"""strategy templates — calibration_pending_* deployment gate (PR-3)

Revision ID: 0051
Revises: 0050
Create Date: 2026-05-07

PR-C2 (`fit_isotonic_for_strategy`) overwrites
`calibration_curve` directly when a sweep finishes. There's no
guard against a freshly-fitted curve that's misshapen — endpoint
inversion (calibrated(0.95) < calibrated(0.5)), a wild jump from
the prior curve at any control point, or a brier-degradation
signal from the recent sample window. PR-3 adds a deployment gate:
when the new curve fails any safety check, it lands in a *pending*
slot for admin review instead of overwriting the live one.

Three new columns on `discussion_strategy_templates`:

  - `calibration_pending_curve` JSONB — same shape as
    `calibration_curve`, NULL when no review is queued.
  - `calibration_pending_reason` TEXT — human-readable summary of
    which safety checks failed. Surfaced in the AdminPage review
    card so the operator knows why the curve was held.
  - `calibration_pending_at` TIMESTAMP TZ — when the curve was
    queued. Lets the UI sort review queue oldest-first.

Existing rows stay NULL on all three columns. Existing
`calibration_curve` is unaffected — old curves keep serving
predictions until either (a) a fresh fit deploys cleanly, or
(b) an admin approves the pending one.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0051"
down_revision: str | None = "0050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "discussion_strategy_templates",
        sa.Column(
            "calibration_pending_curve",
            postgresql.JSONB(astext_type=sa.Text()).with_variant(
                sa.JSON(), "sqlite",
            ),
            nullable=True,
        ),
    )
    op.add_column(
        "discussion_strategy_templates",
        sa.Column(
            "calibration_pending_reason",
            sa.Text(),
            nullable=True,
        ),
    )
    op.add_column(
        "discussion_strategy_templates",
        sa.Column(
            "calibration_pending_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("discussion_strategy_templates", "calibration_pending_at")
    op.drop_column("discussion_strategy_templates", "calibration_pending_reason")
    op.drop_column("discussion_strategy_templates", "calibration_pending_curve")
