from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class QuoteSnapshot(Base):
    """Periodic last-price snapshot, written by the TW quote refresh task.

    Composite PK (market, symbol, ts) makes the inserts idempotent if the
    same wall-clock instant ever produces two writes; in practice the
    refresh task runs every 60 s so collisions are unobservable. Uses
    same hypertable-friendly key shape as ohlcv_daily.
    """
    __tablename__ = "quote_snapshots"
    __table_args__ = (
        Index("ix_quote_snapshots_lookup", "market", "symbol", "ts"),
    )

    market: Mapped[str] = mapped_column(String(12), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)

    last_price: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    change_pct: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    prev_close: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    source: Mapped[str] = mapped_column(String(24), nullable=False)
