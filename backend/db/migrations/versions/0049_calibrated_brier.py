"""discussions — calibrated_brier_score for PR-C2 measurement (PR-C2 follow-up)

Revision ID: 0049
Revises: 0048
Create Date: 2026-05-05

PR-C1 added `brier_score` over the raw synthesizer confidence;
PR-C2 layered a calibration curve on top so each pick gets a
`calibrated_confidence`. To answer "is calibration actually
reducing error?" we need a Brier score over the calibrated values
alongside the raw — comparing the two over time is the
operationally meaningful signal that PR-C2's curve is improving
predictions.

This migration only adds the column; PR-C2-followup-2's scoreboard
service will start computing + persisting it. Existing rows stay
NULL (no data was calibrated before this).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0049"
down_revision: str | None = "0048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "discussions",
        sa.Column("calibrated_brier_score", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("discussions", "calibrated_brier_score")
