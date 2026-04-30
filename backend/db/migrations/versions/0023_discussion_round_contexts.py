"""discussion_round_contexts — per-round context JSON snapshots

Revision ID: 0023
Revises: 0022
Create Date: 2026-04-30

`gather_market_context` is recomputed per round (top movers,
chip metrics, news sentiment, etc.). Until this migration the
assembled dict was streamed once via SSE and discarded; persisted
discussions only carried each persona's reply text. That meant
re-opening an old discussion couldn't show "what data the personas
saw at the time" — re-running gather_market_context returns the
*current* market state, not the historical snapshot.

This table stores one JSON blob per round so the discussion is
fully reproducible:

  - PK (discussion_id, round) — composite. A second write for the
    same round (e.g. a failed status reset that re-fires run_round)
    is an idempotent UPSERT, not a duplicate.
  - `context` is JSON-typed so the schema doesn't churn whenever
    we add a new context block (international_sentiment,
    top_revenue_growers, etc.).
  - ON DELETE CASCADE so deleting a discussion drops its contexts
    too; matches discussion_turns' existing FK behaviour.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True) if _is_postgres() else sa.String(36)

    op.create_table(
        "discussion_round_contexts",
        sa.Column(
            "discussion_id", uuid_type,
            sa.ForeignKey("discussions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.Column(
            "captured_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "discussion_id", "round",
            name="pk_discussion_round_contexts",
        ),
    )
    op.create_index(
        "ix_discussion_round_contexts_lookup",
        "discussion_round_contexts",
        ["discussion_id", "round"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_discussion_round_contexts_lookup",
        table_name="discussion_round_contexts",
    )
    op.drop_table("discussion_round_contexts")
