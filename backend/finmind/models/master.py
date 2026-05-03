"""Static / slowly-changing dimension tables.

These mirror FinMind's master datasets:

  - `tw_stock_info` ← TaiwanStockInfo / TaiwanStockInfoWithWarrant
  - `tw_trading_calendar` ← TaiwanStockTradingDate
  - `tw_broker_master` ← TaiwanSecuritiesTraderInfo
  - `tw_industry_chain` ← TaiwanStockIndustryChain
  - `tw_delisting` ← TaiwanStockDelisting

Updated monthly by the ingest cron — UPSERT on PK so additions/edits
land idempotently. Not hypertables (low row volume, queried by symbol
not by time)."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from finmind.db.base import Base


class TwStockInfo(Base):
    """One row per listed equity / warrant. PK = (market, symbol).

    `industry_category` is the same TWSE 產業別 string you'd see on
    finance.yahoo.tw — used by the discussion service for sector flow
    analysis ("外資集中買超半導體業 X 億")."""

    __tablename__ = "tw_stock_info"

    market: Mapped[str] = mapped_column(String(8), primary_key=True)  # 'TWSE' | 'OTC'
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    name_zh: Mapped[str | None] = mapped_column(String(64), nullable=True)
    industry_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    listed_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_warrant: Mapped[bool] = mapped_column(default=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwTradingCalendar(Base):
    """One row per (market, calendar date). `is_trading_day` is False on
    weekends + TWSE holidays. Holidays come from FinMind's
    TaiwanStockTradingDate which already encodes them; the self-crawl
    fallback derives them by scanning TWSE's 行事曆 PDF."""

    __tablename__ = "tw_trading_calendar"

    market: Mapped[str] = mapped_column(String(8), primary_key=True)
    ts: Mapped[date] = mapped_column(Date, primary_key=True)
    is_trading_day: Mapped[bool] = mapped_column(nullable=False)
    note: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwBrokerMaster(Base):
    """Securities-firm code → name map. Used by the 分點 (broker
    daily-report) table to label each broker_id."""

    __tablename__ = "tw_broker_master"

    broker_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    name_zh: Mapped[str | None] = mapped_column(String(64), nullable=True)
    branch_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwIndustryChain(Base):
    """Symbol → industry-chain edges. One symbol can sit on multiple
    chains (台積電 ∈ 半導體, AI 概念, Apple 供應鏈), so PK is composite."""

    __tablename__ = "tw_industry_chain"

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    industry: Mapped[str] = mapped_column(String(64), primary_key=True)
    sub_industry: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwDelisting(Base):
    """One row per delisted symbol. `delisted_at` is the last trading
    day. Symbols with rows here should be excluded from active-universe
    queries by default (`SELECT ... WHERE NOT EXISTS (... tw_delisting)`)."""

    __tablename__ = "tw_delisting"

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    delisted_at: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
