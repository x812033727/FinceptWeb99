"""Pydantic schemas for the discussion API.

Kept deliberately small — the full Discussion + Turn rows include
internal columns (owner_id, ingested_at) that the API doesn't need to
expose, so we project them through these response models.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class CreateDiscussionRequest(BaseModel):
    topic: str = Field(min_length=1, max_length=500)
    rules: str = Field(min_length=1, max_length=2000)
    persona_ids: list[str] = Field(min_length=2, max_length=8)
    # Market the discussion is anchored to. Optional for backwards
    # compat with clients that pre-date the field — `discussion_service`
    # falls back to the default ('TW') when None / missing. Validation
    # against `_VALID_MARKETS` happens in the service layer so the
    # allowed-set lives in one place.
    market: str | None = Field(default=None, max_length=8)
    # Backtest anchor (PR #224). NULL/missing = live mode (default).
    # ISO date string ("2025-01-15") = "pretend it's that date" —
    # ctx fetches filter to data on/before that date and verifier
    # grades against the next 5 trading days.
    as_of_date: str | None = Field(default=None, max_length=10)


class UpdateDiscussionRequest(BaseModel):
    topic: str | None = Field(default=None, min_length=1, max_length=500)
    rules: str | None = Field(default=None, min_length=1, max_length=2000)
    persona_ids: list[str] | None = Field(default=None, min_length=2, max_length=8)
    market: str | None = Field(default=None, max_length=8)


class TurnResponse(BaseModel):
    id: int
    round: int
    turn_index: int
    persona_id: str
    stance: str
    content: str
    citations: dict[str, Any] | None = None
    created_at: datetime


class DiscussionResponse(BaseModel):
    id: uuid.UUID
    topic: str
    rules: str
    persona_ids: list[str]
    market: str
    status: str
    current_round: int
    conclusion: dict[str, Any] | None = None
    # PR #272: post-mortem self-critique conclusion. Populated when
    # the user runs the post-mortem flow (inject + new round +
    # re-conclude); the synthesizer routes its output here instead
    # of overwriting the original `conclusion`. Defaults None so
    # older clients ignore the field; new clients render a second
    # card alongside the original when populated.
    post_mortem_conclusion: dict[str, Any] | None = None
    # Self-grading fields (migration 0018). All optional / default-None
    # so legacy clients ignore them; frontend uses them to render the
    # dynamic title (YYYYMMDD(syms)勝/敗) instead of the user-typed topic.
    verdict: str | None = None
    verdict_reason: str | None = None
    verified_at: datetime | None = None
    auto_run: bool = False
    # Per-symbol price snapshots so the frontend can render
    # `4958:55/51 (-7.3%)` per recommended symbol without re-fetching
    # the OHLCV bars. Captured by the verifier task once the 5-day
    # window closes.
    day1_open_prices: dict[str, float] | None = None
    day5_close_prices: dict[str, float] | None = None
    # Per-day closes from the scoreboard cron (PR #140). Shape:
    # `{symbol: [d1, d2, d3, d4, d5]}` with NULL slots for unresolved
    # days. Surfaced so the sidebar title can render the latest
    # non-null close even before D5 lands, instead of showing `—/—`
    # until the full window completes.
    daily_close_prices: dict[str, list[float | None]] | None = None
    # Backtest anchor (PR #224). Non-null = backtest mode; UI shows
    # a "回測" badge and the as_of date next to the topic.
    as_of_date: str | None = None
    created_at: datetime
    updated_at: datetime


class DiscussionDetailResponse(DiscussionResponse):
    turns: list[TurnResponse]


class ConclusionResponse(BaseModel):
    discussion_id: uuid.UUID
    conclusion: dict[str, Any]


class AutoRunConfigRequest(BaseModel):
    enabled: bool
    persona_ids: list[str] = Field(min_length=2, max_length=8)
    topic: str = Field(min_length=1, max_length=500)
    rules: str = Field(min_length=1, max_length=2000)
    market: str | None = Field(default=None, max_length=8)


class AutoRunConfigResponse(BaseModel):
    enabled: bool
    persona_ids: list[str]
    topic: str
    rules: str
    market: str
    updated_at: datetime | None = None


class InjectUserMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


class ScoreboardRow(BaseModel):
    symbol: str
    day1_open: float | None
    daily_closes: list[float | None]
    change_pcts: list[float | None]
    days_resolved: int


class PostMortemGainerOut(BaseModel):
    """One row in a daily post-mortem top-N leaderboard."""
    symbol: str
    change_pct: float
    close: float
    base_close: float
    trading_day: str       # PR #273: the day this gainer is for


class PostMortemDailyGainersOut(BaseModel):
    """All top-N gainers for a single trading day in the window."""
    trading_day: str
    gainers: list[PostMortemGainerOut]


class PostMortemDayPerformanceOut(BaseModel):
    """One recommended symbol's close + cumulative-since-as_of
    change on a single trading day."""
    trading_day: str
    close: float
    change_pct: float


class PostMortemRecommendedPerformanceOut(BaseModel):
    """Per-recommendation D1-D5 self-evaluation row."""
    symbol: str
    base_close: float       # close on as_of_date (entry price)
    days: list[PostMortemDayPerformanceOut]


class PostMortemResponse(BaseModel):
    """Result of `POST /sessions/{id}/post-mortem`.

    PR #273: evolved from the single-day next-day-gainers shape
    to a 5-trading-day window with two ground-truth views:
      - `recommended_performance`: each recommended symbol's own
        D1-D5 cumulative-since-as_of returns (跟自己比).
      - `daily_top_gainers`: per-day top-N leaderboards across
        D1-D5 (cross-section of what was actually trending).

    The flat `top_gainers` field is preserved for back-compat
    with older clients — populated from D1's leaderboard so the
    field still reflects "next-day gainers". `next_trading_day`
    similarly aliases to `trading_days[0]` when present.
    """
    # New shape (PR #273).
    trading_days: list[str] = []
    recommended_performance: list[PostMortemRecommendedPerformanceOut] = []
    daily_top_gainers: list[PostMortemDailyGainersOut] = []

    # Back-compat aliases — older frontends read these directly.
    next_trading_day: str
    top_gainers: list[PostMortemGainerOut]

    injected_turn_id: int


class ScoreboardResponse(BaseModel):
    discussion_id: uuid.UUID
    # Anchor date the D1-D5 window starts from. For backtest discussions
    # this is `Discussion.as_of_date`; for live discussions it's
    # `to_tw_date(Discussion.created_at)`. Frontends should prefer this
    # field over `created_at_tw_date`.
    anchor_date: str
    # Backwards-compatible alias of `anchor_date` for older frontends
    # that haven't migrated. Same value in both modes now (the live-mode
    # value is unchanged from before backtest support).
    created_at_tw_date: str
    rows: list[ScoreboardRow]


# ── Backtest sweep (PR #274) ─────────────────────────────────────


class BacktestSweepCreate(BaseModel):
    """POST body for creating a sweep job."""
    topic: str = Field(..., min_length=1)
    rules: str = Field(..., min_length=1)
    market: str
    persona_ids: list[str] = Field(..., min_length=1)
    anchor_date: str   # ISO date — coerced to date in the service layer
    trading_days_count: int = Field(..., ge=1, le=60)
    rounds_per_discussion: int = Field(default=1, ge=1, le=5)
    concurrency: int = Field(default=1, ge=1, le=3)
    # PR #275: auto-trigger the post-mortem self-critique after each
    # spawned discussion's conclude. Default True — if you're
    # running a multi-day backtest you almost always want the
    # critique attached.
    auto_post_mortem: bool = True


class BacktestSweepFailedDate(BaseModel):
    date: str
    error: str


class BacktestSweepResponse(BaseModel):
    """Server's view of a sweep, surfaced to the operator UI for
    progress polling. `resolved_dates` populates after /start;
    `completed_dates` / `failed_dates` grow as the worker
    advances."""
    id: uuid.UUID
    status: str
    topic: str
    rules: str
    market: str
    persona_ids: list[str]
    anchor_date: str
    trading_days_count: int
    rounds_per_discussion: int
    concurrency: int
    auto_post_mortem: bool   # PR #275
    resolved_dates: list[str]
    completed_dates: list[str]
    failed_dates: list[BacktestSweepFailedDate]
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    cancelled_at: datetime | None
