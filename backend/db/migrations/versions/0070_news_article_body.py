"""news_articles: add body + body_fetched_at for G5 full-text extraction

Revision ID: 0070
Revises: 0069
Create Date: 2026-07-12

Adds two nullable columns to `news_articles`:

  - `body` (Text): full article text extracted from the first-party
    link (direct-publisher feeds only — Google-News redirect links
    aren't fetchable). NULL until the enrichment task fills it.
  - `body_fetched_at` (timestamptz): when extraction was last attempted.
    Set even when `body` stays NULL (paywall / block / empty extract) so
    the enrichment task doesn't retry a dead page forever.

Both nullable, no backfill — existing rows simply have no body until the
enrichment task revisits recent ones.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0070"
down_revision: str | None = "0069"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "news_articles",
        sa.Column("body", sa.Text(), nullable=True),
    )
    op.add_column(
        "news_articles",
        sa.Column(
            "body_fetched_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("news_articles", "body_fetched_at")
    op.drop_column("news_articles", "body")
