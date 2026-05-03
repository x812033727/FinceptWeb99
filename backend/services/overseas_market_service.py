"""Overseas (US + global) market indicators for the discussion context.

TW market opens at 09:00 TPE (= 21:00 ET previous day, after the US
close). Personas reasoning about TW direction at the open need a
read on what US did overnight — without it, they over-weight TW-
internal signals (chip flows, margin balance, technicals) and miss
fed-policy / SOX-leadership / risk-off shifts that propagate across
markets within hours.

Surfaced as a small ctx block with the latest close + 1-day %
change for a curated universe of macro-relevant indices and ETFs
(future tickers are intentionally omitted — yfinance's futures
data is delayed + the spot indices already convey the same
direction signal). Personas can mention `SOX -2.3% 拖累台積電`
without us paying for a tool call to fetch the value.

Source: yfinance, no API key needed. Same connector as the rest
of the US market services. Cached at the cache-tier defaults
(`TTL_QUOTE_US_OFF_HOURS` = 5 min during off-hours, fresh during
US session).

Backtest mode (`as_of != None`): falls through to historical bars
via `yfinance.history(period='5d')` and picks the close <= as_of.
Live mode uses `get_quote` for the latest tick. Same-day futures
reads are deliberately disabled in backtest mode — they don't have
meaningful day-ago closes per series (futures roll), so we'd leak
"current contract" data into a historical anchor.

Read-only — no new tables.
"""
from __future__ import annotations

import json
import logging
from datetime import date as _date, datetime as _dt, timezone as _tz
from typing import Any

from cache.redis_cache import cache_get, cache_set

log = logging.getLogger(__name__)


# Curated universe. Each entry: ticker → display name. The display
# name surfaces in the ctx block so personas reading the prompt
# don't have to know what `^IXIC` is. Ordered roughly by relevance
# to TW-market direction (semis-heavy index first).
_OVERSEAS_INDICES: dict[str, str] = {
    "^SOX":  "PHLX 半導體",          # leading indicator for TW chip stocks
    "^IXIC": "NASDAQ Composite",
    "^GSPC": "S&P 500",
    "^DJI":  "道瓊工業",
    "^VIX":  "VIX 波動率指數",        # serves as a partial options_iv_skew proxy
}

# How long the result is cached. Indices barely move during TW
# trading hours (US is closed), so a long cache is fine; the cron-
# free, lazy-fetch pattern keeps this from hammering yfinance.
_CACHE_TTL_SECONDS = 5 * 60   # 5 min
_CACHE_KEY_PREFIX = "overseas_indicators"


async def _fetch_one_live(ticker: str) -> dict[str, Any] | None:
    """Single-ticker fetch for live mode. Returns None on connector
    failure so the aggregator can skip-and-continue rather than
    blanking the whole block."""
    # Lazy import — yfinance pulls in pandas; keeping the import
    # at function scope means dev sandboxes / lightweight test
    # contexts can import this module without the full data stack.
    from data.us import yfinance_connector as yfin
    try:
        q = await yfin.get_quote(ticker)
    except Exception as exc:
        log.warning(
            "overseas.quote_failed",
            extra={"ticker": ticker, "error": str(exc)},
        )
        return None
    if not q or not q.get("price"):
        return None
    return {
        "symbol":     ticker,
        "name":       _OVERSEAS_INDICES[ticker],
        "close":      q["price"],
        "prev_close": q["prev_close"],
        "change_pct": q["change_pct"],
    }


async def _fetch_one_backtest(
    ticker: str, *, as_of: _date,
) -> dict[str, Any] | None:
    """Backtest-mode fetch: pull a short window of daily bars and
    pick the close on/before `as_of` plus the previous bar for the
    1-day change. Skips returning a row when fewer than 2 bars are
    available within the window — without two bars we can't compute
    the change %."""
    from data.us import yfinance_connector as yfin
    try:
        bars = await yfin.get_history(
            ticker, period="10d", interval="1d",
        )
    except Exception as exc:
        log.warning(
            "overseas.history_failed",
            extra={"ticker": ticker, "as_of": as_of.isoformat(),
                   "error": str(exc)},
        )
        return None
    if not bars:
        return None
    # Bar timestamps are ms-since-epoch UTC. Pick the last bar with
    # date <= as_of (end-of-day UTC inclusive).
    cutoff_ms = int(
        _dt(as_of.year, as_of.month, as_of.day, 23, 59, 59,
            tzinfo=_tz.utc).timestamp() * 1000
    )
    eligible = [b for b in bars if (b.get("time") or 0) <= cutoff_ms]
    if len(eligible) < 2:
        return None
    last = eligible[-1]
    prev = eligible[-2]
    last_close = float(last["close"])
    prev_close = float(prev["close"])
    if not prev_close:
        return None
    change_pct = round((last_close / prev_close - 1) * 100, 4)
    return {
        "symbol":     ticker,
        "name":       _OVERSEAS_INDICES[ticker],
        "close":      last_close,
        "prev_close": prev_close,
        "change_pct": change_pct,
    }


async def get_overseas_snapshot(
    *, as_of: _date | None = None,
) -> dict[str, Any]:
    """Return `{as_of, indices: [...]}` for the discussion ctx.

    Live mode (`as_of=None`) fans out one `get_quote` per ticker
    and caches the rolled-up dict for `_CACHE_TTL_SECONDS` so
    parallel discussion runs don't hammer yfinance.

    Backtest mode reads daily bars and picks the as-of close. The
    bars endpoint is cheap enough (single call per ticker, ~5
    rows) that the per-as_of cache key suffices — no warm-up cron
    needed.
    """
    cache_key = (
        f"{_CACHE_KEY_PREFIX}:{as_of.isoformat() if as_of else 'live'}"
    )
    cached_raw = await cache_get(cache_key)
    if cached_raw is not None:
        try:
            return json.loads(cached_raw)
        except (json.JSONDecodeError, TypeError):
            # Corrupt cache row — fall through to fresh fetch.
            log.warning("overseas.cache_decode_failed",
                        extra={"key": cache_key})

    rows: list[dict[str, Any]] = []
    for ticker in _OVERSEAS_INDICES:
        row = (
            await _fetch_one_backtest(ticker, as_of=as_of)
            if as_of is not None
            else await _fetch_one_live(ticker)
        )
        if row is not None:
            rows.append(row)

    out = {
        "as_of": as_of.isoformat() if as_of else None,
        "indices": rows,
    }
    if rows:
        # Don't cache empty results — a transient yfinance outage
        # would otherwise lock 5 min of zero state. Mirrors the
        # tw_market_service / us_market_service "don't cache empty"
        # convention.
        await cache_set(
            cache_key, json.dumps(out, ensure_ascii=False),
            _CACHE_TTL_SECONDS,
        )
    return out


__all__ = ["get_overseas_snapshot"]
