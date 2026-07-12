from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
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


class NewsArticle(Base):
    """News article archive populated by scheduled ingest from FinMind
    (and other sources). `symbol` is NULL for market-wide articles.

    Dedup is enforced by a UNIQUE constraint on `dedup_hash`
    (sha256 of normalized title + canonical link) — re-running the
    ingest task is idempotent.

    Sentiment fields are populated asynchronously by the hourly scoring
    job (services/news_sentiment_service.py). Articles ingested before
    the job ran read NULL until the next pass picks them up.
    """
    __tablename__ = "news_articles"
    __table_args__ = (
        UniqueConstraint("dedup_hash", name="uq_news_articles_dedup_hash"),
        Index("ix_news_articles_lookup", "market", "symbol", "published_at"),
        Index("ix_news_articles_published_at", "published_at"),
        Index("ix_news_articles_sentiment_lookup", "market", "sentiment_scored_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market: Mapped[str] = mapped_column(String(12), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(20), nullable=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    link: Mapped[str] = mapped_column(Text, nullable=False)
    publisher: Mapped[str | None] = mapped_column(String(120), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    dedup_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp(), nullable=False,
    )
    sentiment_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    sentiment_label: Mapped[str | None] = mapped_column(String(16), nullable=True)
    sentiment_scored_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    # Full article text extracted from the first-party link (G5 phase 2,
    # direct-publisher feeds only — Google-News redirect links aren't
    # fetchable). NULL = not yet attempted; `body_fetched_at` set (with
    # body possibly still NULL) = attempted, so the enrichment task won't
    # retry a page that yielded nothing.
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
