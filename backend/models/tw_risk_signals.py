"""ORM models for the TW risk-warning archives (PR #192).

Three small tables that share a cron + a discussion-context block.
Migration 0027 created them.

  * ``TwStockDisposition``  — 處置股票 (level + period window)
  * ``TwStockSuspended``    — 暫停交易 event log
  * ``TwStockDayTradingDaily`` — 現股當沖 daily volume aggregates
"""
from datetime import date, datetime

from sqlalchemy import (
    BigInteger, Date, DateTime, Index, Integer, String, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class TwStockDisposition(Base):
    __tablename__ = "tw_stock_disposition"
    __table_args__ = (
        Index("ix_tw_stock_disposition_active", "market", "period_end"),
    )

    market: Mapped[str] = mapped_column(String(12), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    period_start: Mapped[date] = mapped_column(Date, primary_key=True)

    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    classification: Mapped[str | None] = mapped_column(String(40), nullable=True)
    level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)

    source: Mapped[str] = mapped_column(String(24), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
        nullable=False,
    )


class TwStockSuspended(Base):
    __tablename__ = "tw_stock_suspended"
    __table_args__ = (
        Index("ix_tw_stock_suspended_recent", "market", "ts"),
    )

    market: Mapped[str] = mapped_column(String(12), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts: Mapped[date] = mapped_column(Date, primary_key=True)

    status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)

    source: Mapped[str] = mapped_column(String(24), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
        nullable=False,
    )


class TwStockDayTradingDaily(Base):
    __tablename__ = "tw_stock_day_trading_daily"
    __table_args__ = (
        Index("ix_tw_stock_day_trading_lookup", "market", "ts"),
    )

    market: Mapped[str] = mapped_column(String(12), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts: Mapped[date] = mapped_column(Date, primary_key=True)

    volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    buy_amount: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sell_amount: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    source: Mapped[str] = mapped_column(String(24), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
        nullable=False,
    )
