"""Technical (price + per-day market-microstructure) tables.

  - `ohlcv_daily` ← TaiwanStockPrice (mirrors the main app's table
    for future merge-back)
  - `tw_stock_price_adj` ← TaiwanStockPriceAdj (adjusted close, kept
    as a side-car so the canonical OHLCV stays untouched)
  - `tw_stock_per_daily` ← TaiwanStockPER (PER / PBR / dividend yield)
  - `tw_day_trade_daily` ← TaiwanStockDayTrading (當沖)
  - `tw_total_return_index` ← TaiwanStockTotalReturnIndex (報酬指數;
    symbol carries the index code `TAIEX`/`OTC`)
  - `tw_suspended` ← TaiwanStockSuspended (sparse — only rows for
    actually-suspended symbols)
  - `tw_price_limit_daily` ← TaiwanStockPriceLimit
  - `tw_market_value_weight` ← TaiwanStockMarketValueWeight
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Date, DateTime, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from finmind.db.base import Base


class OhlcvDaily(Base):
    __tablename__ = "ohlcv_daily"
    market: Mapped[str] = mapped_column(String(12), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    high: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    low: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    close: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwStockPriceAdj(Base):
    """Adjusted close per (market, symbol, ts). Kept side-car so the
    raw ohlcv_daily preserves its idempotent UPSERT contract — adjusted
    prices change retroactively after corporate actions, raw doesn't."""

    __tablename__ = "tw_stock_price_adj"
    market: Mapped[str] = mapped_column(String(12), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts: Mapped[date] = mapped_column(Date, primary_key=True)
    adj_close: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    adj_factor: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwStockPerDaily(Base):
    """PER / PBR / dividend yield per (symbol, ts). FinMind's
    TaiwanStockPER schema — these are TWSE-published, not derived."""

    __tablename__ = "tw_stock_per_daily"
    market: Mapped[str] = mapped_column(String(12), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts: Mapped[date] = mapped_column(Date, primary_key=True)
    per: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    pbr: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    dividend_yield: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwDayTradeDaily(Base):
    """當沖 — day-trade buy/sell volume per symbol per day."""

    __tablename__ = "tw_day_trade_daily"
    market: Mapped[str] = mapped_column(String(12), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts: Mapped[date] = mapped_column(Date, primary_key=True)
    buy_volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sell_volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    buy_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    sell_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwTotalReturnIndex(Base):
    """加權 / 櫃買 報酬指數. `symbol` is the index code (e.g. `TAIEX`,
    `OTC`); kept under `market='TW'` for routing consistency."""

    __tablename__ = "tw_total_return_index"
    market: Mapped[str] = mapped_column(String(12), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts: Mapped[date] = mapped_column(Date, primary_key=True)
    value: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwSuspended(Base):
    """Sparse event table — one row per symbol-suspension. PK includes
    `suspended_at` so multiple historical suspensions of the same symbol
    aren't conflated."""

    __tablename__ = "tw_suspended"
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    suspended_at: Mapped[date] = mapped_column(Date, primary_key=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    resumed_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwPriceLimitDaily(Base):
    """Per-symbol daily price-limit (漲跌停價). Useful to detect limit-
    up / limit-down at backtest replay time without re-deriving from
    prev close × 10%."""

    __tablename__ = "tw_price_limit_daily"
    market: Mapped[str] = mapped_column(String(12), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts: Mapped[date] = mapped_column(Date, primary_key=True)
    upper_limit: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    lower_limit: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwMarketValueWeight(Base):
    """Market-cap weight per symbol per day — used by index-replication
    backtests."""

    __tablename__ = "tw_market_value_weight"
    market: Mapped[str] = mapped_column(String(12), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts: Mapped[date] = mapped_column(Date, primary_key=True)
    weight: Mapped[Decimal | None] = mapped_column(Numeric(12, 8), nullable=True)
    market_cap: Mapped[Decimal | None] = mapped_column(Numeric(24, 2), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
