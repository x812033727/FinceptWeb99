"""Taiwan-market derivatives signal helpers.

Today: TAIFEX 三大法人台指期未平倉部位 (TX-contract index futures
positioning) — the canonical "smart money" directional indicator for
the TW market. When 外資 builds net long positions in TX overnight
the index typically gaps up the next session; sustained 外資 net
selling in TX is the cleanest leading indicator for short-term
weakness across the cash market.

Pulls from FinMind's `TaiwanFuturesInstitutionalInvestors` dataset
via the public connector wrapper added in PR #232. Cached per
(contract, as_of) for 4 hours so a busy day of round button
presses doesn't burn the FinMind hourly quota — TAIFEX data
publishes ~once per day after market close, so a 4h TTL is well
inside the staleness budget.

Future expansion: per-stock individual-stock futures (個股期),
options-derived P/C ratios, after-hours session positioning. All
follow the same caching shape.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

from cache.redis_cache import cache_get, cache_set
from data.tw import finmind_connector as finmind

log = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 4 * 3600   # 4 hours
# How many calendar days back to fetch to cover ~5 trading sessions
# even with a holiday cluster.
_LOOKBACK_DAYS = 14
# Minimum sessions required to compute a 5-day change. Below this we
# return None rather than emit a partial number.
_MIN_SESSIONS = 2

# FinMind investor-name mapping. The dataset can return rows in a few
# different conventions across years; we normalise to the canonical
# Chinese labels personas already understand.
_INVESTOR_LABELS = {
    "外資":              "fini",
    "外資及陸資":         "fini",
    "Foreign_Investor":  "fini",
    "投信":              "sitc",
    "Investment_Trust":  "sitc",
    "自營商":            "dealer",
    "自營商(自行買賣)":   "dealer",
    "自營商(避險)":       "dealer",
    "Dealer":            "dealer",
    "Dealer_Self":       "dealer",
    "Dealer_Hedge":      "dealer",
}


def _net_oi(row: dict[str, Any]) -> int | None:
    """Long-OI minus short-OI for a single FinMind row. Tolerant of
    the few field-name variants the dataset has shipped with."""
    long_keys = (
        "long_open_interest_balance_volume",
        "open_position_long_volume",
        "long_open_interest_balance",
    )
    short_keys = (
        "short_open_interest_balance_volume",
        "open_position_short_volume",
        "short_open_interest_balance",
    )
    long_v = next((row[k] for k in long_keys if k in row and row[k] is not None), None)
    short_v = next((row[k] for k in short_keys if k in row and row[k] is not None), None)
    if long_v is None or short_v is None:
        return None
    try:
        return int(long_v) - int(short_v)
    except (TypeError, ValueError):
        return None


def _classify_trend(fini_change: int | None, threshold: int = 1_000) -> str:
    """Foreign investors dominate TAIFEX flow, so we anchor the trend
    label on 外資's 5-day net OI change. Threshold is in contracts; 1k
    contracts is roughly NT$ 6-8 億 worth of TX exposure, large enough
    to filter day-to-day noise."""
    if fini_change is None:
        return "neutral"
    if fini_change > threshold:
        return "bullish"
    if fini_change < -threshold:
        return "bearish"
    return "neutral"


async def get_taifex_positioning(
    contract: str = "TX",
    as_of: date | None = None,
) -> dict[str, Any] | None:
    """Per-investor net OI snapshot + 5-day change for the index
    futures contract. Returns:

        {
          "contract": "TX",
          "as_of": "2026-04-30",
          "session_count": int,             # sessions seen in window
          "fini":  {"net_oi": int, "change_5d": int|None},
          "sitc":  {"net_oi": int, "change_5d": int|None},
          "dealer":{"net_oi": int, "change_5d": int|None},
          "trend": "bullish" | "bearish" | "neutral",
        }

    None when FinMind returns no rows (quota exhausted, paywall, or
    genuine empty window). Cached for `_CACHE_TTL_SECONDS` per
    (contract, as_of) — the same backtest replayed twice in a row
    only fans out one FinMind call.
    """
    end = as_of or datetime.now(UTC).date()
    cache_key = f"taifex:positioning:{contract}:{end.isoformat()}"
    try:
        cached = await cache_get(cache_key)
    except Exception:
        cached = None
    if cached:
        try:
            return json.loads(cached)
        except json.JSONDecodeError:
            pass

    start = end - timedelta(days=_LOOKBACK_DAYS)
    try:
        rows = await finmind.get_futures_institutional(
            contract, start.isoformat(), end.isoformat(),
        )
    except Exception as exc:
        log.warning(
            "taifex_positioning.fetch_failed",
            extra={"contract": contract, "error": str(exc)},
        )
        return None
    if not rows:
        return None

    # Pivot: dates set + per-(date, group) net OI.
    by_date_group: dict[tuple[str, str], int] = {}
    dates: set[str] = set()
    for r in rows:
        d = r.get("date")
        name = r.get("name") or r.get("institutional_investors") or ""
        group = _INVESTOR_LABELS.get(name)
        if not d or not group:
            continue
        net = _net_oi(r)
        if net is None:
            continue
        # Multiple dealer sub-rows per date sum into one group total.
        by_date_group[(d, group)] = by_date_group.get((d, group), 0) + net
        dates.add(d)

    if len(dates) < _MIN_SESSIONS:
        return None

    sorted_dates = sorted(dates)
    latest_date = sorted_dates[-1]
    earliest_date = sorted_dates[0]

    def _summary(group: str) -> dict[str, int | None]:
        latest = by_date_group.get((latest_date, group))
        earliest = by_date_group.get((earliest_date, group))
        change = (
            latest - earliest if latest is not None and earliest is not None
            else None
        )
        return {"net_oi": latest, "change_5d": change}

    result = {
        "contract":      contract,
        "as_of":         latest_date,
        "session_count": len(dates),
        "fini":          _summary("fini"),
        "sitc":          _summary("sitc"),
        "dealer":        _summary("dealer"),
        "trend":         _classify_trend(_summary("fini")["change_5d"]),
    }

    try:
        await cache_set(cache_key, json.dumps(result), _CACHE_TTL_SECONDS)
    except Exception:
        pass
    return result


# ── Per-symbol securities lending (借券) ──────────────────────────


_SBL_LOOKBACK_DAYS = 14
_SBL_MIN_SESSIONS = 2
# Trend bands as a fraction of the mean balance, not absolute contracts —
# stocks vary by orders of magnitude in absolute lending balance, so a
# percentage-relative threshold travels better.
_SBL_TREND_BAND = 0.05


def _sbl_balance(row: dict[str, Any]) -> int | None:
    """Securities-lending balance for a FinMind row. Tolerant of the
    field-name variants the dataset has shipped with — some have
    `securities_lending_balance`, some plain `balance`, some
    `lending_balance`."""
    keys = (
        "securities_lending_balance",
        "lending_balance",
        "balance",
    )
    for k in keys:
        v = row.get(k)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
    return None


def _sbl_volume(row: dict[str, Any]) -> int | None:
    keys = (
        "securities_lending_volume",
        "lending_volume",
        "transaction_volume",
        "volume",
    )
    for k in keys:
        v = row.get(k)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
    return None


async def get_securities_lending_trend(
    symbol: str,
    as_of: date | None = None,
) -> dict[str, Any] | None:
    """Per-symbol 借券 (securities lending) 5-day trend. Returns:

        {
          "as_of":             "2026-04-30",
          "session_count":     int,
          "latest_balance":    int,        # 借券餘額 latest session
          "balance_change_5d": int,        # earliest → latest
          "latest_volume":     int|None,   # 當日成交量
          "mean_volume_5d":    int|None,
          "trend":             "rising" | "stable" | "falling",
        }

    `trend` based on `balance_change_5d` vs the latest balance:
      change > +5% of latest → "rising" (institutional short-side pressure building)
      change < -5% of latest → "falling" (covering)
      else                   → "stable"

    None when FinMind returns no rows or fewer than `_SBL_MIN_SESSIONS`
    sessions in the window. Cached per (symbol, as_of) for
    `_CACHE_TTL_SECONDS` so multi-persona rounds don't re-fan-out.
    """
    end = as_of or datetime.now(UTC).date()
    cache_key = f"sbl:trend:{symbol}:{end.isoformat()}"
    try:
        cached = await cache_get(cache_key)
    except Exception:
        cached = None
    if cached:
        try:
            return json.loads(cached)
        except json.JSONDecodeError:
            pass

    start = end - timedelta(days=_SBL_LOOKBACK_DAYS)
    try:
        rows = await finmind.get_securities_lending(
            symbol, start.isoformat(), end.isoformat(),
        )
    except Exception as exc:
        log.warning(
            "sbl_trend.fetch_failed",
            extra={"symbol": symbol, "error": str(exc)},
        )
        return None
    if not rows:
        return None

    # Pivot by date → (balance, volume). FinMind returns one row per
    # date for the symbol; multiple rows on the same date (rare) get
    # last-write-wins which is fine for trend calc.
    by_date: dict[str, dict[str, int | None]] = {}
    for r in rows:
        d = r.get("date")
        if not d:
            continue
        by_date[d] = {
            "balance": _sbl_balance(r),
            "volume":  _sbl_volume(r),
        }

    sorted_dates = sorted(by_date.keys())
    if len(sorted_dates) < _SBL_MIN_SESSIONS:
        return None

    latest_date = sorted_dates[-1]
    earliest_date = sorted_dates[0]
    latest_balance = by_date[latest_date]["balance"]
    earliest_balance = by_date[earliest_date]["balance"]
    if latest_balance is None or earliest_balance is None:
        return None

    balance_change = latest_balance - earliest_balance
    if latest_balance == 0:
        trend = "stable"
    else:
        rel = balance_change / abs(latest_balance)
        if rel > _SBL_TREND_BAND:
            trend = "rising"
        elif rel < -_SBL_TREND_BAND:
            trend = "falling"
        else:
            trend = "stable"

    volumes = [
        v["volume"] for v in by_date.values() if v["volume"] is not None
    ]
    latest_volume = by_date[latest_date]["volume"]
    mean_volume = int(sum(volumes) / len(volumes)) if volumes else None

    result = {
        "as_of":             latest_date,
        "session_count":     len(sorted_dates),
        "latest_balance":    latest_balance,
        "balance_change_5d": balance_change,
        "latest_volume":     latest_volume,
        "mean_volume_5d":    mean_volume,
        "trend":             trend,
    }

    try:
        await cache_set(cache_key, json.dumps(result), _CACHE_TTL_SECONDS)
    except Exception:
        pass
    return result
