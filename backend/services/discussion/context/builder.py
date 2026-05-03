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
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from .blocks import chip, http, news, owner, risk

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
) -> dict[str, Any]:
    ctx = _initial_ctx(market=market, as_of=as_of)
    record_error = _make_error_recorder(ctx)

    # ── concurrent HTTP-bound blocks ───────────────────────────────
    # Each block goes through `*_autosession` service helpers (or
    # in-memory caches) — none of them touch the shared `db`, which
    # makes them safe to fan out via `asyncio.gather`. Sequential
    # cold-cache would burn 5-7s before the first persona could
    # speak; parallel fall to ~max(any one).
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

    if market == "TW":
        await chip.fetch_top_foreign_buyers(
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

    # Backtest mode: convert `as_of` (date) → datetime anchor at
    # end-of-day so `published_at <= as_of_dt` covers everything
    # posted on that day. News readers operate on datetime; chip
    # readers above operate on date.
    as_of_dt = (
        datetime.combine(as_of, datetime.max.time(), tzinfo=UTC)
        if as_of is not None else None
    )

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

    return ctx
