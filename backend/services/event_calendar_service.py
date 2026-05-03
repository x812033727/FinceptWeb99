"""Per-symbol upcoming corporate-event calendar (法說 / 除息).

Today's source is yfinance ``Ticker.calendar`` — Yahoo aggregates
法說會 + 除息 announcements from the issuers' filings, so coverage
is reasonable for TW listings (especially TWSE 上市) and gaps
gracefully on under-covered 興櫃 / 創新板 names.

Used by the discussion's per-symbol short_term_signals block to
warn personas about *event risk*: a focus stock with earnings in
2 days has asymmetric move potential that overrides whatever the
technical signals say. The canonical persona response is "停看聽
避開法說前後" rather than directional commentary.

Caching: per (market, symbol) for 24h. Calendar dates rarely move
intra-day, so a daily refresh cycle is plenty even in active
trading periods. Cache key includes the lookup date so backtest
mode can replay the same event-state the personas would have seen.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime
from typing import Any

from cache.redis_cache import cache_get, cache_set

log = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 24 * 3600
# Yahoo expects TW listings as `<symbol>.TW` — TWSE (上市). 上櫃
# uses `.TWO`. We default to .TW; callers needing 上櫃 can pass a
# pre-suffixed ticker.
_TW_SUFFIX = ".TW"
# Personas care about events in this window; further-out dates are
# just noise relative to a 1-5 day horizon. None when no events
# inside the window so the field reads cleanly in the prompt.
_LOOKAHEAD_DAYS = 14


def _suffix_for_market(market: str, symbol: str) -> str:
    """Map our internal symbol → yfinance ticker. TW symbols need a
    ``.TW`` suffix; US / crypto pass through verbatim."""
    if market == "TW":
        if symbol.endswith((".TW", ".TWO")):
            return symbol
        return f"{symbol}{_TW_SUFFIX}"
    return symbol


def _days_until(iso: str | None, anchor: date) -> int | None:
    """Negative when the event is in the past, None when iso is None
    or unparseable. Caller filters to keep only future events inside
    the lookahead window."""
    if not iso:
        return None
    try:
        d = date.fromisoformat(iso[:10])
    except ValueError:
        return None
    return (d - anchor).days


async def get_upcoming_event(
    market: str,
    symbol: str,
    as_of: date | None = None,
    *,
    lookahead_days: int = _LOOKAHEAD_DAYS,
) -> dict[str, Any] | None:
    """Return the next scheduled event(s) for the symbol within
    `lookahead_days`. Shape:

        {
          "as_of":            "2026-05-03",
          "earnings_date":    "2026-05-12" | None,
          "earnings_in_days": 9 | None,
          "ex_dividend_date": "2026-06-10" | None,
          "ex_dividend_in_days": 38 | None,
          "next_event":       "earnings" | "ex_dividend" | None,
          "next_event_in_days": 9 | None,
        }

    Returns None when:
      - the connector lookup fails (yfinance offline, ticker not on
        Yahoo, etc.), OR
      - no event of either type lands within `lookahead_days`.

    Cached per (market, symbol, as_of) for 24h so a multi-persona
    round only fires one yfinance Ticker.calendar fan-out per
    focus symbol per day.
    """
    anchor = as_of or datetime.now(UTC).date()
    cache_key = f"event_calendar:{market}:{symbol}:{anchor.isoformat()}"
    try:
        cached = await cache_get(cache_key)
    except Exception:
        cached = None
    if cached:
        try:
            decoded = json.loads(cached)
            # Sentinel cached-None: "_no_event" keeps a busy round
            # from re-fanning out yfinance just to learn the symbol
            # has nothing pending.
            return None if decoded == "_no_event" else decoded
        except json.JSONDecodeError:
            pass

    ticker = _suffix_for_market(market, symbol)
    try:
        from data.us import yfinance_connector
        raw = await yfinance_connector.get_calendar(ticker)
    except Exception as exc:
        log.warning(
            "event_calendar.fetch_failed",
            extra={"symbol": symbol, "ticker": ticker, "error": str(exc)},
        )
        return None
    if not raw:
        return None

    earnings_in = _days_until(raw.get("earnings_date"), anchor)
    ex_div_in = _days_until(raw.get("ex_dividend_date"), anchor)

    # Filter to events in `[0, lookahead_days]` — past dates and
    # far-future dates aren't actionable for short-term decisions.
    earnings_visible = (
        earnings_in is not None and 0 <= earnings_in <= lookahead_days
    )
    ex_div_visible = (
        ex_div_in is not None and 0 <= ex_div_in <= lookahead_days
    )
    if not (earnings_visible or ex_div_visible):
        # Cache the empty result so we don't keep re-fetching for a
        # symbol Yahoo simply doesn't carry events for.
        try:
            await cache_set(cache_key, json.dumps("_no_event"), _CACHE_TTL_SECONDS)
        except Exception:
            pass
        return None

    # Pick the soonest event as `next_event` so personas can grep one
    # field for the trigger date instead of computing across two.
    next_event: str | None = None
    next_event_in_days: int | None = None
    if earnings_visible and ex_div_visible:
        if earnings_in <= ex_div_in:
            next_event, next_event_in_days = "earnings", earnings_in
        else:
            next_event, next_event_in_days = "ex_dividend", ex_div_in
    elif earnings_visible:
        next_event, next_event_in_days = "earnings", earnings_in
    elif ex_div_visible:
        next_event, next_event_in_days = "ex_dividend", ex_div_in

    result = {
        "as_of":               anchor.isoformat(),
        "earnings_date":       raw.get("earnings_date") if earnings_visible else None,
        "earnings_in_days":    earnings_in if earnings_visible else None,
        "ex_dividend_date":    raw.get("ex_dividend_date") if ex_div_visible else None,
        "ex_dividend_in_days": ex_div_in if ex_div_visible else None,
        "next_event":          next_event,
        "next_event_in_days":  next_event_in_days,
    }
    try:
        await cache_set(cache_key, json.dumps(result), _CACHE_TTL_SECONDS)
    except Exception:
        pass
    return result


# ── Market-wide aggregation (PR #284) ─────────────────────────────


# Default window for the broader-market calendar — a 30-day forward
# horizon catches a full earnings cycle without dragging in events
# so far out (60+ days) that they're noise relative to short-term
# decisions.
_MARKET_WIDE_LOOKAHEAD_DAYS = 30


async def get_market_upcoming_events(
    market: str,
    symbols: list[str],
    *,
    as_of: date | None = None,
    lookahead_days: int = _MARKET_WIDE_LOOKAHEAD_DAYS,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Aggregate upcoming events across `symbols`, sorted by
    `next_event_in_days` ascending. Each entry has the same fields
    `get_upcoming_event` returns plus a `symbol` key + a flat
    `next_event_date` so the discussion-prompt rendering doesn't
    have to branch on event type.

    Caller usage (PR #284): collect the universe of "currently
    relevant" symbols from already-populated ctx blocks
    (`top_foreign_buyers` / `top_revenue_growers` /
    `single_stock_futures_oi` / `focus_briefs` / `focus_symbols`)
    so the calendar surfaces events for stocks the personas are
    already tracking, not a static large-cap list. The ctx-driven
    universe biases coverage toward what's actionable in this
    discussion.

    Each per-symbol lookup hits the existing 24h cache, so
    repeated rounds within a discussion (or back-to-back
    discussions over the same trending stocks) only fan out the
    underlying yfinance call once per symbol per day.

    Defensive handling: per-symbol failures are silently dropped
    (the event_calendar already returns None on connector error),
    so a single mid-list yfinance hiccup doesn't blank the whole
    block.
    """
    if not symbols:
        return []

    seen: set[str] = set()
    events: list[dict[str, Any]] = []
    for sym in symbols:
        sym = (sym or "").strip()
        if not sym or sym in seen:
            continue
        # Synthetic index symbols (`_TAIEX`, `_TAIEX_TR`) carry no
        # corporate calendar — skip without a yfinance call.
        if sym.startswith("_"):
            continue
        seen.add(sym)
        ev = await get_upcoming_event(
            market, sym, as_of, lookahead_days=lookahead_days,
        )
        if ev is None or ev.get("next_event") is None:
            continue
        next_event = ev["next_event"]
        next_event_date = (
            ev.get("earnings_date") if next_event == "earnings"
            else ev.get("ex_dividend_date")
        )
        events.append({
            "symbol":              sym,
            "next_event":          next_event,
            "next_event_in_days":  ev["next_event_in_days"],
            "next_event_date":     next_event_date,
        })

    # Sort by soonest-first; ties broken by symbol so the
    # prompt's day-3 cluster has a stable order across runs
    # (keeps audit / signal-trend reads reproducible).
    events.sort(key=lambda e: (e["next_event_in_days"], e["symbol"]))
    return events[:limit]
