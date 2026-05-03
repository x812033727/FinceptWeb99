"""ORM model for TAIWAN VIX daily close archive (PR #283).

One row per (market, ts). `market` is always 'TW' for now —
future-proofs cross-market when we add KOSPI VKOSPI etc.
"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Date, DateTime, Index, Numeric, String, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class TwVixDaily(Base):
    __tablename__ = "tw_vix_daily"
    __table_args__ = (
        Index("ix_tw_vix_daily_lookup", "market", "ts"),
    )

    market: Mapped[str] = mapped_column(String(12), primary_key=True)
    ts: Mapped[date] = mapped_column(Date, primary_key=True)

    vix_value: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)

    source: Mapped[str] = mapped_column(String(24), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
        nullable=False,
    )
