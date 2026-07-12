"""Cryptocurrency tables — sourced from Binance (OHLCV / funding / open
interest) and CoinGecko (universe + market-cap info).

Unlike the TW tables these are "born self-sourced": their
`dataset_sources.active_source` starts at `'binance'` / `'coingecko'`,
never `'finmind'` (FinMind has no crypto data), so they route straight
through the self-crawl connectors with no Phase A → B cutover.

Time columns are `TIMESTAMPTZ` (not `Date`) because crypto trades 24/7
and we keep sub-day granularity (1h bars, 8h funding). `crypto_ohlcv`
carries an `interval` PK column so the daily and hourly series share one
table (segment-compressed by symbol+interval in the migration).

TimescaleDB hypertable + compression is configured in the migration
(`0022_crypto_tables`), not here — models stay plain SQLAlchemy schema,
matching the TW tables.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Numeric,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from finmind.db.base import Base


class CryptoOhlcv(Base):
    """Kline / candlestick bars per (market, symbol, interval, ts).

    `market` is the exchange (`'BINANCE'`), `interval` ∈ {'1d','1h'}.
    Prices use wide Numeric because crypto spans sub-cent alt-coins to
    five-figure BTC; volume is the base-asset amount, quote_volume the
    USDT-denominated turnover."""

    __tablename__ = "crypto_ohlcv"

    market: Mapped[str] = mapped_column(String(12), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(24), primary_key=True)
    interval: Mapped[str] = mapped_column(String(4), primary_key=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True
    )
    open: Mapped[Decimal | None] = mapped_column(Numeric(24, 8), nullable=True)
    high: Mapped[Decimal | None] = mapped_column(Numeric(24, 8), nullable=True)
    low: Mapped[Decimal | None] = mapped_column(Numeric(24, 8), nullable=True)
    close: Mapped[Decimal | None] = mapped_column(Numeric(24, 8), nullable=True)
    volume: Mapped[Decimal | None] = mapped_column(Numeric(28, 8), nullable=True)
    quote_volume: Mapped[Decimal | None] = mapped_column(
        Numeric(28, 8), nullable=True
    )
    trades: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CryptoUniverse(Base):
    """Current tracked-coin roster (top-N by market cap). PK is the
    stable `coingecko_id` (symbols collide / get reused across chains;
    the CoinGecko id doesn't). `binance_symbol` is the mapped USDT pair
    (null when the coin has no Binance listing → `status='unmapped'`,
    info-only). `status` ∈ {'active','delisted','unmapped'}; delisted
    coins keep their history but stop being scheduled."""

    __tablename__ = "crypto_universe"

    coingecko_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(24), nullable=False)
    name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    binance_symbol: Mapped[str | None] = mapped_column(String(24), nullable=True)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    market_cap_rank: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    added_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    removed_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CryptoAssetInfo(Base):
    """Weekly append-only market-cap / supply snapshot per coin, keyed
    by (snapshot_date, coingecko_id) so history is preserved for
    rank-over-time analysis. Sourced from CoinGecko /coins/markets."""

    __tablename__ = "crypto_asset_info"

    snapshot_date: Mapped[date] = mapped_column(Date, primary_key=True)
    coingecko_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    symbol: Mapped[str | None] = mapped_column(String(24), nullable=True)
    name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    market_cap_rank: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    market_cap: Mapped[Decimal | None] = mapped_column(Numeric(30, 2), nullable=True)
    circulating_supply: Mapped[Decimal | None] = mapped_column(
        Numeric(30, 4), nullable=True
    )
    total_supply: Mapped[Decimal | None] = mapped_column(Numeric(30, 4), nullable=True)
    ath: Mapped[Decimal | None] = mapped_column(Numeric(24, 8), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CryptoFundingRate(Base):
    """Perpetual-swap funding rate per (market, symbol, funding_time).
    Binance settles funding every 8h; `funding_time` is the settlement
    instant. `mark_price` is the mark at settlement."""

    __tablename__ = "crypto_funding_rate"

    market: Mapped[str] = mapped_column(String(12), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(24), primary_key=True)
    funding_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True
    )
    funding_rate: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 10), nullable=True
    )
    mark_price: Mapped[Decimal | None] = mapped_column(Numeric(24, 8), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CryptoOpenInterest(Base):
    """Perpetual-swap open interest per (market, symbol, ts). Binance
    only serves ~30 days of OI history, so this accumulates from
    go-live rather than backfilling deep."""

    __tablename__ = "crypto_open_interest"

    market: Mapped[str] = mapped_column(String(12), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(24), primary_key=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True
    )
    open_interest: Mapped[Decimal | None] = mapped_column(
        Numeric(28, 8), nullable=True
    )
    open_interest_value: Mapped[Decimal | None] = mapped_column(
        Numeric(30, 2), nullable=True
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
