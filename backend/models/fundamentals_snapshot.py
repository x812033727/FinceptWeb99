from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, Date, DateTime, Index, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class FundamentalsSnapshot(Base):
    """Daily PE/PB/dividend_yield (and optional EPS/revenue) snapshot.

    Composite PK (market, symbol, as_of) keeps the ingest idempotent:
    re-running on the same day overwrites the row. EPS / revenue are
    nullable because TWSE BWIBBU only exposes ratios — a future FinMind
    enrichment task can backfill those per shard.
    """
    __tablename__ = "fundamentals_snapshots"
    __table_args__ = (
        Index("ix_fundamentals_snapshots_lookup", "market", "symbol", "as_of"),
    )

    market: Mapped[str] = mapped_column(String(12), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    as_of: Mapped[date] = mapped_column(Date, primary_key=True)

    pe_ratio: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    pb_ratio: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    dividend_yield: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    eps: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    revenue: Mapped[float | None] = mapped_column(Numeric(20, 2), nullable=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    source: Mapped[str] = mapped_column(String(24), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp(), nullable=False,
    )
