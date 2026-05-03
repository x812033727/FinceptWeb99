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
