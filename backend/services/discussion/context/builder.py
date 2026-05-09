"""Orchestrator for `build_market_context`.

Builds the default `ctx` shape, fans out HTTP-bound blocks in
parallel via `asyncio.gather`, then runs DB-bound blocks serially
(SQLAlchemy `AsyncSession` is not safe to share across concurrent
awaits).

Each block writes its result into a dedicated `ctx[<key>]` and
records any exception via the shared `record_error` callback so a
single connector outage just blanks one block instead of failing the
whole assembly. Errors are surfaced in `ctx["errors"]` so personas
(and the synthesizer) can mention "context was incomplete" instead
of confidently citing missing data.

Public API:

    ctx = await build_market_context(
        db,
        market="TW",
        as_of=date(2025, 1, 15),     # None = live mode
        focus_symbols=["2330"],
        owner_id=user_id,
        top_n=8,
    )

The returned dict shape is fixed (default keys are always present)
so the prompt template and the round-context replay path don't have
to reason about missing keys. Empty list / None means "no signal";
`ctx["errors"]` carries diagnostic detail when something failed.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from .blocks import (
    chip, derivatives, http, lessons, news, overseas, owner, risk, technical,
)

# Progress callback type. Caller (e.g. `run_round`) provides one so
# the long ctx-gathering window (~15-30 s when news sentiment scoring
# fires inline) can surface intermediate "in progress" events to the
# user instead of going silent. None disables progress emission.
ProgressCb = Callable[[str], Awaitable[None]]

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)


def _initial_ctx(*, market: str, as_of: date | None) -> dict[str, Any]:
    """Default empty-shape ctx. Every key is always present so the
    prompt template doesn't have to handle missing keys; values
    populate during the gather/serial passes below."""
    return {
        "market": market,
        "captured_at": datetime.now(UTC).isoformat(),
        "backtest": as_of is not None,
        "as_of": as_of.isoformat() if as_of is not None else None,
        "top_gainers": [],
        "top_losers": [],
        "index": None,
        "news_sentiment": None,
        # Backtest auto-backfill diagnostic (PR #216). Populated only
        # in backtest mode (`as_of != None`); shape:
        # `{covered: bool, backfilled: int, error?: str, skipped?: str}`.
        # Lets the user distinguish "archive truly doesn't reach this
        # date" from "FINMIND_TOKEN is missing/free-tier" or
        # "FinMind upstream error" without digging into ctx["errors"].
        "news_backfill": None,
        "per_symbol_news_sentiment": {},
        # Per-symbol Tier-1 short-term technicals (volume_ratio,
        # return_5d, return_20d, rsi_14, gap_pct). Computed from
        # `ohlcv_daily` via `services.short_term_signals` for each
        # focus symbol. Empty dict when focus_symbols is empty OR
        # the archive lacks >= 21 trading days for any of them.
        "short_term_signals": {},
        "focus_briefs": [],
        "macro": None,
        "user_context": None,
        "prior_discussions": [],
        "international_sentiment": None,
        "top_foreign_buyers": [],
        "margin_balance_trend": None,
        "top_revenue_growers": [],
        "active_buybacks": [],
        "govt_bank_flow_5d": [],
        "risk_warnings": {
            "active_dispositions": [],
            "recent_suspensions": [],
            "high_day_trading_ratio": [],
        },
        "market_institutional_5d": [],
        # TAIFEX 三大法人台指期未平倉 (TW only, FinMind-backed). When
        # `as_of` is set the cache key includes the anchor so backtest
        # replays don't re-fan-out to FinMind. Shape:
        # `{contract, as_of, session_count, fini, sitc, dealer, trend}`
        # — fini / sitc / dealer carry `{net_oi, change_5d}`.
        "taifex_positioning": None,
        # Per-stock futures (個股期貨) institutional net-OI shifts
        # (PR #282). List of `{symbol, contract_id, fini_net_oi,
        # fini_change, fini_long_oi, fini_short_oi, as_of, from_ts,
        # industry, name_zh}` ranked by 5-day foreign-net-OI delta
        # descending. TW-only.
        "single_stock_futures_oi": [],
        # TAIWAN VIX 臺指選擇權波動率指數 (PR #283). Shape:
        # `{as_of, value, from_ts, from_value, change_pct}`.
        # Populated only when `tw_vix_daily` has rows in the
        # window — `taiwan_vix` key is omitted entirely when the
        # archive is empty so the schema annotation's gating
        # cleanly hides the prompt mention.
        "taiwan_vix": None,
        # Market-wide rolling 30-day 法說會 / 除息 calendar
        # (PR #284). Shape:
        # `[{symbol, next_event, next_event_in_days, next_event_date}, ...]`
        # sorted soonest-first. Universe is the set of symbols
        # already in other ctx blocks (top_foreign_buyers /
        # top_revenue_growers / single_stock_futures_oi /
        # focus_briefs / focus_symbols) — biases coverage toward
        # what the discussion is already tracking.
        "upcoming_events_calendar": [],
        # Per-focus-symbol 主力分點 (broker concentration, PR #285).
        # List of `{symbol, as_of, from_ts, session_count,
        # top_buyers: [{broker, broker_id, net_buy_shares}, ...],
        # top_sellers: [...]}`. Live FinMind read with 24h Redis
        # cache; no DB table. Bounded fan-out (≤ 5 focus_symbols)
        # to cap Sponsor quota burn per discussion.
        "broker_concentration": [],
        # Past-discussion lessons retrieved by `discussion_lesson_service`
        # for the same market + per focus_symbol. Shape:
        # `{market: [LessonSummary, ...], per_symbol: {sym: [...], ...}}`.
        # Stays empty when the learning loop is disabled
        # (`LESSONS_INJECTION_ENABLED=False`) or the owner has no
        # priors yet. Owner-scoped + backtest-time-safe (a sweep
        # discussion at as_of=2025-06-01 only sees lessons from
        # before that date).
        "recent_lessons": {"market": [], "per_symbol": {}},
        # Overseas index snapshot (PR #269): SOX / NDX / SPX / DJI /
        # VIX latest close + 1-day % change. Wired in for ALL
        # markets — TW personas need overnight US direction, US
        # personas need it for self-consistency. Shape:
        # `{as_of, indices: [{symbol, name, close, prev_close, change_pct}, ...]}`.
        "overseas_indicators": None,
        "errors": [],
    }


def _make_error_recorder(ctx: dict[str, Any]):
    """Closure that logs at ERROR + appends `{source, error}` to
    `ctx['errors']`. Each block calls this on failure so the
    diagnostic surface is uniform."""
    def _record(source: str, exc: Exception) -> None:
        log.error(
            "discussion.context.connector_failed",
            extra={"source": source, "error": str(exc)},
        )
        ctx["errors"].append({"source": source, "error": str(exc)})
    return _record


async def build_market_context(
    db: "AsyncSession",
    *,
    market: str = "TW",
    top_n: int = 8,
    focus_symbols: list[str] | None = None,
    owner_id: UUID | None = None,
    exclude_discussion_id: UUID | None = None,
    as_of: date | None = None,
    max_focus_symbols: int = 5,
    progress_cb: ProgressCb | None = None,
    topic: str | None = None,
) -> dict[str, Any]:
    ctx = _initial_ctx(market=market, as_of=as_of)
    record_error = _make_error_recorder(ctx)

    async def _progress(stage: str) -> None:
        """Emit a progress milestone if the caller wired a callback,
        else no-op. Wrapped so the body of build_market_context stays
        readable — `await _progress("X")` reads as a one-liner instead
        of `if progress_cb: await progress_cb("X")` everywhere.
        Callback failure is intentionally allowed to bubble up — if
        the caller's queue/SSE pipe is broken there's no point
        continuing the gather."""
        if progress_cb is not None:
            await progress_cb(stage)

    # ── concurrent HTTP-bound blocks ───────────────────────────────
    # Each block goes through `*_autosession` service helpers (or
    # in-memory caches) — none of them touch the shared `db`, which
    # makes them safe to fan out via `asyncio.gather`. Sequential
    # cold-cache would burn 5-7s before the first persona could
    # speak; parallel fall to ~max(any one).
    await _progress("fetching_market_data")
    await asyncio.gather(
        http.fetch_screener(
            ctx, market=market, top_n=top_n,
            as_of=as_of, record_error=record_error,
        ),
        http.fetch_index(
            ctx, market=market, as_of=as_of, record_error=record_error,
        ),
        http.fetch_macro(
            ctx, as_of=as_of, record_error=record_error,
        ),
        http.fetch_focus_briefs(
            ctx, market=market, focus_symbols=focus_symbols,
            as_of=as_of, record_error=record_error,
        ),
    )

    # ── DB-bound blocks (sequential on shared session) ─────────────
    # SQLAlchemy AsyncSession is not safe to share across concurrent
    # awaits. Each read is fast (~5-10ms) so total DB-bound cost is
    # ~50-100ms, well below the HTTP wall clock above.

    # Per-symbol short-term technicals — works for any market with an
    # `ohlcv_daily` archive (TW today, US/crypto in future). Doesn't
    # depend on TW-specific tables so it lives outside the `if TW`
    # gate below.
    await technical.fetch_short_term_signals(
        ctx, db, market=market, focus_symbols=focus_symbols,
        as_of=as_of, record_error=record_error,
        max_focus_symbols=max_focus_symbols,
    )

    # Overseas index snapshot — fires for all markets. TW personas
    # need overnight US direction (SOX leadership / VIX / risk-on
    # vs risk-off); US personas get it for free with the same
    # one-call cost.
    await overseas.fetch_overseas_indicators(
        ctx, as_of=as_of, record_error=record_error,
    )

    if market == "TW":
        # TAIFEX positioning fires regardless of focus_symbols — it's
        # a market-wide directional signal personas use even on
        # symbol-less topics ("外資台指期淨空 → 短線偏空").
        await derivatives.fetch_taifex_positioning(
            ctx, as_of=as_of, record_error=record_error,
        )
        await chip.fetch_top_foreign_buyers(
            ctx, db, as_of=as_of, record_error=record_error,
        )
        # PR #282: 個股期貨 三大法人未平倉 — futures-side
        # complement to top_foreign_buyers. TW only.
        await chip.fetch_top_stock_futures_buyers(
            ctx, db, as_of=as_of, record_error=record_error,
        )
        # PR #283: TAIWAN VIX (臺指選擇權波動率指數). TW only —
        # different volatility regime from US `^VIX` in
        # overseas_indicators, useful side-by-side.
        await chip.fetch_taiwan_vix(
            ctx, db, as_of=as_of, record_error=record_error,
        )
        await chip.fetch_margin_balance_trend(
            ctx, db, as_of=as_of, record_error=record_error,
        )
        await chip.fetch_top_revenue_growers(
            ctx, db, as_of=as_of, record_error=record_error,
        )
        await chip.fetch_active_buybacks(
            ctx, db, as_of=as_of, record_error=record_error,
        )
        await chip.fetch_govt_bank_flow(
            ctx, db, as_of=as_of, record_error=record_error,
        )
        await risk.fetch_risk_warnings(
            ctx, db, as_of=as_of, record_error=record_error,
        )
        await chip.fetch_market_institutional(
            ctx, db, as_of=as_of, record_error=record_error,
        )
        # PR #284: market-wide 法說會 / 除息 calendar. Must fire
        # AFTER top_foreign_buyers / top_revenue_growers /
        # single_stock_futures_oi / focus_briefs because it draws
        # its symbol universe from those blocks' output. focus_briefs
        # is populated later (in the news/owner phase) — that's OK,
        # the universe is "everything in ctx so far" + focus_symbols,
        # and a follow-up symbol arriving via focus_briefs just
        # means the next round picks it up.
        await chip.fetch_upcoming_events_calendar(
            ctx, market=market,
            focus_symbols=focus_symbols,
            as_of=as_of, record_error=record_error,
        )
        # PR #285: per-focus-symbol 主力分點. Live FinMind +
        # cache, no DB. focus_symbols-only fan-out keeps quota
        # bounded.
        await chip.fetch_broker_concentration(
            ctx,
            focus_symbols=focus_symbols,
            as_of=as_of, record_error=record_error,
        )

    # Backtest mode: convert `as_of` (date) → datetime anchor at
    # end-of-day so `published_at <= as_of_dt` covers everything
    # posted on that day. News readers operate on datetime; chip
    # readers above operate on date.
    as_of_dt = (
        datetime.combine(as_of, datetime.max.time(), tzinfo=UTC)
        if as_of is not None else None
    )

    # News sentiment is the single slowest block (~15-30 s when
    # inline scoring kicks in for backtest backfill). Emit a
    # progress event so the UI's preparing card can switch to
    # "scoring news sentiment..." and the user knows why this
    # phase is taking longer than the others.
    await _progress("scoring_news_sentiment")
    await news.fetch_market_sentiment(
        ctx, db, market=market, as_of_dt=as_of_dt,
        record_error=record_error,
        focus_symbols=focus_symbols,
    )
    await news.fetch_international_sentiment(
        ctx, db, as_of_dt=as_of_dt, record_error=record_error,
    )
    await news.fetch_per_symbol_sentiment(
        ctx, db, market=market, focus_symbols=focus_symbols,
        as_of_dt=as_of_dt, record_error=record_error,
        max_focus_symbols=max_focus_symbols,
    )

    if owner_id is not None:
        await owner.fetch_user_context(
            ctx, db, owner_id=owner_id, focus_symbols=focus_symbols,
            record_error=record_error,
        )
        if focus_symbols:
            await owner.fetch_prior_discussions(
                ctx, db, owner_id=owner_id,
                focus_symbols=focus_symbols,
                exclude_discussion_id=exclude_discussion_id,
                as_of_dt=as_of_dt, record_error=record_error,
            )
        # Past-discussion lessons are owner-scoped — only fired when
        # we have an owner_id. Backtest-time-safe via `as_of`.
        # `topic` (PR-J2) lets the lessons block compute a query
        # embedding for semantic-similarity ranking — None falls
        # back to the pre-J2 time/symbol-only ranking gracefully.
        await lessons.fetch_recent_lessons(
            ctx, db, owner_id=owner_id, market=market,
            focus_symbols=focus_symbols, as_of=as_of,
            record_error=record_error,
            topic=topic,
        )

    await _progress("ctx_ready")
    return ctx
