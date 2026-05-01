"""ORM model for 八大行庫 (eight-government-bank) daily flow.

One row per (market, ts, bank_name) — FinMind aggregates per bank
per trading day. Read-tier `read_recent_govt_bank_flow` sums across
banks for the discussion context block; the per-bank rows stay
queryable for future drill-down (e.g. a stock detail page tab).
"""
from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class TwGovtBankFlowDaily(Base):
    __tablename__ = "tw_govt_bank_flow_daily"
    __table_args__ = (
        Index("ix_tw_govt_bank_flow_lookup", "market", "ts"),
    )

    market: Mapped[str] = mapped_column(String(12), primary_key=True)
    ts: Mapped[date] = mapped_column(Date, primary_key=True)
    bank_name: Mapped[str] = mapped_column(String(40), primary_key=True)

    buy_amount: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sell_amount: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    source: Mapped[str] = mapped_column(String(24), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
        nullable=False,
    )
