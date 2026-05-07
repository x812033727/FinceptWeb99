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
    # PR-2: structured delta between `conclusion` and
    # `post_mortem_conclusion`, computed at synthesis time. Lets the
    # UI render a "what changed" card without recomputing on every
    # read. NULL when the discussion never went through post-mortem,
    # either side parse-errored, or the row is from before PR-2.
    post_mortem_diff: dict[str, Any] | None = None
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


class PostMortemWinnerOut(BaseModel):
    """One recommended symbol that crossed the win threshold inside
    the evaluation window. Surfaced both in the `verdict` block (for
    clients that want to render badges) and used by the backend to
    label the skip reason."""
    symbol: str
    peak_pct: float
    peak_day: str


class PostMortemVerdictOut(BaseModel):
    """The win/miss/insufficient_data scoring of the recommendation
    against the D1-D{window} window. `status='win'` means the
    self-critique was skipped — no LLM round was injected."""
    status: str
    threshold_pct: float
    window_days: int
    winners: list[PostMortemWinnerOut]
    best_pct: float | None
    reason: str


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

    Win-skip: when `status="skipped"` the recommendation already
    cleared the win threshold so no critique LLM round is fired.
    `verdict.winners` carries the symbols that hit the bar; the
    UI renders a "✅ 推薦已達標" badge instead of the critique
    flow. `injected_turn_id` is null in that case.
    """
    # New shape (PR #273).
    trading_days: list[str] = []
    recommended_performance: list[PostMortemRecommendedPerformanceOut] = []
    daily_top_gainers: list[PostMortemDailyGainersOut] = []

    # Back-compat aliases — older frontends read these directly.
    next_trading_day: str
    top_gainers: list[PostMortemGainerOut]

    # Outcome-aware fields (learning-loop PR).
    status: str = "ran"   # "ran" | "skipped"
    verdict: PostMortemVerdictOut | None = None
    injected_turn_id: int | None = None


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
    """POST body for creating a sweep job. When `strategy_id` is
    set, server resolves the template and uses its fields for any
    omitted-or-empty caller field — letting the UI submit just
    `{strategy_id, anchor_date, trading_days_count}` for a
    "load + go" flow."""
    topic: str | None = None
    rules: str | None = None
    market: str | None = None
    persona_ids: list[str] | None = None
    anchor_date: str   # ISO date — coerced to date in the service layer
    trading_days_count: int = Field(..., ge=1, le=60)
    rounds_per_discussion: int | None = Field(default=None, ge=1, le=5)
    concurrency: int | None = Field(default=None, ge=1, le=3)
    auto_post_mortem: bool | None = None
    strategy_id: uuid.UUID | None = None


class PersonaWeightLearnResponse(BaseModel):
    """PR-C: outcome of a strategy weight-learn pass.

    `updated=False` is not an error — it surfaces "no eligible
    persona has reached MIN_SAMPLES yet" so the UI can render
    a hint instead of a generic failure."""
    updated: bool
    reason: str | None
    weights: dict[str, float]
    samples: dict[str, int]


class StrategyTemplateCreate(BaseModel):
    """POST body for creating a strategy template (PR-A)."""
    name: str = Field(..., min_length=1, max_length=120)
    description: str | None = None
    topic: str = Field(..., min_length=1)
    rules: str = Field(..., min_length=1)
    market: str
    persona_ids: list[str] = Field(..., min_length=1)
    default_rounds: int = Field(default=1, ge=1, le=5)
    default_concurrency: int = Field(default=1, ge=1, le=3)
    default_auto_post_mortem: bool = True
    # PR-D: optional auto-schedule fields. Disabled by default.
    auto_schedule_enabled: bool = False
    auto_schedule_cadence_hours: int = Field(default=24, ge=1, le=720)
    auto_schedule_anchor_offset_days: int = Field(default=-1, ge=-30, le=0)
    auto_schedule_trading_days_count: int = Field(default=1, ge=1, le=30)


class StrategyTemplateUpdate(BaseModel):
    """PATCH body — every field optional. Server validates the
    merged shape so a partial update can't leave an uncreatable
    template behind."""
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    topic: str | None = Field(default=None, min_length=1)
    rules: str | None = Field(default=None, min_length=1)
    market: str | None = None
    persona_ids: list[str] | None = Field(default=None, min_length=1)
    default_rounds: int | None = Field(default=None, ge=1, le=5)
    default_concurrency: int | None = Field(default=None, ge=1, le=3)
    default_auto_post_mortem: bool | None = None
    auto_schedule_enabled: bool | None = None
    auto_schedule_cadence_hours: int | None = Field(
        default=None, ge=1, le=720,
    )
    auto_schedule_anchor_offset_days: int | None = Field(
        default=None, ge=-30, le=0,
    )
    auto_schedule_trading_days_count: int | None = Field(
        default=None, ge=1, le=30,
    )


class SweepAggregatePersona(BaseModel):
    persona_id: str
    discussions_count: int
    win_count: int
    hit_rate: float | None
    agree_turn_count: int
    dissent_turn_count: int


class SweepAggregateLesson(BaseModel):
    category: str
    lesson_text: str
    as_of_date: str
    related_symbols: list[str]
    created_at: str | None


class SweepAggregateVerdictCounts(BaseModel):
    win: int
    loss: int
    unverifiable: int
    pending: int


class SweepAggregateResponse(BaseModel):
    """Folded KPIs for one sweep or one strategy template (PR-B).

    Same shape for both scopes; extra fields populated per scope:
    - scope='sweep'    -> sweep_id + anchor_date + completed_count
    - scope='strategy' -> sweep_count
    """
    scope: str
    sweep_id: str | None = None
    strategy_id: str | None = None
    sweep_count: int | None = None
    anchor_date: str | None = None
    trading_days_count: int | None = None
    completed_count: int | None = None
    failed_count: int | None = None
    discussions_total: int
    verdict_counts: SweepAggregateVerdictCounts
    win_rate: float | None
    avg_pnl_pct: list[float | None]
    per_persona: list[SweepAggregatePersona]
    lessons: list[SweepAggregateLesson]


class StrategyTemplateResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    topic: str
    rules: str
    market: str
    persona_ids: list[str]
    default_rounds: int
    default_concurrency: int
    default_auto_post_mortem: bool
    persona_weights: dict[str, float]
    weights_updated_at: datetime | None
    auto_schedule_enabled: bool
    auto_schedule_cadence_hours: int
    auto_schedule_anchor_offset_days: int
    auto_schedule_trading_days_count: int
    auto_schedule_last_run_at: datetime | None
    # PR-4a: lifecycle tier
    maturity_tier: str = "cold_start"
    maturity_computed_at: datetime | None = None
    # PR-4b: walk-forward auto-promote knobs
    auto_promote_enabled: bool = False
    auto_promote_min_oos_brier_improvement: float = 0.0
    auto_promote_min_oos_hit_rate: float = 0.5
    # PR-4c: per-strategy persona status map. Personas not present
    # default to 'active'. Keys are persona IDs; values one of
    # 'active' / 'frozen' / 'shadow'.
    persona_status: dict[str, str] = {}
    persona_status_updated_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


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
    strategy_id: uuid.UUID | None   # PR-A: source template, if any
    resolved_dates: list[str]
    completed_dates: list[str]
    failed_dates: list[BacktestSweepFailedDate]
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    cancelled_at: datetime | None


class StrategyBrierHistoryPoint(BaseModel):
    """One point in a strategy's per-sweep Brier time-series.
    Each sweep that has at least one resolved discussion
    contributes one point. NULLs surface for sweeps where the
    metric isn't applicable (e.g. pre-PR-C0 raw_brier or
    cold-start calibrated_brier with partial coverage)."""
    sweep_id: uuid.UUID
    anchor_date: str | None
    completed_at: datetime | None
    fold_kind: str | None
    raw_brier: float | None
    calibrated_brier: float | None
    samples: int


class WalkForwardRequest(BaseModel):
    """PR-A1 follow-up — kick off a walk-forward run for a strategy.

    `anchor_date` is the *most-recent* day the test fold should
    cover; the orchestrator walks back N folds from there. Default
    train/test windows match the C-axis paper anchors agreed in
    PR-A1 (rolling 60d train + 20d test).
    """
    anchor_date: str   # ISO date — coerced to date in the service
    train_window_days: int = Field(default=60, ge=1, le=120)
    test_window_days: int = Field(default=20, ge=1, le=120)
    n_folds: int = Field(default=2, ge=1, le=6)
    rounds_per_discussion: int = Field(default=1, ge=1, le=5)
    concurrency: int = Field(default=1, ge=1, le=3)
    auto_post_mortem: bool = True


class WalkForwardFoldOut(BaseModel):
    fold_index: int
    train_anchor: str
    train_dates: list[str]
    test_anchor: str
    test_dates: list[str]


class WalkForwardPlanResponse(BaseModel):
    """Returned at /walk-forward kick-off — the resolved plan plus
    the orchestrator-task handle indicator. The actual sweep IDs
    are NOT in the response because the worker creates them
    asynchronously; clients poll
    GET /sweeps?fold_kind=train|test to follow progress."""
    strategy_id: uuid.UUID
    market: str
    train_window_days: int
    test_window_days: int
    folds: list[WalkForwardFoldOut]
    started: bool   # True when the background task was scheduled
