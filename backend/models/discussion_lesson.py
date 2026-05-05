"""ORM model for `discussion_lessons` (migration `0040`).

Each row is a structured "what we got wrong last time" takeaway
extracted by the post-mortem synthesizer. Owner-scoped + market-
scoped + tagged with the symbols it concerns so
`gather_market_context` can fetch the most relevant ones for the
next discussion.

`related_symbols` / `missed_winners` are stored as JSON arrays
(Python `list[str]`) for SQLite parity with the test stack — the
production read path filters owner+market via the btree index
first, then does in-Python symbol matching against the (~100-row)
candidate set.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class DiscussionLesson(Base):
    __tablename__ = "discussion_lessons"
    __table_args__ = (
        Index(
            "ix_discussion_lessons_owner_market_asof",
            "owner_user_id", "market", "as_of_date",
        ),
        Index(
            "ix_discussion_lessons_hash_recent",
            "owner_user_id", "lesson_text_hash", "created_at",
        ),
        Index(
            "ix_discussion_lessons_market_tier_regime_asof",
            "market", "tier", "regime", "as_of_date",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True,
    )
    discussion_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("discussions.id", ondelete="CASCADE"),
        nullable=False,
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    market: Mapped[str] = mapped_column(String(16), nullable=False)
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    lesson_text: Mapped[str] = mapped_column(Text, nullable=False)
    lesson_text_hash: Mapped[str] = mapped_column(
        String(64), nullable=False,
    )
    related_symbols: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]",
    )
    missed_winners: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]",
    )
    # PR-B0: regime tag captured at extraction time (the market state
    # this lesson was *learned in*, not the state it should be applied
    # to). NULL when ctx didn't carry enough signal at write time —
    # the lesson is still persisted, fetch just doesn't get the
    # regime-match boost.
    regime: Mapped[str | None] = mapped_column(
        String(32), nullable=True,
    )
    # PR-B0 / B2: memory layering — episodic (default 60d decay) →
    # semantic (180d decay, B2 auto-promote) → structural (no decay,
    # admin-only manual promote). Schema lands here; B1 wires it into
    # the fetch scoring, B2 implements the promotion job.
    tier: Mapped[str] = mapped_column(
        String(16),
        nullable=False, default="episodic", server_default="episodic",
    )
    # PR-B0 / B2: usage telemetry — every fetch that includes this
    # lesson in the prompt bumps `usage_count`; every win-verdict
    # discussion that consumed it bumps `hit_count`. Promotion rule
    # in B2 reads `hit_count / usage_count`.
    usage_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    hit_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    promoted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(), nullable=False,
    )
