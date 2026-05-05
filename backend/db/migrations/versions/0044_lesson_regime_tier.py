"""discussion_lessons — regime tag + memory tier (PR-B0)

Revision ID: 0044
Revises: 0043
Create Date: 2026-05-05

Adds the lesson layering substrate: each lesson now carries the
market `regime` it was learned in (computed at write time from
`taiwan_vix.value` + TAIEX 5d/20d MA cross), a `tier` label
(episodic / semantic / structural) governing recency decay, and
usage/outcome counters that PR-B2's promotion rule reads to lift
proven lessons into longer-lived tiers.

This migration is **schema only**: no behavior change. PR-B0 starts
writing `regime` at extraction time but `tier` stays at the default
'episodic' for every existing row, leaving the current 60-day-decay
fetch behavior intact. PR-B1 starts using `regime` to bias fetch,
PR-B2 starts promoting to higher tiers.

The composite index `(market, tier, regime, as_of_date)` is sized
for PR-B1's hot path: filter by market + tier band, then match
regime, then take the most recent N (B-tree indexes handle the
descending scan natively, so no `DESC` needed). The existing
`(owner_user_id, market, as_of_date)` index stays — owner-scoped
queries still hit it first.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "discussion_lessons",
        sa.Column("regime", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "discussion_lessons",
        sa.Column(
            "tier", sa.String(length=16),
            nullable=False, server_default="episodic",
        ),
    )
    op.add_column(
        "discussion_lessons",
        sa.Column(
            "usage_count", sa.Integer(),
            nullable=False, server_default="0",
        ),
    )
    op.add_column(
        "discussion_lessons",
        sa.Column(
            "hit_count", sa.Integer(),
            nullable=False, server_default="0",
        ),
    )
    op.add_column(
        "discussion_lessons",
        sa.Column(
            "last_used_at", sa.DateTime(timezone=True), nullable=True,
        ),
    )
    op.add_column(
        "discussion_lessons",
        sa.Column(
            "promoted_at", sa.DateTime(timezone=True), nullable=True,
        ),
    )
    op.create_index(
        "ix_discussion_lessons_market_tier_regime_asof",
        "discussion_lessons",
        ["market", "tier", "regime", "as_of_date"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_discussion_lessons_market_tier_regime_asof",
        table_name="discussion_lessons",
    )
    op.drop_column("discussion_lessons", "promoted_at")
    op.drop_column("discussion_lessons", "last_used_at")
    op.drop_column("discussion_lessons", "hit_count")
    op.drop_column("discussion_lessons", "usage_count")
    op.drop_column("discussion_lessons", "tier")
    op.drop_column("discussion_lessons", "regime")
