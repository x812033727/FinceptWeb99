"""ORM model for `corporate_announcements` (migration 0056, PR-D1).

TW MOPS 重大訊息 archive. The `score_news_sentiment` cron treats
both this table and `news_articles` as fungible inputs, so the
sentiment fields mirror `NewsArticle` exactly — only the body shape
and the required `category` differ. See migration 0056 docstring
for the rationale on keeping a separate table instead of a `kind`
column on `news_articles`.
"""
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class CorporateAnnouncement(Base):
    __tablename__ = "corporate_announcements"
    __table_args__ = (
        UniqueConstraint(
            "dedup_hash",
            name="uq_corporate_announcements_dedup_hash",
        ),
        Index(
            "ix_corporate_announcements_lookup",
            "market", "symbol", "announced_at",
        ),
        Index(
            "ix_corporate_announcements_announced_at",
            "announced_at",
        ),
        Index(
            "ix_corporate_announcements_sentiment_lookup",
            "market", "sentiment_scored_at",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True,
    )
    market: Mapped[str] = mapped_column(String(12), nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    announced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    dedup_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(), nullable=False,
    )
    sentiment_score: Mapped[float | None] = mapped_column(
        Float, nullable=True,
    )
    sentiment_label: Mapped[str | None] = mapped_column(
        String(16), nullable=True,
    )
    sentiment_scored_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
