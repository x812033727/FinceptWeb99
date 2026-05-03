"""Corporate-action + convertible-bond + news + misc tables.

Corporate actions (sparse, event-shaped):
  - tw_dividend            ← TaiwanStockDividend (公告)
  - tw_dividend_result     ← TaiwanStockDividendResult (除權息結果)
  - tw_split               ← TaiwanStockSplitPrice
  - tw_par_value_change    ← TaiwanStockParValueChange
  - tw_capital_reduction   ← TaiwanStockCapitalReductionReferencePrice
  - tw_buyback             ← TaiwanStockBuyBack (庫藏股)

Convertible bond (4 tables):
  - tw_cb_info
  - tw_cb_daily
  - tw_cb_inst_daily
  - tw_cb_daily_overview

News:
  - tw_news_articles       ← TaiwanStockNews (also fed by Google RSS
                               sibling job; `source` column distinguishes)
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from finmind.db.base import Base


# ── Corporate actions ───────────────────────────────────────────


class TwDividend(Base):
    """Dividend announcement — paid in cash, shares, or both."""

    __tablename__ = "tw_dividend"
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    announce_date: Mapped[date] = mapped_column(Date, primary_key=True)
    cash_dividend: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    stock_dividend: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    ex_dividend_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    ex_rights_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwDividendResult(Base):
    """除權息實際結果 — actual ex-rights / ex-dividend price + adjusted
    reference price. PK is (symbol, ex_date) — there's at most one event
    per symbol per day."""

    __tablename__ = "tw_dividend_result"
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    ex_date: Mapped[date] = mapped_column(Date, primary_key=True)
    before_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    after_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    cash_dividend: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    stock_dividend: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwSplit(Base):
    """Stock split — pre/post reference price after a 分割."""

    __tablename__ = "tw_split"
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    ex_date: Mapped[date] = mapped_column(Date, primary_key=True)
    before_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    after_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    split_ratio: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwParValueChange(Base):
    __tablename__ = "tw_par_value_change"
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    ex_date: Mapped[date] = mapped_column(Date, primary_key=True)
    old_par: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    new_par: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    reference_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwCapitalReduction(Base):
    __tablename__ = "tw_capital_reduction"
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    ex_date: Mapped[date] = mapped_column(Date, primary_key=True)
    before_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    after_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    reduction_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwBuyback(Base):
    """庫藏股公告. announced_at + symbol unique by FinMind contract."""

    __tablename__ = "tw_buyback"
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    announced_at: Mapped[date] = mapped_column(Date, primary_key=True)
    started_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    ended_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    plan_shares: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    actual_shares: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    avg_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ── Convertible bond ────────────────────────────────────────────


class TwCbInfo(Base):
    """可轉債靜態資料 — issuer, coupon, conversion price, maturity."""

    __tablename__ = "tw_cb_info"
    cb_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    underlying_symbol: Mapped[str | None] = mapped_column(String(20), nullable=True)
    name_zh: Mapped[str | None] = mapped_column(String(64), nullable=True)
    issue_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    maturity_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    conversion_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    par_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwCbDaily(Base):
    __tablename__ = "tw_cb_daily"
    cb_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    high: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    low: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    close: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwCbInstDaily(Base):
    __tablename__ = "tw_cb_inst_daily"
    cb_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts: Mapped[date] = mapped_column(Date, primary_key=True)
    foreign_buy: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    foreign_sell: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sitc_buy: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sitc_sell: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    dealer_buy: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    dealer_sell: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwCbDailyOverview(Base):
    __tablename__ = "tw_cb_daily_overview"
    cb_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts: Mapped[date] = mapped_column(Date, primary_key=True)
    outstanding_amount: Mapped[Decimal | None] = mapped_column(Numeric(24, 2), nullable=True)
    converted_shares: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ── News ────────────────────────────────────────────────────────


class TwNewsArticle(Base):
    """News articles — both FinMind's `TaiwanStockNews` and the local
    Google RSS sibling cron land here. `source` distinguishes; sha256
    of (title || link) prevents duplicates regardless of source."""

    __tablename__ = "tw_news_articles"
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    market: Mapped[str] = mapped_column(String(12), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(20), nullable=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    link: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sentiment_score: Mapped[Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)
    sentiment_label: Mapped[str | None] = mapped_column(String(16), nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_tw_news_articles_sha256", "sha256", unique=True),
        Index(
            "ix_tw_news_articles_market_published",
            "market", "published_at",
        ),
        Index("ix_tw_news_articles_symbol_published", "symbol", "published_at"),
    )
