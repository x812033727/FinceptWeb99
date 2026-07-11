"""Intraday OHLCV bars aggregated from the `quote_snapshots` archive (A2 分時).

One shared implementation behind `GET /api/{us|tw|crypto}/intraday/{symbol}`.
The quote refresh task writes one last-price snapshot per subscribed symbol
per minute during market hours; this service rolls those ticks up into
1m / 5m / 15m OHLCV bars.

Coverage
--------
Snapshots are pruned after `tasks.ingest_quotes_retention_tw.RETENTION_DAYS`
(30 days), so intraday bars can never reach further back than that. The
response carries `coverage_days` so the UI can label the limitation. A
symbol with no snapshots (e.g. never subscribed, or a market whose refresh
task doesn't persist snapshots yet — today only TW does) returns 200 with
an empty `bars` list rather than 404: "no intraday data" is an expected
state, not an error.

Volume semantics
----------------
Snapshot `volume` stores the *cumulative session volume* as reported by the
quote upstream (TWSE 累計成交量 / normalized quote `volume`), not a per-tick
delta. Per-bar volume is therefore the difference between consecutive
buckets' cumulative volume within the same UTC calendar day, clamped at 0
(upstream corrections can briefly regress). Both the TW session
(09:00–13:30 CST = 01:00–05:30 UTC) and the US session (09:30–16:00 ET =
13:30–21:00 UTC) fall inside a single UTC day, so a UTC-day boundary is a
safe session separator. The first bar of a day reports the raw cumulative
value — i.e. volume traded from the session open up to that bar — which is
exact when snapshots start at the open and an over-estimate otherwise.

Why Python-side bucketing instead of SQL date_trunc
---------------------------------------------------
The blueprint sketched a `date_trunc` aggregation, but (a) the test suite
runs on SQLite where `date_trunc` doesn't exist, (b) first/last-in-bucket
open/close semantics need dialect-specific window tricks, and (c) the
cumulative-volume differencing needs a `lag()` over buckets anyway. Row
count is bounded by the 30-day retention prune (~8k rows for a TW symbol
at 60 s cadence over ~4.5 h sessions), so a narrow 3-column indexed fetch
plus a linear scan is cheap, portable, and unit-testable.
"""
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel

from cache.cache_ttls import TTL_INTRADAY_BARS
from cache.redis_cache import cache_get_json, cache_set_json, key_intraday
from services.ingest.repository import read_quote_snapshots_range_autosession
from tasks.ingest_quotes_retention_tw import RETENTION_DAYS as SNAPSHOT_COVERAGE_DAYS

INTERVAL_SECONDS: dict[str, int] = {"1m": 60, "5m": 300, "15m": 900}


class IntradayBar(BaseModel):
    time: int               # bucket start, Unix ms UTC (chart intraday convention)
    open: float
    high: float
    low: float
    close: float
    volume: int


class IntradayResponse(BaseModel):
    symbol: str
    market: str
    interval: str           # "1m" | "5m" | "15m"
    # Snapshot retention window — the furthest back `bars` can ever reach.
    # Surfaced so the UI can label the 30-day limitation next to the chart.
    coverage_days: int
    bars: list[IntradayBar]


def aggregate_snapshot_bars(
    rows: list[tuple[datetime, float | None, int | None]],
    interval_seconds: int,
) -> list[dict[str, Any]]:
    """Roll ts-ascending (ts, last_price, cumulative_volume) ticks into
    OHLCV bars bucketed on `interval_seconds` boundaries (epoch-aligned,
    same alignment `date_trunc` would give for 1m/5m/15m).

    Pure function — unit-tested directly. Ticks without a price are
    skipped; ticks without a volume leave the bucket's cumulative volume
    untouched. See module docstring for the volume-differencing rules.
    """
    bars: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    cur_cum: int | None = None          # max cumulative volume seen in bucket
    prev_day: str | None = None         # UTC date of previously flushed bar
    prev_cum: int | None = None         # cumulative volume at previous flush

    def _flush() -> None:
        nonlocal prev_day, prev_cum
        if cur is None:
            return
        day = datetime.fromtimestamp(cur["time"] // 1000, tz=UTC).date().isoformat()
        cum = cur_cum
        same_day = prev_day == day
        if cum is None:
            volume = 0
        elif same_day and prev_cum is not None:
            volume = max(0, cum - prev_cum)
        else:
            volume = cum
        cur["volume"] = volume
        bars.append(cur)
        if not same_day:
            prev_cum = None      # never difference across a session/day boundary
        if cum is not None:
            prev_cum = cum
        prev_day = day

    for ts, price, volume in rows:
        if price is None:
            continue
        epoch = int(ts.timestamp())
        bucket = (epoch // interval_seconds) * interval_seconds
        bucket_ms = bucket * 1000
        if cur is None or cur["time"] != bucket_ms:
            _flush()
            cur = {
                "time": bucket_ms,
                "open": price, "high": price, "low": price, "close": price,
            }
            cur_cum = volume
        else:
            cur["high"] = max(cur["high"], price)
            cur["low"] = min(cur["low"], price)
            cur["close"] = price
            if volume is not None:
                cur_cum = volume if cur_cum is None else max(cur_cum, volume)
    _flush()
    return bars


async def get_intraday(market: str, symbol: str, interval: str) -> dict[str, Any]:
    """Aggregated intraday bars for (market, symbol). `interval` must be a
    key of INTERVAL_SECONDS (routers enforce via Query pattern)."""
    market = market.upper()
    symbol = symbol.upper()
    key = key_intraday(market.lower(), symbol, interval)
    cached = await cache_get_json(key)
    if cached is not None:
        return cached

    start = datetime.now(UTC) - timedelta(days=SNAPSHOT_COVERAGE_DAYS)
    rows = await read_quote_snapshots_range_autosession(market, symbol, start=start)
    payload: dict[str, Any] = {
        "symbol": symbol,
        "market": market,
        "interval": interval,
        "coverage_days": SNAPSHOT_COVERAGE_DAYS,
        "bars": aggregate_snapshot_bars(rows, INTERVAL_SECONDS[interval]),
    }
    # Don't cache empties: a transient DB blip must not pin "no data" for
    # the TTL, and the underlying indexed fetch is cheap for symbols that
    # genuinely have no snapshots.
    if payload["bars"]:
        await cache_set_json(key, payload, TTL_INTRADAY_BARS)
    return payload
