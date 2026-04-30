"""ORM model for TW monthly revenue archive.

One row per (symbol, month) with absolute revenue + YoY / MoM
growth percentages as reported by TWSE / FinMind. PK matches the
upsert pattern used by the rest of the chip-metric / OHLCV
archives.
"""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Date, DateTime, Index, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class TwRevenueMonthly(Base):
    __tablename__ = "tw_revenue_monthly"
    __table_args__ = (
        Index(
            "ix_tw_revenue_monthly_lookup",
            "market", "symbol", "ts",
        ),
    )

    market: Mapped[str] = mapped_column(String(12), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts: Mapped[date] = mapped_column(Date, primary_key=True)

    revenue: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    revenue_yoy: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    revenue_mom: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)

    source: Mapped[str] = mapped_column(String(24), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
        nullable=False,
    )
