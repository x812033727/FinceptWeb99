"""ORM models for the expert-discussion subsystem.

A `Discussion` is a single round-table session created by a user. It
carries the user-supplied topic + rules, the persona roster, the current
status, and (once the synthesizer has run) a structured conclusion.

A `DiscussionTurn` is one persona utterance within a round. `stance` is a
plain string (`agree` | `dissent` | `supplement`) instead of a SQL ENUM
so adding a new stance later is a code change, not a migration. `agree`
turns typically carry empty content — the persona "passed".

The `discussion_id` FK uses `ondelete="CASCADE"` so a single delete on
the parent row removes the entire conversation history.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class Discussion(Base):
    __tablename__ = "discussions"
    __table_args__ = (
        Index("ix_discussions_owner_created", "owner_id", "created_at"),
        Index(
            "ix_discussions_verify_pending",
            "verify_after_date",
        ),
        # Defence in depth (PR #220): the service's `_normalize_market`
        # already validates against `_VALID_MARKETS`, but raw SQL or
        # bypassing call paths could still write `'JP'` etc. The
        # CHECK locks the contract at the DB so downstream code can
        # rely on the value being one of the three known markets.
        CheckConstraint(
            "market IN ('TW', 'US', 'GLOBAL')",
            name="ck_discussions_market_allowed",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    topic: Mapped[str] = mapped_column(Text, nullable=False)
    rules: Mapped[str] = mapped_column(Text, nullable=False)
    persona_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    # Which market the discussion is anchored to. Drives which blocks
    # `gather_market_context` populates (TW chip metrics only fire for
    # TW, etc.) and which symbol-extraction rules apply to the topic.
    market: Mapped[str] = mapped_column(
        String(8), nullable=False, default="TW", server_default="TW",
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="draft", server_default="draft",
    )
    current_round: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    conclusion: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # Self-grading columns (added in migration 0018). `auto_run` flags
    # rows produced by the daily scheduler so the verifier task doesn't
    # touch user-created manual discussions. `verdict` stays NULL until
    # the verifier writes one — its possible terminal values are
    # 'win' / 'loss' / 'unverifiable'.
    verdict: Mapped[str | None] = mapped_column(
        String(16), nullable=True,
    )
    verdict_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    auto_run: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false(),
    )
    day1_open_prices: Mapped[dict[str, float] | None] = mapped_column(
        JSON, nullable=True,
    )
    day5_close_prices: Mapped[dict[str, float] | None] = mapped_column(
        JSON, nullable=True,
    )
    # Per-symbol D1-D5 closes (PR #140) for the "對答案" scoreboard.
    # Shape: {symbol: [d1, d2, d3, d4, d5]} where each slot is the
    # day's close in TWD or NULL when the day hasn't resolved yet.
    # Populated by `tasks.score_discussion_outcomes` once the 5-day
    # window expires; left NULL for active discussions so the cron
    # knows to retry on the next tick.
    daily_close_prices: Mapped[dict[str, list[float | None]] | None] = mapped_column(
        JSON, nullable=True,
    )
    verify_after_date: Mapped[date | None] = mapped_column(
        Date, nullable=True,
    )
    # Backtest anchor (PR #224, migration 0033). NULL = live mode.
    # Non-null = "pretend it's that date" — every ctx fetch filters
    # to data with ts/published_at <= as_of_date, and the verifier
    # uses as_of_date + 5 trading days as the post-window.
    as_of_date: Mapped[date | None] = mapped_column(
        Date, nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )


class DiscussionTurn(Base):
    __tablename__ = "discussion_turns"
    __table_args__ = (
        # Unique (PR #218): `inject_user_message` computes turn_index
        # = max + 1 from a SELECT, so two concurrent injects could
        # both write the same index. The unique gate surfaces the
        # race as an IntegrityError; the service retries.
        Index(
            "ix_discussion_turns_lookup",
            "discussion_id", "round", "turn_index",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    discussion_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("discussions.id", ondelete="CASCADE"),
        nullable=False,
    )
    round: Mapped[int] = mapped_column(Integer, nullable=False)
    turn_index: Mapped[int] = mapped_column(Integer, nullable=False)
    persona_id: Mapped[str] = mapped_column(String(64), nullable=False)
    stance: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default="",
    )
    citations: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
