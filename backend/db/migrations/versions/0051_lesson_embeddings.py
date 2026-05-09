"""discussion_lessons — semantic embedding for similarity retrieval (PR-J1)

Revision ID: 0051
Revises: 0050
Create Date: 2026-05-09

Adds the embedding substrate for the next-tier lesson retrieval.
Today the fetch ranking uses time decay × symbol overlap × regime
match × tier weight — but symbol overlap is exact-string only, so a
lesson titled "半導體高估值風險" never surfaces for a discussion
that names 2330 directly without happening to mention "半導體".
PR-J2 wires cosine similarity into the score so that semantic
match closes the gap.

Schema only — embeddings are written by `lesson_embedding_service`
out-of-band: inline best-effort at lesson-write time + a backfill
script (`scripts/backfill_lesson_embeddings.py`) for existing
rows. Until the service runs, every row has `embedding=NULL` which
the J2 retrieval path treats as "no semantic boost, fall back to
the legacy score" — so this migration is non-breaking.

Choice of JSONB over pgvector: tests run on in-memory SQLite, and
pgvector isn't a stock extension. The candidate window in
`fetch_relevant_lessons` is capped at 200 rows so Python cosine
over a JSONB list-of-floats is fast enough (a typical owner has
< 500 lessons after a year of usage).

`embedding_model` is persisted alongside so we can re-embed
subsets when the model upgrades (e.g. text-embedding-3-small ->
4-small) without re-doing the world. `embedded_at` distinguishes
"never embedded" from "embedding failed and we should retry"
when read by the backfill script.
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
        "discussion_lessons",
        sa.Column(
            "embedding",
            postgresql.JSONB(astext_type=sa.Text()).with_variant(
                sa.JSON(), "sqlite",
            ),
            nullable=True,
        ),
    )
    op.add_column(
        "discussion_lessons",
        sa.Column(
            "embedding_model", sa.String(length=64), nullable=True,
        ),
    )
    op.add_column(
        "discussion_lessons",
        sa.Column(
            "embedded_at", sa.DateTime(timezone=True), nullable=True,
        ),
    )
    # Partial index over the un-embedded rows so the backfill script
    # finds work efficiently as the table grows. PG-only — SQLite
    # ignores the WHERE clause but the create still succeeds.
    op.create_index(
        "ix_discussion_lessons_pending_embedding",
        "discussion_lessons",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("embedding IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_discussion_lessons_pending_embedding",
        table_name="discussion_lessons",
    )
    op.drop_column("discussion_lessons", "embedded_at")
    op.drop_column("discussion_lessons", "embedding_model")
    op.drop_column("discussion_lessons", "embedding")
