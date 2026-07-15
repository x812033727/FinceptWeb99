import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base


class TransactionType(str, enum.Enum):
    buy = "buy"
    sell = "sell"
    dividend = "dividend"


class Market(str, enum.Enum):
    US = "US"
    TW = "TW"
    CRYPTO = "CRYPTO"


class Portfolio(Base):
    __tablename__ = "portfolios"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # No SQLAlchemy default — caller must pick a base currency at
    # creation time. The portfolio service / Pydantic schema enforce
    # the same at the API layer so a USD default can't sneak back in.
    # Existing rows are unaffected (this only changes ORM-default
    # behaviour for new inserts; DB column stays NOT NULL).
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    holdings: Mapped[list["Holding"]] = relationship("Holding", back_populates="portfolio", cascade="all, delete-orphan")
    transactions: Mapped[list["Transaction"]] = relationship("Transaction", back_populates="portfolio", cascade="all, delete-orphan")
    cash_entries: Mapped[list["PortfolioCashEntry"]] = relationship(
        "PortfolioCashEntry", back_populates="portfolio", cascade="all, delete-orphan",
    )


class Holding(Base):
    __tablename__ = "holdings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("portfolios.id"), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    market: Mapped[Market] = mapped_column(Enum(Market), nullable=False)
    quantity: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    avg_cost: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    cost_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    portfolio: Mapped["Portfolio"] = relationship("Portfolio", back_populates="holdings")


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("portfolios.id"), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    market: Mapped[Market] = mapped_column(Enum(Market), nullable=False)
    tx_type: Mapped[TransactionType] = mapped_column(Enum(TransactionType), nullable=False)
    quantity: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    price: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    fx_rate: Mapped[float] = mapped_column(Numeric(18, 6), default=1.0, nullable=False)
    tx_date: Mapped[date] = mapped_column(Date, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    portfolio: Mapped["Portfolio"] = relationship("Portfolio", back_populates="transactions")


class PortfolioTransactionImport(Base):
    """One successfully committed CSV batch, used for retry-safe imports."""

    __tablename__ = "portfolio_transaction_imports"
    __table_args__ = (
        UniqueConstraint(
            "portfolio_id", "content_hash",
            name="uq_portfolio_transaction_import_content",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("portfolios.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    row_count: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False,
    )


class PortfolioCashEntry(Base):
    """Append-only multi-currency cash ledger entry.

    Corrections are represented by a counter-entry via ``reversal_of``;
    existing entries are never edited or deleted through the service layer.
    """

    __tablename__ = "portfolio_cash_entries"
    __table_args__ = (
        UniqueConstraint(
            "portfolio_id", "idempotency_key",
            name="uq_portfolio_cash_entry_idempotency",
        ),
        Index(
            "ix_portfolio_cash_entries_lookup",
            "portfolio_id", "currency", "occurred_on", "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("portfolios.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(20, 6), nullable=False)
    entry_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    occurred_on: Mapped[date] = mapped_column(Date, nullable=False)
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("transactions.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    reversal_of: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("portfolio_cash_entries.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    entry_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False,
    )

    portfolio: Mapped["Portfolio"] = relationship(
        "Portfolio", back_populates="cash_entries",
    )


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "snapshot_date",
                         name="uq_portfolio_snapshots_portfolio_id_snapshot_date"),
    )

    # snapshot_date is part of the PK so the table satisfies TimescaleDB's
    # "partitioning column must be in every UNIQUE index" rule when converted
    # to a hypertable in migration 0004.
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    snapshot_date: Mapped[date] = mapped_column(Date, primary_key=True, nullable=False)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("portfolios.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    total_value_usd: Mapped[float] = mapped_column(Float, nullable=False)
    base_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    holdings_value_base: Mapped[float | None] = mapped_column(Float, nullable=True)
    cash_value_base: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_value_base: Mapped[float | None] = mapped_column(Float, nullable=True)
    positions: Mapped[list | None] = mapped_column(JSON, nullable=True)
    cash_balances: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    valuation_quality: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    portfolio: Mapped["Portfolio"] = relationship("Portfolio")
