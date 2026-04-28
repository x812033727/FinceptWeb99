from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, Index, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class OhlcvDaily(Base):
    """Daily K-line archive populated by scheduled ingest tasks.

    Composite PK (market, symbol, ts) makes the daily upsert idempotent
    and matches TimescaleDB's "partitioning column must appear in every
    UNIQUE index" rule. Read path: services.ingest.repository.read_ohlcv_range.
    """
    __tablename__ = "ohlcv_daily"
    __table_args__ = (
        Index("ix_ohlcv_daily_lookup", "market", "symbol", "ts"),
    )

    market: Mapped[str] = mapped_column(String(12), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts: Mapped[date] = mapped_column(Date, primary_key=True)

    open: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    high: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    low: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    close: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    source: Mapped[str] = mapped_column(String(24), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp(), nullable=False,
    )
