"""C3 回測結果持久化 (`backtest_runs`).

One row per saved backtest run from ``POST /api/analytics/backtest``
with ``save=true``. The row stores everything needed to re-render the
result later and to compare runs side-by-side:

* ``params``       — the strategy parameters that were used.
* ``config``       — run configuration: symbols / markets / dates /
  initial_capital plus every C2 risk-control knob, and the
  ``trades_truncated`` bookkeeping flag (see below).
* ``metrics``      — the engine's metrics dict verbatim.
* ``equity_curve`` — full ``[{date, value}]`` series.
* ``trades``       — at most the last :data:`MAX_PERSISTED_TRADES`
  trade dicts (NULL when the run produced none). When the engine
  reported more trades than were persisted, ``config`` carries
  ``trades_truncated: true`` — ``metrics.total_trades`` remains the
  authoritative full count.
"""
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base

if TYPE_CHECKING:
    from models.user import User

# Cap on persisted trade dicts per run — keeps a busy multi-year run's
# row bounded. The last N trades are kept (most recent activity).
MAX_PERSISTED_TRADES = 500


class BacktestRun(Base):
    __tablename__ = "backtest_runs"
    __table_args__ = (
        # User history list (`GET /api/analytics/backtest-runs`) — newest first.
        Index("ix_backtest_runs_user_id_created_at", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    strategy: Mapped[str] = mapped_column(String(64), nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    equity_curve: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list,
    )
    trades: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(UTC),
    )

    user: Mapped["User"] = relationship("User")  # type: ignore[name-defined]
