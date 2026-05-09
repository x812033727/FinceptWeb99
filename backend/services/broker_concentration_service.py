"""Per-symbol 主力分點 (broker concentration) for the discussion ctx
(PR #285).

Closes the broker_dot_concentration item from PR #262's
post-mortem-gap taxonomy. Personas reasoning about TW stocks
care about WHICH brokers are accumulating / distributing —
"凱基台北 連 5 日淨買 +50K 張" is a much richer signal than
"the stock has been going up". The branch-dot pattern catches:
  - 主力 stake-building before a public breakout
  - Insider distribution disguised as a steady price climb
  - 公司派 buyback execution at specific brokers

Architecture trade-off (vs the previous-PR DB-cron pattern):
  - Per-stock fan-out at scale would be ~1700 FinMind calls/day,
    burning the entire Sponsor hourly budget. Live read with
    a focused universe (focus_symbols only) keeps this to 1-3
    calls per discussion.
  - Backtest mode just queries FinMind with the historical
    date range — same surface as live. No DB table needed.
  - Redis cache keyed by (symbol, as_of) handles repeated
    queries (e.g. multi-day sweeps re-running the same
    backtest date).

Cache TTL: 24h. Branch-level positioning data updates daily
post-close, and a 24h cache survives the weekend without going
stale. Cache key includes the lookup date so backtest replays
produce stable hits.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

from cache.redis_cache import cache_get, cache_set_unless_empty

log = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 24 * 3600
_DEFAULT_DAYS = 5
# Hard upper bound on per-discussion fan-out so a topic that
# happens to mention 50 symbols (mass-screener pattern) doesn't
# burn the full Sponsor quota. Personas only need a handful of
# 主力分點 reads to ground their reasoning.
_DEFAULT_MAX_SYMBOLS = 5


def _net_buy_share(buy: int | None, sell: int | None) -> int:
    """Net buy in shares, treating None as 0 (FinMind sometimes
    omits one side of a row when a broker only bought OR sold)."""
    return int(buy or 0) - int(sell or 0)


async def get_top_brokers_for_symbol(
    symbol: str,
    *,
    as_of: date | None = None,
    days: int = _DEFAULT_DAYS,
    top_n: int = 3,
) -> dict[str, Any] | None:
    """Top buyers + top sellers (by 5-day net buy) for a single
    symbol. Returns:

        {
          "symbol":   "2330",
          "as_of":    "2026-04-15",
          "from_ts":  "2026-04-08",
          "session_count": 5,
          "top_buyers":  [
              {"broker": "凱基台北", "broker_id": "9200",
               "net_buy_shares": 12345},
              ...
          ],
          "top_sellers": [...],   # most-NEGATIVE net buyers
        }

    Returns None when:
      - the connector lookup fails (FinMind quota / outage), OR
      - FinMind returns zero rows for the window (the symbol may
        not be heavily traded at any single broker).

    Cached per (symbol, as_of) for 24h.
    """
    anchor = as_of or datetime.now(UTC).date()
    cache_key = (
        f"broker_concentration:{symbol}:{anchor.isoformat()}:{days}:{top_n}"
    )
    try:
        cached = await cache_get(cache_key)
    except Exception:
        cached = None
    if cached:
        try:
            decoded = json.loads(cached)
            # Sentinel None — keeps a busy round from re-fanning
            # FinMind for a symbol that has no broker data in window.
            return None if decoded == "_no_data" else decoded
        except json.JSONDecodeError:
            pass

    # FinMind expects YYYY-MM-DD strings. We pull `days * 2` calendar
    # days back to comfortably span weekends + a holiday cluster
    # without missing the 5th trading day.
    start = (anchor - timedelta(days=days * 2 + 5)).isoformat()
    end = anchor.isoformat()

    try:
        from data.tw import finmind_connector
        rows = await finmind_connector.get_broker_concentration_per_symbol(
            symbol, start, end,
        )
    except Exception as exc:
        log.warning(
            "broker_concentration.fetch_failed",
            extra={"symbol": symbol, "error": str(exc)},
        )
        return None
    if not rows:
        # Permanent-empty: cache a sentinel so symbols FinMind has no
        # broker breakdown for don't keep re-fetching.
        await cache_set_unless_empty(
            cache_key, json.dumps("_no_data"), _CACHE_TTL_SECONDS,
        )
        return None

    # Aggregate per (broker, broker_id) over the window.
    by_broker: dict[
        tuple[str, str],
        dict[str, Any],
    ] = {}
    days_seen: set[str] = set()
    for r in rows:
        broker = (r.get("securities_trader") or "").strip()
        broker_id = str(r.get("securities_trader_id") or "").strip()
        if not broker:
            continue
        # Window-clamp: as_of is the top boundary; FinMind sometimes
        # returns rows up to the latest available date, which can
        # exceed `anchor` for live mode. Drop those.
        ts = (r.get("date") or "").strip()[:10]
        if ts:
            days_seen.add(ts)
            try:
                row_d = date.fromisoformat(ts)
            except ValueError:
                continue
            if row_d > anchor:
                continue
        net = _net_buy_share(r.get("buy"), r.get("sell"))
        key = (broker, broker_id)
        cell = by_broker.get(key) or {
            "broker": broker, "broker_id": broker_id,
            "net_buy_shares": 0,
        }
        cell["net_buy_shares"] += net
        by_broker[key] = cell

    if not by_broker:
        await cache_set_unless_empty(
            cache_key, json.dumps("_no_data"), _CACHE_TTL_SECONDS,
        )
        return None

    aggregated = list(by_broker.values())
    aggregated.sort(key=lambda c: c["net_buy_shares"], reverse=True)
    top_buyers = [
        c for c in aggregated[:top_n] if c["net_buy_shares"] > 0
    ]
    top_sellers = [
        c for c in reversed(aggregated[-top_n:]) if c["net_buy_shares"] < 0
    ]

    sorted_days = sorted(days_seen)
    result = {
        "symbol":         symbol,
        "as_of":          sorted_days[-1] if sorted_days else anchor.isoformat(),
        "from_ts":        sorted_days[0] if sorted_days else anchor.isoformat(),
        "session_count":  len(sorted_days),
        "top_buyers":     top_buyers,
        "top_sellers":    top_sellers,
    }
    await cache_set_unless_empty(
        cache_key, json.dumps(result, ensure_ascii=False), _CACHE_TTL_SECONDS,
    )
    return result


async def get_broker_concentration_for_focus(
    focus_symbols: list[str] | None,
    *,
    as_of: date | None = None,
    max_symbols: int = _DEFAULT_MAX_SYMBOLS,
) -> list[dict[str, Any]]:
    """Per-focus-symbol top-broker snapshot. Capped at `max_symbols`
    to bound FinMind quota burn — even on a backtest sweep with
    500 spawned discussions, each discussion's fan-out is ≤ 5
    cached calls.

    Returns a list (not a dict) so the order matches the caller's
    `focus_symbols` ordering — personas reading the prompt see
    the topic-mentioned stocks in the same order they typed them.
    Symbols without broker data are omitted; the empty-list case
    is handled by the ctx block (block omitted entirely).
    """
    if not focus_symbols:
        return []
    out: list[dict[str, Any]] = []
    for sym in focus_symbols[:max_symbols]:
        sym = (sym or "").strip()
        if not sym or sym.startswith("_"):
            continue
        snapshot = await get_top_brokers_for_symbol(sym, as_of=as_of)
        if snapshot is not None:
            out.append(snapshot)
    return out


__all__ = [
    "get_top_brokers_for_symbol",
    "get_broker_concentration_for_focus",
]
