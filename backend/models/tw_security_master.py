"""Effective-dated Taiwan security classification and trading rules."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Index, Integer, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class TwSecurityMasterVersion(Base):
    __tablename__ = "tw_security_master_versions"
    __table_args__ = (
        Index(
            "ix_tw_security_master_effective_lookup",
            "symbol", "effective_from", "effective_to",
        ),
    )

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    effective_from: Mapped[date] = mapped_column(Date, primary_key=True)
    source: Mapped[str] = mapped_column(String(40), primary_key=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    name_zh: Mapped[str | None] = mapped_column(Text, nullable=True)
    exchange: Mapped[str] = mapped_column(String(10), nullable=False)
    instrument_type: Mapped[str] = mapped_column(String(32), nullable=False)
    asset_class: Mapped[str] = mapped_column(String(24), nullable=False)
    is_etf: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_bond_etf: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_leveraged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_inverse: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    board_lot_size: Mapped[int] = mapped_column(Integer, nullable=False, default=1000)
    odd_lot_size: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    sell_tax_bps: Mapped[float] = mapped_column(Numeric(8, 4), nullable=False)
    tax_rule_code: Mapped[str] = mapped_column(String(48), nullable=False)
    classification_source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    tax_source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[str] = mapped_column(String(24), nullable=False)
    is_manual_override: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
    )
    override_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    overridden_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp(), nullable=False,
    )
