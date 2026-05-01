"""ORM model for TW buyback announcement archive.

One row per `(market, symbol, announce_date)` — a single listed
company can have multiple concurrent buyback rounds across
different announcement dates. `current_shares` updates day-by-day
as execution progresses, so the cron's UPSERT pattern intentionally
overwrites the same row to reflect the latest execution figure.
"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger, Date, DateTime, Index, Integer, Numeric, String, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class TwStockBuyback(Base):
    __tablename__ = "tw_stock_buyback"
    __table_args__ = (
        Index(
            "ix_tw_stock_buyback_active",
            "market", "period_end", "announce_date",
        ),
    )

    market: Mapped[str] = mapped_column(String(12), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    announce_date: Mapped[date] = mapped_column(Date, primary_key=True)

    period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    method: Mapped[int | None] = mapped_column(Integer, nullable=True)
    purpose: Mapped[str | None] = mapped_column(String(80), nullable=True)

    max_shares: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    current_shares: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    price_lower: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    price_upper: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)

    source: Mapped[str] = mapped_column(String(24), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
        nullable=False,
    )
