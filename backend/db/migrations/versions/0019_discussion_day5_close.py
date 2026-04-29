"""discussion day-5 close prices

Revision ID: 0019
Revises: 0018
Create Date: 2026-04-29

Adds `discussions.day5_close_prices` so the sidebar can render
`{day1_open}/{day5_close}` per symbol with a close-to-close gain
percentage. The verifier already snapshots day1 opens (column added
in 0018); this migration completes the matching pair.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "discussions",
        sa.Column("day5_close_prices", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("discussions", "day5_close_prices")
