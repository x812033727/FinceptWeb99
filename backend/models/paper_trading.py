"""Paper-trading orders and immutable fill records."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class PaperOrder(Base):
    __tablename__ = "paper_orders"
    __table_args__ = (
        UniqueConstraint(
            "portfolio_id",
            "idempotency_key",
            name="uq_paper_orders_portfolio_idempotency",
        ),
        Index(
            "ix_paper_orders_portfolio_status_created",
            "portfolio_id",
            "status",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("portfolios.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    market: Mapped[str] = mapped_column(String(10), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    order_type: Mapped[str] = mapped_column(String(10), nullable=False)
    time_in_force: Mapped[str] = mapped_column(String(3), nullable=False, default="day")
    quantity: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    filled_quantity: Mapped[float] = mapped_column(
        Numeric(18, 6),
        nullable=False,
        default=0,
    )
    limit_price: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    reservation_price: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    average_fill_price: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    fee_bps: Mapped[float] = mapped_column(Numeric(10, 4), nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PaperFill(Base):
    __tablename__ = "paper_fills"
    __table_args__ = (
        UniqueConstraint(
            "order_id",
            "idempotency_key",
            name="uq_paper_fills_order_idempotency",
        ),
        Index("ix_paper_fills_order_filled", "order_id", "filled_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("paper_orders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("transactions.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    quantity: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    price: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    fee: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False, default=0)
    quote_price: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    slippage_bps: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    liquidity_quantity: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    quote_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    execution_source: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
