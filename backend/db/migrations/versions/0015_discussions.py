"""discussions + discussion_turns + news sentiment fields

Revision ID: 0015
Revises: 0014
Create Date: 2026-04-28

Adds the expert-discussion subsystem:

  - `discussions` — one row per round-table session. Stores the user-supplied
    topic + rules, the selected persona roster, current status, and the
    structured conclusion JSON (recommended_symbols / reasoning / risks /
    time_horizon) once the synthesizer has run.

  - `discussion_turns` — one row per persona utterance. `stance` is an
    enum-as-string (`agree` | `dissent` | `supplement`); `agree` rows
    typically have empty content (the persona "passed"). `citations`
    captures structured references the persona used (news article IDs,
    snapshot dates, screener hits) so the UI can link back to evidence.

Also extends `news_articles` with three sentiment columns populated by the
hourly scoring job (services/news_sentiment_service.py). Sentiment is
optional — articles ingested before the job ran will simply read NULL until
the next scoring pass picks them up.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    # ── news_articles sentiment columns ──────────────────────────
    op.add_column(
        "news_articles",
        sa.Column("sentiment_score", sa.Float(), nullable=True),
    )
    op.add_column(
        "news_articles",
        sa.Column("sentiment_label", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "news_articles",
        sa.Column("sentiment_scored_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_news_articles_sentiment_lookup",
        "news_articles",
        ["market", "sentiment_scored_at"],
        unique=False,
    )

    # ── discussions ──────────────────────────────────────────────
    uuid_type = postgresql.UUID(as_uuid=True) if _is_postgres() else sa.String(36)
    uuid_default = sa.text("uuid_generate_v4()") if _is_postgres() else None

    discussions_kwargs: dict = {}
    if uuid_default is not None:
        discussions_kwargs["server_default"] = uuid_default

    op.create_table(
        "discussions",
        sa.Column("id", uuid_type, primary_key=True, **discussions_kwargs),
        sa.Column(
            "owner_id",
            uuid_type,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("rules", sa.Text(), nullable=False),
        sa.Column("persona_ids", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="draft"),
        sa.Column("current_round", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("conclusion", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_discussions_owner_created",
        "discussions",
        ["owner_id", "created_at"],
        unique=False,
    )

    # ── discussion_turns ─────────────────────────────────────────
    op.create_table(
        "discussion_turns",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "discussion_id",
            uuid_type,
            sa.ForeignKey("discussions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("turn_index", sa.Integer(), nullable=False),
        sa.Column("persona_id", sa.String(length=64), nullable=False),
        sa.Column("stance", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("citations", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_discussion_turns_lookup",
        "discussion_turns",
        ["discussion_id", "round", "turn_index"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_discussion_turns_lookup", table_name="discussion_turns")
    op.drop_table("discussion_turns")
    op.drop_index("ix_discussions_owner_created", table_name="discussions")
    op.drop_table("discussions")
    op.drop_index("ix_news_articles_sentiment_lookup", table_name="news_articles")
    op.drop_column("news_articles", "sentiment_scored_at")
    op.drop_column("news_articles", "sentiment_label")
    op.drop_column("news_articles", "sentiment_score")
