"""ORM model for per-round discussion context snapshots.

One row per (discussion, round) holding the JSON blob that
`gather_market_context` produced when the round ran. Lets the
discussion detail UI show "what the personas saw at the time"
without re-running the aggregator (which would return the
*current* market, not the historical snapshot).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class DiscussionRoundContext(Base):
    __tablename__ = "discussion_round_contexts"
    __table_args__ = (
        Index(
            "ix_discussion_round_contexts_lookup",
            "discussion_id", "round",
        ),
    )

    discussion_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("discussions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    round: Mapped[int] = mapped_column(Integer, primary_key=True)
    context: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    # R6 PR2: compact ~300-word recap of THIS round's debate (unresolved
    # disagreements preserved). NULL when the round-digest feature is
    # disabled (the default) or the summariser call failed — callers must
    # treat absence as "no digest", never as an error.
    digest: Mapped[str | None] = mapped_column(Text, nullable=True)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
