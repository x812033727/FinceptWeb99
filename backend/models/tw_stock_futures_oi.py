"""ORM model for daily 個股期貨 三大法人未平倉 snapshot (PR #282).

One row per (market, symbol, ts) — the three investor types
(foreign / SITC / dealer) live as separate columns to match the
`tw_institutional_daily` (cash equity) shape, so read-tier
helpers can join the two on (symbol, ts) without re-aggregating.

Populated by `tasks/ingest_stock_futures_oi_tw.py` (daily cron,
post-close).
"""
from datetime import date, datetime

from sqlalchemy import (
    BigInteger, Date, DateTime, Index, String, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class TwStockFuturesOi(Base):
    __tablename__ = "tw_stock_futures_oi"
    __table_args__ = (
        Index("ix_tw_stock_futures_oi_lookup", "market", "ts"),
        Index("ix_tw_stock_futures_oi_symbol", "market", "symbol", "ts"),
    )

    market: Mapped[str] = mapped_column(String(12), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    ts: Mapped[date] = mapped_column(Date, primary_key=True)

    contract_id: Mapped[str] = mapped_column(String(12), nullable=False)

    fini_long_oi: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    fini_short_oi: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    fini_net_oi: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    sitc_long_oi: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sitc_short_oi: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sitc_net_oi: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    dealer_long_oi: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    dealer_short_oi: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    dealer_net_oi: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    source: Mapped[str] = mapped_column(String(24), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
        nullable=False,
    )
