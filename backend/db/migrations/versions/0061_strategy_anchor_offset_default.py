"""strategy auto-schedule anchor offset default -1 → 0

Revision ID: 0061
Revises: 0060
Create Date: 2026-05-29

The strategy auto-sweep dispatcher previously defaulted
`auto_schedule_anchor_offset_days = -1`, anchoring every auto-run
discussion at "yesterday relative to today (UTC)". On a Taipei
afternoon tick (e.g. 17:30 local = 09:30 UTC) the TW EOD ingest
(14:30 local = 06:30 UTC) has already landed today's bar in
`ohlcv_daily`, but the dispatcher still skipped it. This migration
flips the column server_default to `0` so newly-created templates
opt in to "use today's data when available". Existing rows are
NOT touched — operators who deliberately set `-1` keep that value.

The runtime resolver (`strategy_auto_sweep_service._resolve_anchor`)
also gained a snap-back: when `offset_days=0` lands on a day
without a bar in `ohlcv_daily` (weekend, holiday, or pre-ingest
tick), it falls back to the latest available bar instead of
walking forward to nothing.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0061"
down_revision: str | None = "0060"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "discussion_strategy_templates",
        "auto_schedule_anchor_offset_days",
        existing_type=sa.Integer(),
        existing_nullable=False,
        server_default="0",
    )


def downgrade() -> None:
    op.alter_column(
        "discussion_strategy_templates",
        "auto_schedule_anchor_offset_days",
        existing_type=sa.Integer(),
        existing_nullable=False,
        server_default="-1",
    )
