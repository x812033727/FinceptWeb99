"""news_articles — TW news archive

Revision ID: 0014
Revises: 0013
Create Date: 2026-04-28

Phase 4 of the scheduled-fetch subsystem. Stores news headlines pulled
from FinMind (and Google News RSS as fallback) so the read path can
serve recent news during upstream outages, and AI agents can search
historical articles by keyword or symbol.

Schema choices:
  - `dedup_hash` UNIQUE — same article from two sources collapses into
    one row. Hash of normalized title + canonical link is computed
    in services/ingest/repository.py.
  - `symbol` is NULLABLE — market-wide news (no specific symbol) gets
    NULL; per-symbol news gets a value. Index on (symbol, published_at)
    serves both lookup patterns.
  - `payload` jsonb captures source-specific extras (e.g. tags,
    thumbnail) without growing the column count.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "news_articles",
        # sa.Integer (not BigInteger) so SQLite test runs get the
        # INTEGER-PRIMARY-KEY auto-rowid alias. ~120 K rows / yr × 17 yrs
        # = ~2 M, well under the 32-bit int ceiling.
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("market", sa.String(length=12), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("link", sa.Text(), nullable=False),
        sa.Column("publisher", sa.String(length=120), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column("dedup_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "ingested_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_news_articles"),
        sa.UniqueConstraint("dedup_hash", name="uq_news_articles_dedup_hash"),
    )
    op.create_index(
        "ix_news_articles_lookup",
        "news_articles",
        ["market", "symbol", "published_at"],
        unique=False,
    )
    op.create_index(
        "ix_news_articles_published_at",
        "news_articles",
        ["published_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_news_articles_published_at", table_name="news_articles")
    op.drop_index("ix_news_articles_lookup", table_name="news_articles")
    op.drop_table("news_articles")
