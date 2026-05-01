"""ORM models for the holdings-aggregate archives (PR #193).

  * `TwStockShareholding` — 股權分散 long-form row per
    (market, symbol, ts, bucket_id). Fifteen buckets per FinMind's
    canonical level breakdown.
  * `TwMarketInstitutionalDaily` — full-market 三大法人 daily
    aggregate (one row per trading day).
"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger, Date, DateTime, Index, Integer, Numeric, String, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class TwStockShareholding(Base):
    __tablename__ = "tw_stock_shareholding"
    __table_args__ = (
        Index("ix_tw_stock_shareholding_lookup", "market", "symbol", "ts"),
    )

    market: Mapped[str] = mapped_column(String(12), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts: Mapped[date] = mapped_column(Date, primary_key=True)
    bucket_id: Mapped[int] = mapped_column(Integer, primary_key=True)

    bucket_label: Mapped[str | None] = mapped_column(String(40), nullable=True)
    holders_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    shares_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    shares_percent: Mapped[Decimal | None] = mapped_column(
        Numeric(8, 4), nullable=True,
    )

    source: Mapped[str] = mapped_column(String(24), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
        nullable=False,
    )


class TwMarketInstitutionalDaily(Base):
    __tablename__ = "tw_market_institutional_daily"
    __table_args__ = (
        Index("ix_tw_market_institutional_daily_lookup", "market", "ts"),
    )

    market: Mapped[str] = mapped_column(String(12), primary_key=True)
    ts: Mapped[date] = mapped_column(Date, primary_key=True)

    foreign_buy: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    foreign_sell: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sitc_buy: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sitc_sell: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    dealer_buy: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    dealer_sell: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    source: Mapped[str] = mapped_column(String(24), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
        nullable=False,
    )
