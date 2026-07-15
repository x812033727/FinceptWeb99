"""Point-in-time TW company name/exchange/industry snapshots."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class TwCompanyClassificationSnapshot(Base):
    __tablename__ = "tw_company_classification_snapshots"
    __table_args__ = (
        Index(
            "ix_tw_company_classification_lookup",
            "symbol", "snapshot_date",
        ),
    )

    snapshot_date: Mapped[date] = mapped_column(Date, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    exchange: Mapped[str] = mapped_column(String(10), nullable=False)
    industry: Mapped[str | None] = mapped_column(Text, nullable=True)
    name_zh: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp(), nullable=False,
    )
