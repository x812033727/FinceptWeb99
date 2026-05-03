"""Fundamental tables — financial statements, revenue, market cap.

Strategy: each statement type has ~120 line-items in FinMind's
schema (revenue, COGS, operating income, EPS, etc.) and the exact
column set drifts as accounting standards evolve. Storing the full
payload as JSONB plus a small set of canonical columns
(`revenue`, `net_income`, `eps`, `period_end`) gives:

  - typed indexed access for the headline metrics that 80% of
    queries need
  - lossless storage for the long tail without periodic
    schema migrations when FinMind adds a column

Tables:
  - tw_income_statement   ← TaiwanStockFinancialStatements (綜合損益表)
  - tw_balance_sheet      ← TaiwanStockBalanceSheet (資產負債表)
  - tw_cash_flow          ← TaiwanStockCashFlowsStatement (現金流量表)
  - tw_revenue_monthly    ← TaiwanStockMonthRevenue (月營收)
  - tw_business_indicator ← TaiwanBusinessIndicator (景氣對策信號)
  - tw_market_value       ← TaiwanStockMarketValue (個股市值)
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Numeric,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from finmind.db.base import Base

# JSONB on Postgres, JSON on SQLite (tests). Keeps the model portable.
_JSON = JSONB().with_variant(JSON(), "sqlite")


class TwIncomeStatement(Base):
    """Quarterly comprehensive income statement.

    `period` is the YYYYQN period code (e.g. '2026Q1'); `period_end`
    is the actual statement cutoff date — both kept so range queries
    on calendar dates AND clean grouping by fiscal quarter both work."""

    __tablename__ = "tw_income_statement"
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    period: Mapped[str] = mapped_column(String(8), primary_key=True)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)

    revenue: Mapped[Decimal | None] = mapped_column(Numeric(24, 2), nullable=True)
    operating_income: Mapped[Decimal | None] = mapped_column(Numeric(24, 2), nullable=True)
    net_income: Mapped[Decimal | None] = mapped_column(Numeric(24, 2), nullable=True)
    eps: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)

    raw: Mapped[dict[str, Any] | None] = mapped_column(_JSON, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwBalanceSheet(Base):
    """Quarterly balance sheet. Same JSONB-with-canonical-columns
    pattern as the income statement."""

    __tablename__ = "tw_balance_sheet"
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    period: Mapped[str] = mapped_column(String(8), primary_key=True)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)

    total_assets: Mapped[Decimal | None] = mapped_column(Numeric(24, 2), nullable=True)
    total_liabilities: Mapped[Decimal | None] = mapped_column(Numeric(24, 2), nullable=True)
    total_equity: Mapped[Decimal | None] = mapped_column(Numeric(24, 2), nullable=True)
    cash: Mapped[Decimal | None] = mapped_column(Numeric(24, 2), nullable=True)

    raw: Mapped[dict[str, Any] | None] = mapped_column(_JSON, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwCashFlow(Base):
    __tablename__ = "tw_cash_flow"
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    period: Mapped[str] = mapped_column(String(8), primary_key=True)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)

    operating_cash_flow: Mapped[Decimal | None] = mapped_column(Numeric(24, 2), nullable=True)
    investing_cash_flow: Mapped[Decimal | None] = mapped_column(Numeric(24, 2), nullable=True)
    financing_cash_flow: Mapped[Decimal | None] = mapped_column(Numeric(24, 2), nullable=True)
    free_cash_flow: Mapped[Decimal | None] = mapped_column(Numeric(24, 2), nullable=True)

    raw: Mapped[dict[str, Any] | None] = mapped_column(_JSON, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwRevenueMonthly(Base):
    """Monthly revenue per symbol — the 公開觀測站 月營收 dataset.
    `revenue_yoy` and `revenue_mom` are pre-computed at ingest time
    so backtest / discussion queries don't repeat the lag-join."""

    __tablename__ = "tw_revenue_monthly"
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts: Mapped[date] = mapped_column(Date, primary_key=True)  # YYYY-MM-01
    revenue: Mapped[Decimal | None] = mapped_column(Numeric(24, 2), nullable=True)
    revenue_yoy: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    revenue_mom: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwBusinessIndicator(Base):
    """國發會景氣對策信號. One row per month."""

    __tablename__ = "tw_business_indicator"
    ts: Mapped[date] = mapped_column(Date, primary_key=True)  # YYYY-MM-01
    score: Mapped[int | None] = mapped_column(nullable=True)
    signal: Mapped[str | None] = mapped_column(String(8), nullable=True)  # 紅 / 黃紅 / 綠 / 黃藍 / 藍
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwMarketValue(Base):
    """Per-symbol daily market cap. FinMind's TaiwanStockMarketValue."""

    __tablename__ = "tw_market_value"
    market: Mapped[str] = mapped_column(String(12), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts: Mapped[date] = mapped_column(Date, primary_key=True)
    market_cap: Mapped[Decimal | None] = mapped_column(Numeric(24, 2), nullable=True)
    issued_shares: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
