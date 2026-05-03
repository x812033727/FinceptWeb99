"""Derivative tables — futures + options + 三大法人 + 大額交易人.

Per-contract daily:
  - tw_futures_daily            ← TaiwanFuturesDaily
  - tw_option_daily             ← TaiwanOptionDaily

Three institutional layers — `session` column ('day'/'night')
distinguishes the day vs after-hours rows from the same FinMind
dataset pair (TaiwanFuturesInstitutionalInvestors +
TaiwanFuturesInstitutionalInvestorsAfterHours):
  - tw_futures_inst_daily
  - tw_option_inst_daily

Large-trader OI:
  - tw_futures_oi_largetraders  ← TaiwanFuturesOpenInterestLargeTraders
  - tw_option_oi_largetraders   ← TaiwanOptionOpenInterestLargeTraders

Settlement:
  - tw_futures_settlement       ← TaiwanFuturesFinalSettlementPrice
  - tw_option_settlement        ← TaiwanOptionFinalSettlementPrice

Misc:
  - tw_futopt_daily_info        ← TaiwanFutOptDailyInfo (market overview)
  - tw_futures_dealer_volume    ← TaiwanFuturesDealerTradingVolumeDaily
  - tw_option_dealer_volume     ← TaiwanOptionDealerTradingVolumeDaily
  - tw_futures_spread           ← TaiwanFuturesSpreadTrading
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Date, DateTime, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from finmind.db.base import Base


class TwFuturesDaily(Base):
    """One row per (contract, ts). `contract` carries the FinMind
    instrument code (e.g. 'TX' for 大台, 'MTX' for 小台)."""

    __tablename__ = "tw_futures_daily"
    contract: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    high: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    low: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    close: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    open_interest: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    settlement_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwOptionDaily(Base):
    """Each (contract, strike, call_put, ts) is one row. `strike`
    in the PK because a chain has dozens of strikes per expiry."""

    __tablename__ = "tw_option_daily"
    contract: Mapped[str] = mapped_column(String(20), primary_key=True)
    strike: Mapped[Decimal] = mapped_column(Numeric(18, 6), primary_key=True)
    call_put: Mapped[str] = mapped_column(String(4), primary_key=True)  # 'C' / 'P'
    ts: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    high: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    low: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    close: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    open_interest: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwFuturesInstDaily(Base):
    """Day + night session 三大法人. `session` discriminates."""

    __tablename__ = "tw_futures_inst_daily"
    contract: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts: Mapped[date] = mapped_column(Date, primary_key=True)
    session: Mapped[str] = mapped_column(String(8), primary_key=True)  # 'day' | 'night'
    foreign_long_open_interest: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    foreign_short_open_interest: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sitc_long_open_interest: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sitc_short_open_interest: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    dealer_long_open_interest: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    dealer_short_open_interest: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwOptionInstDaily(Base):
    __tablename__ = "tw_option_inst_daily"
    contract: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts: Mapped[date] = mapped_column(Date, primary_key=True)
    session: Mapped[str] = mapped_column(String(8), primary_key=True)
    call_put: Mapped[str] = mapped_column(String(4), primary_key=True)
    foreign_long_open_interest: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    foreign_short_open_interest: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sitc_long_open_interest: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sitc_short_open_interest: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    dealer_long_open_interest: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    dealer_short_open_interest: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwFuturesOiLargeTraders(Base):
    """大額交易人未沖銷部位 — top-5/top-10 cumulative OI."""

    __tablename__ = "tw_futures_oi_largetraders"
    contract: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts: Mapped[date] = mapped_column(Date, primary_key=True)
    rank: Mapped[str] = mapped_column(String(16), primary_key=True)  # 'top5' / 'top10'
    long_oi: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    short_oi: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    long_oi_special: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    short_oi_special: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwOptionOiLargeTraders(Base):
    __tablename__ = "tw_option_oi_largetraders"
    contract: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts: Mapped[date] = mapped_column(Date, primary_key=True)
    call_put: Mapped[str] = mapped_column(String(4), primary_key=True)
    rank: Mapped[str] = mapped_column(String(16), primary_key=True)
    long_oi: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    short_oi: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwFuturesSettlement(Base):
    __tablename__ = "tw_futures_settlement"
    contract: Mapped[str] = mapped_column(String(20), primary_key=True)
    settlement_date: Mapped[date] = mapped_column(Date, primary_key=True)
    final_settlement_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwOptionSettlement(Base):
    __tablename__ = "tw_option_settlement"
    contract: Mapped[str] = mapped_column(String(20), primary_key=True)
    settlement_date: Mapped[date] = mapped_column(Date, primary_key=True)
    strike: Mapped[Decimal] = mapped_column(Numeric(18, 6), primary_key=True)
    call_put: Mapped[str] = mapped_column(String(4), primary_key=True)
    final_settlement_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwFutoptDailyInfo(Base):
    """Aggregate market overview row per ts. One row per day."""

    __tablename__ = "tw_futopt_daily_info"
    ts: Mapped[date] = mapped_column(Date, primary_key=True)
    futures_volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    options_volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    futures_open_interest: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    options_open_interest: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwFuturesDealerVolume(Base):
    __tablename__ = "tw_futures_dealer_volume"
    ts: Mapped[date] = mapped_column(Date, primary_key=True)
    dealer_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    contract: Mapped[str] = mapped_column(String(20), primary_key=True)
    volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwOptionDealerVolume(Base):
    __tablename__ = "tw_option_dealer_volume"
    ts: Mapped[date] = mapped_column(Date, primary_key=True)
    dealer_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    contract: Mapped[str] = mapped_column(String(20), primary_key=True)
    call_put: Mapped[str] = mapped_column(String(4), primary_key=True)
    volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwFuturesSpread(Base):
    __tablename__ = "tw_futures_spread"
    ts: Mapped[date] = mapped_column(Date, primary_key=True)
    spread_pair: Mapped[str] = mapped_column(String(40), primary_key=True)
    open: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    close: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
