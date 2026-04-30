"""ORM models for TW chip-metric daily archives.

Two parallel tables for the two staple chip indicators TW personas
read every morning:

  - 法人買賣超 (institutional investor net buys/sells) →
    `tw_institutional_daily`
  - 融資融券 (margin / short balance) →
    `tw_margin_daily`

Schema mirrors `OhlcvDaily`: composite PK (market, symbol, ts)
so daily ingest is an idempotent UPSERT, and `market` is included
from day one for future cross-market extensions.
"""
from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class TwInstitutionalDaily(Base):
    """法人買賣超 — fini / sitc / dealer buy + sell volumes per day."""
    __tablename__ = "tw_institutional_daily"
    __table_args__ = (
        Index(
            "ix_tw_institutional_daily_lookup",
            "market", "symbol", "ts",
        ),
    )

    market: Mapped[str] = mapped_column(String(12), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts: Mapped[date] = mapped_column(Date, primary_key=True)

    fini_buy: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    fini_sell: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sitc_buy: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sitc_sell: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    dealer_buy: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    dealer_sell: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    source: Mapped[str] = mapped_column(String(24), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
        nullable=False,
    )


class TwMarginDaily(Base):
    """融資融券 — daily margin purchase / balance + short sale / balance."""
    __tablename__ = "tw_margin_daily"
    __table_args__ = (
        Index(
            "ix_tw_margin_daily_lookup",
            "market", "symbol", "ts",
        ),
    )

    market: Mapped[str] = mapped_column(String(12), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts: Mapped[date] = mapped_column(Date, primary_key=True)

    margin_purchase: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    margin_balance: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    short_sale: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    short_balance: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    source: Mapped[str] = mapped_column(String(24), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
        nullable=False,
    )
