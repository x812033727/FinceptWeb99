"""discussion_strategy_templates — calibration curve (PR-C2)

Revision ID: 0048
Revises: 0047
Create Date: 2026-05-05

PR-C2 closes the C-axis loop: PR-C0 emitted per-pick confidence,
PR-C1 graded each one against D1-D5 outcomes, and now PR-C2 fits
an isotonic regression over the rolling pool of (raw, outcome)
pairs and persists the curve here so the synthesizer can
calibrate future emissions with one lookup.

Schema additions on `discussion_strategy_templates`:

  - `calibration_curve` JSONB — sorted list of
    `[{"raw": float, "calibrated": float}, ...]` control points.
    Empty list / NULL when the strategy hasn't accumulated
    `MIN_SAMPLES_FOR_FIT` (=30) outcome pairs yet.

  - `calibration_updated_at` TIMESTAMPTZ — when the curve was
    last fitted; the sweep worker's Phase 3 hook updates this
    after each completed sweep.

  - `calibration_sample_count` INTEGER — pool size at the last
    fit, useful for the operator to reason about "is the curve
    stable?" in the admin UI.

Additive migration: every existing row stays NULL, no behavioral
change until the C2 worker hook starts populating.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "discussion_strategy_templates",
        sa.Column("calibration_curve", sa.JSON(), nullable=True),
    )
    op.add_column(
        "discussion_strategy_templates",
        sa.Column(
            "calibration_updated_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "discussion_strategy_templates",
        sa.Column(
            "calibration_sample_count", sa.Integer(), nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column(
        "discussion_strategy_templates", "calibration_sample_count",
    )
    op.drop_column(
        "discussion_strategy_templates", "calibration_updated_at",
    )
    op.drop_column(
        "discussion_strategy_templates", "calibration_curve",
    )
