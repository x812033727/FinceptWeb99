"""Chip / 籌碼 tables.

Per-symbol daily:
  - tw_margin_daily             ← TaiwanStockMarginPurchaseShortSale
  - tw_institutional_daily      ← TaiwanStockInstitutionalInvestorsBuySell
  - tw_securities_lending       ← TaiwanStockSecuritiesLending
  - tw_holdings_aggregates      ← TaiwanStockHoldingSharesPer (TDCC weekly)
  - tw_day_trade_fee            ← TaiwanStockDayTradingBorrowingFeeRate
  - tw_foreign_shareholding     ← TaiwanStockShareholding

Market-wide daily:
  - tw_total_margin_daily       ← TaiwanStockTotalMarginPurchaseShortSale
  - tw_total_inst_daily         ← TaiwanStockTotalInstitutionalInvestors
  - tw_short_sale_balance_daily ← TaiwanDailyShortSaleBalances
  - tw_short_sale_suspension    ← TaiwanStockMarginShortSaleSuspension
  - tw_margin_maintenance       ← TaiwanTotalExchangeMarginMaintenance
  - tw_govt_bank_flow           ← TaiwanstockGovernmentBankBuySell

Sparse / event:
  - tw_block_trade              ← TaiwanStockBlockTrade
  - tw_loan_collateral          ← TaiwanStockLoanCollateralBalance
  - tw_disposition              ← TaiwanStockDispositionSecuritiesPeriod

Heavy hypertable:
  - tw_broker_daily_report      ← TaiwanStockTradingDailyReport (分點)
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Index,
    Numeric,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from finmind.db.base import Base


# ── Per-symbol daily ─────────────────────────────────────────────


class TwMarginDaily(Base):
    __tablename__ = "tw_margin_daily"
    market: Mapped[str] = mapped_column(String(12), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts: Mapped[date] = mapped_column(Date, primary_key=True)
    margin_purchase: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    margin_sale: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    margin_balance: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    short_sale: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    short_cover: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    short_balance: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwInstitutionalDaily(Base):
    """三大法人買賣超 — 外資 / 投信 / 自營商."""

    __tablename__ = "tw_institutional_daily"
    market: Mapped[str] = mapped_column(String(12), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
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


class TwSecuritiesLending(Base):
    """借券 — `TaiwanStockSecuritiesLending`."""

    __tablename__ = "tw_securities_lending"
    market: Mapped[str] = mapped_column(String(12), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts: Mapped[date] = mapped_column(Date, primary_key=True)
    transaction_type: Mapped[str | None] = mapped_column(String(32), primary_key=True)
    volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    fee_rate: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwHoldingsAggregates(Base):
    """TDCC 集保戶股權分散表 — distribution of shareholdings by holding-
    size bracket. Updated weekly."""

    __tablename__ = "tw_holdings_aggregates"
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts: Mapped[date] = mapped_column(Date, primary_key=True)
    bracket: Mapped[str] = mapped_column(String(32), primary_key=True)
    holders: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    shares: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwDayTradeFee(Base):
    __tablename__ = "tw_day_trade_fee"
    market: Mapped[str] = mapped_column(String(12), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts: Mapped[date] = mapped_column(Date, primary_key=True)
    fee_rate: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwForeignShareholding(Base):
    """外資持股比 per symbol per day."""

    __tablename__ = "tw_foreign_shareholding"
    market: Mapped[str] = mapped_column(String(12), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts: Mapped[date] = mapped_column(Date, primary_key=True)
    foreign_holding_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    foreign_holding_shares: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    available_shares: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ── Market-wide daily ────────────────────────────────────────────


class TwTotalMarginDaily(Base):
    __tablename__ = "tw_total_margin_daily"
    market: Mapped[str] = mapped_column(String(12), primary_key=True)
    ts: Mapped[date] = mapped_column(Date, primary_key=True)
    margin_balance: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    margin_purchase: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    margin_sale: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    short_balance: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    short_sale: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    short_cover: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwTotalInstDaily(Base):
    __tablename__ = "tw_total_inst_daily"
    market: Mapped[str] = mapped_column(String(12), primary_key=True)
    ts: Mapped[date] = mapped_column(Date, primary_key=True)
    foreign_net: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sitc_net: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    dealer_net: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwShortSaleBalanceDaily(Base):
    """信用額度總量管制餘額表."""

    __tablename__ = "tw_short_sale_balance_daily"
    market: Mapped[str] = mapped_column(String(12), primary_key=True)
    ts: Mapped[date] = mapped_column(Date, primary_key=True)
    short_balance: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    short_quota: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwShortSaleSuspension(Base):
    """暫停融券賣出表 — sparse, only rows for symbols with active
    suspensions."""

    __tablename__ = "tw_short_sale_suspension"
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    suspended_at: Mapped[date] = mapped_column(Date, primary_key=True)
    resumed_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwMarginMaintenance(Base):
    __tablename__ = "tw_margin_maintenance"
    market: Mapped[str] = mapped_column(String(12), primary_key=True)
    ts: Mapped[date] = mapped_column(Date, primary_key=True)
    maintenance_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwGovtBankFlow(Base):
    """八大行庫買賣 — sponsor-tier dataset on FinMind, no free self-crawl."""

    __tablename__ = "tw_govt_bank_flow"
    market: Mapped[str] = mapped_column(String(12), primary_key=True)
    ts: Mapped[date] = mapped_column(Date, primary_key=True)
    buy_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    sell_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    net_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ── Event-shaped ─────────────────────────────────────────────────


class TwBlockTrade(Base):
    """鉅額交易 — sparse event table, multiple block trades per day per
    symbol get separate rows via `seq`."""

    __tablename__ = "tw_block_trade"
    market: Mapped[str] = mapped_column(String(12), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts: Mapped[date] = mapped_column(Date, primary_key=True)
    seq: Mapped[int] = mapped_column(primary_key=True, default=0)
    price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(24, 2), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwLoanCollateral(Base):
    __tablename__ = "tw_loan_collateral"
    market: Mapped[str] = mapped_column(String(12), primary_key=True)
    ts: Mapped[date] = mapped_column(Date, primary_key=True)
    collateral_balance: Mapped[Decimal | None] = mapped_column(Numeric(24, 2), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TwDisposition(Base):
    """處置股 — TWSE 公布處置有價證券."""

    __tablename__ = "tw_disposition"
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    started_at: Mapped[date] = mapped_column(Date, primary_key=True)
    ended_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ── Heavy hypertable (分點) ──────────────────────────────────────


class TwBrokerDailyReport(Base):
    """分點 — broker-level buy/sell volume per (symbol, date, broker, price).

    The widest hypertable in the system: ~3B rows over 10 years
    market-wide. Aggressive segmentby compression on (market, symbol)
    yields ~20× ratio (see TimescaleDB compression migration). The
    PK includes `price` because the same broker can have multiple
    fills at different price points within one trading day."""

    __tablename__ = "tw_broker_daily_report"
    market: Mapped[str] = mapped_column(String(12), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts: Mapped[date] = mapped_column(Date, primary_key=True)
    broker_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    price: Mapped[Decimal] = mapped_column(Numeric(18, 6), primary_key=True)
    buy_volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sell_volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # "Top buyers/sellers of symbol X on day Y" is the headline
        # query — covering index makes it a tight index-only scan.
        Index(
            "ix_tw_broker_daily_report_lookup",
            "symbol", "ts", "broker_id",
        ),
    )
