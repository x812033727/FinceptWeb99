"""corporate_announcements — TW MOPS material-info disclosures (PR-D1)

Revision ID: 0056
Revises: 0055
Create Date: 2026-05-09

The discussion ctx so far reads `news_articles` (Google News RSS — TW
zh-TW headlines + international macro). MOPS 重大訊息 / 公開資訊
觀測站 disclosures are different in shape and meaningfully more
actionable:

  - **Structured** — every row carries an official `category`
    (財報、減資、處分資產、股利政策、自願性公告、…) the LLM can
    reason about ("3 categories=財報 in 5 days from same issuer
    means earnings season") instead of having to NLP-extract from
    a freeform headline.
  - **Official** — issued by the company itself via MOPS, with
    timestamp accuracy at the minute level. News RSS items
    typically lag MOPS by 30-90 min while the wire-service desk
    rewrites the headline.
  - **Per-symbol** — every announcement carries a stock_id, so
    per-symbol fetch is exact instead of regex-scraped from the
    title (which `news_articles.symbol` already does best-effort
    but with false positives like "2026" parsed as a TW symbol).

Schema mirrors `news_articles` where it makes sense (sentiment
fields, dedup_hash, ingested_at) so the same hourly LLM scorer can
process both archives in one pass — see PR-D1 patch on
`score_news_sentiment`. Departures:

  - `category` is required (announcement taxonomy is the whole
    point of preferring this archive over freeform news).
  - `body` is a separate column from `title` because MOPS
    structures the disclosure body as a multi-paragraph message
    the LLM benefits from quoting verbatim into the discussion ctx.
  - No `publisher` — every row is from MOPS itself.

`(market, symbol, announced_at)` index supports the per-symbol +
market-wide sentiment aggregator queries the existing news pipeline
already runs.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0056"
down_revision: str | None = "0055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "corporate_announcements",
        sa.Column(
            "id", sa.Integer(), primary_key=True, autoincrement=True,
        ),
        sa.Column("market", sa.String(length=12), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column(
            "announced_at", sa.DateTime(timezone=True), nullable=False,
        ),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column("dedup_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "ingested_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
        sa.Column("sentiment_score", sa.Float(), nullable=True),
        sa.Column(
            "sentiment_label", sa.String(length=16), nullable=True,
        ),
        sa.Column(
            "sentiment_scored_at",
            sa.DateTime(timezone=True), nullable=True,
        ),
        sa.UniqueConstraint(
            "dedup_hash", name="uq_corporate_announcements_dedup_hash",
        ),
    )
    op.create_index(
        "ix_corporate_announcements_lookup",
        "corporate_announcements",
        ["market", "symbol", "announced_at"],
        unique=False,
    )
    op.create_index(
        "ix_corporate_announcements_announced_at",
        "corporate_announcements",
        ["announced_at"],
        unique=False,
    )
    op.create_index(
        "ix_corporate_announcements_sentiment_lookup",
        "corporate_announcements",
        ["market", "sentiment_scored_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_corporate_announcements_sentiment_lookup",
        table_name="corporate_announcements",
    )
    op.drop_index(
        "ix_corporate_announcements_announced_at",
        table_name="corporate_announcements",
    )
    op.drop_index(
        "ix_corporate_announcements_lookup",
        table_name="corporate_announcements",
    )
    op.drop_table("corporate_announcements")
