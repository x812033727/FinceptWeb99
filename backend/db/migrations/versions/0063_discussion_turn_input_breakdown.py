"""discussion_turns: add input_breakdown for per-section/per-block ctx usage

Revision ID: 0063
Revises: 0062
Create Date: 2026-06-04

Adds a nullable JSON `input_breakdown` column to `discussion_turns` so the
per-round "ctx 用量明細" view can show each persona turn's prompt
composition — char size per prompt section (system / context_json /
history / instructions / ...) and per top-level context block
(focus_briefs / news_sentiment / ...). Written at turn-persist time from
`measure_prompt_breakdown`. NULL for turns recorded before this column
existed and for error / timeout placeholder turns that never assembled a
prompt.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0063"
down_revision: str | None = "0062"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "discussion_turns",
        sa.Column("input_breakdown", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("discussion_turns", "input_breakdown")
