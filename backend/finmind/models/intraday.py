"""Intraday — minute K and tick tables. Separate model file because
these dwarf every other table by row count and need their own
compression / chunk strategies.

  - tw_stock_minute   ← TaiwanStockKBar (1-minute OHLCV)
                        ~486K rows/day market-wide
                        ~5 GB raw / 10y → ~300 MB compressed

  - tw_stock_tick     ← TaiwanStockPriceTick (逐筆成交)
                        ~30M rows/day market-wide. Strategy: only
                        ingest near-1-year window of top-200 hot
                        symbols (capped at ~3 TB raw / ~150 GB
                        compressed per the Phase 1 plan). Schema is
                        general — operator picks the universe at
                        ingest time.

PK design notes:
  - `tw_stock_minute` PK = (market, symbol, ts) — `ts` is a full
    DateTime to the minute, hypertable partitioned daily.
  - `tw_stock_tick` PK = (market, symbol, ts, seq) — `seq`
    disambiguates multiple fills within the same millisecond.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from finmind.db.base import Base


class TwStockMinute(Base):
    __tablename__ = "tw_stock_minute"
    market: Mapped[str] = mapped_column(String(12), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    open: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    high: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    low: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    close: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_tw_stock_minute_symbol_ts", "symbol", "ts"),
    )


class TwStockTick(Base):
    """逐筆 — at most ~50K rows per hot symbol per day.

    `seq` is FinMind's sequence number within the (symbol, ts)
    millisecond. `tick_type` is the FinMind buy/sell flag (B/S/N)
    if the API exposes it; nullable because some upstream sources
    don't carry it."""

    __tablename__ = "tw_stock_tick"
    market: Mapped[str] = mapped_column(String(12), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    seq: Mapped[int] = mapped_column(
        Integer().with_variant(Integer(), "sqlite"),
        primary_key=True,
        default=0,
    )
    price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    volume: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bid: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    ask: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    tick_type: Mapped[str | None] = mapped_column(String(2), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_tw_stock_tick_symbol_ts", "symbol", "ts"),
    )
