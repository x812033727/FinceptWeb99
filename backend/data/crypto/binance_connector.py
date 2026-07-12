"""Binance connector — spot klines (OHLCV) + USDT-perp funding rate &
open interest, plus exchange-info symbol discovery for universe mapping.

Style mirrors `data.crypto.kraken_connector`: module-level async
functions, a fresh `httpx.AsyncClient` per call, fail-soft (warn + return
[]/None, never raise) so a transient Binance hiccup degrades to "no rows
this chunk" rather than crashing the ingest run. On top of that the raw
`_get` here honours Binance's 429/418 rate-limit responses with a short
bounded backoff that respects `Retry-After`, because backfilling a
top-200 universe issues thousands of kline calls.

Two audiences:
  - the FinMind ingest pipeline, via `finmind/ingest/selfcrawl/binance.py`
    which wraps `fetch_ohlcv` / `fetch_funding_rate` / `fetch_open_interest`
    into the `SourceClient.fetch` shape;
  - any future live-serving path, via the same functions.

Endpoints:
  spot klines            GET  api.binance.com/api/v3/klines
  spot exchangeInfo      GET  api.binance.com/api/v3/exchangeInfo
  perp funding history   GET  fapi.binance.com/fapi/v1/fundingRate
  perp open-interest hist GET fapi.binance.com/futures/data/openInterestHist
  perp exchangeInfo      GET  fapi.binance.com/fapi/v1/exchangeInfo
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_SPOT_BASE = "https://api.binance.com"
_FUT_BASE = "https://fapi.binance.com"
_HTTP_TIMEOUT = 15.0

# Binance klines / fundingRate cap responses at 1000 rows/call.
_MAX_LIMIT = 1000

# Interval → milliseconds, for advancing the pagination cursor.
_INTERVAL_MS: dict[str, int] = {
    "1h": 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
}

# Bounded retry on 429 (rate limit) / 418 (IP auto-ban warning).
_MAX_RETRIES = 4
_BACKOFF_BASE_S = 1.0
_BACKOFF_CAP_S = 30.0


async def _get(
    base: str, path: str, params: dict[str, Any] | None = None,
) -> Any | None:
    """One GET with Binance rate-limit handling. Returns the parsed JSON
    body, or None on any failure (logged as a warning). 429/418 trigger
    a bounded backoff honouring `Retry-After`; other 4xx/5xx and network
    errors fail soft immediately."""
    for attempt in range(_MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as c:
                r = await c.get(base + path, params=params)
            if r.status_code in (429, 418):
                if attempt >= _MAX_RETRIES:
                    logger.warning(
                        "binance.rate_limited_giveup",
                        extra={"path": path, "status": r.status_code},
                    )
                    return None
                retry_after = r.headers.get("Retry-After")
                delay = (
                    float(retry_after)
                    if retry_after and retry_after.isdigit()
                    else min(_BACKOFF_BASE_S * (2 ** attempt), _BACKOFF_CAP_S)
                )
                logger.warning(
                    "binance.rate_limited_retry",
                    extra={"path": path, "status": r.status_code, "delay_s": delay},
                )
                await asyncio.sleep(delay)
                continue
            if r.status_code != 200:
                logger.warning(
                    "binance.http_error",
                    extra={"path": path, "status": r.status_code},
                )
                return None
            return r.json()
        except Exception as exc:  # network jitter / decode
            logger.warning(
                "binance.request_failed",
                extra={"path": path, "error": str(exc)},
            )
            return None
    return None


def _date_to_ms(d: date, *, end: bool = False) -> int:
    """Midnight-UTC epoch ms for `d`. With `end=True`, the last
    millisecond of the day (so an inclusive [start, end] date range maps
    to a half-open ms window Binance understands)."""
    dt = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    ms = int(dt.timestamp() * 1000)
    return ms + (24 * 60 * 60 * 1000 - 1 if end else 0)


# ── Spot klines (OHLCV) ──────────────────────────────────────────


async def get_klines(
    symbol: str, interval: str, start_ms: int, end_ms: int,
) -> list[list]:
    """Paginated raw Binance kline arrays over [start_ms, end_ms]. Each
    element is Binance's 12-field array (openTime, o, h, l, c, volume,
    closeTime, quoteVolume, trades, ...). Walks forward 1000 rows at a
    time until the window is covered or a page comes back short/empty."""
    step = _INTERVAL_MS.get(interval)
    if step is None:
        logger.warning("binance.bad_interval", extra={"interval": interval})
        return []

    out: list[list] = []
    cursor = start_ms
    # Hard cap on pages so a pathological loop can't run away: enough for
    # ~5y of hourly bars (43800 / 1000 ≈ 44) with headroom.
    for _ in range(1000):
        if cursor > end_ms:
            break
        page = await _get(
            _SPOT_BASE, "/api/v3/klines",
            {
                "symbol": symbol,
                "interval": interval,
                "startTime": cursor,
                "endTime": end_ms,
                "limit": _MAX_LIMIT,
            },
        )
        if not page:
            break
        out.extend(page)
        if len(page) < _MAX_LIMIT:
            break
        # Advance past the last bar's open time to avoid re-fetching it.
        cursor = int(page[-1][0]) + step
    return out


async def fetch_ohlcv(
    symbol: str, interval: str, start_date: date, end_date: date,
) -> list[dict[str, Any]]:
    """OHLCV rows in the raw shape the ingest mapping consumes. `symbol`
    is the Binance pair (e.g. `BTCUSDT`). `ts` is an ISO-8601 UTC
    timestamp string (the bar's open time)."""
    rows = await get_klines(
        symbol, interval, _date_to_ms(start_date), _date_to_ms(end_date, end=True),
    )
    out: list[dict[str, Any]] = []
    for k in rows:
        try:
            open_ms = int(k[0])
        except (TypeError, ValueError, IndexError):
            continue
        ts = datetime.fromtimestamp(open_ms / 1000, tz=timezone.utc)
        out.append({
            "symbol": symbol,
            "interval": interval,
            "ts": ts.isoformat(),
            "open": k[1],
            "high": k[2],
            "low": k[3],
            "close": k[4],
            "volume": k[5],
            "quote_volume": k[7],
            "trades": k[8],
        })
    return out


# ── Perp funding rate ────────────────────────────────────────────


async def fetch_funding_rate(
    symbol: str, start_date: date, end_date: date,
) -> list[dict[str, Any]]:
    """USDT-perp funding-rate history for `symbol` (e.g. `BTCUSDT`).
    Binance settles every 8h; each row → `{symbol, funding_time (ISO),
    funding_rate, mark_price}`. Paginated 1000 rows/call."""
    start_ms, end_ms = _date_to_ms(start_date), _date_to_ms(end_date, end=True)
    out: list[dict[str, Any]] = []
    cursor = start_ms
    for _ in range(1000):
        if cursor > end_ms:
            break
        page = await _get(
            _FUT_BASE, "/fapi/v1/fundingRate",
            {"symbol": symbol, "startTime": cursor, "endTime": end_ms,
             "limit": _MAX_LIMIT},
        )
        if not page:
            break
        for row in page:
            try:
                ft_ms = int(row["fundingTime"])
            except (TypeError, ValueError, KeyError):
                continue
            out.append({
                "symbol": symbol,
                "funding_time": datetime.fromtimestamp(
                    ft_ms / 1000, tz=timezone.utc,
                ).isoformat(),
                "funding_rate": row.get("fundingRate"),
                "mark_price": row.get("markPrice"),
            })
        if len(page) < _MAX_LIMIT:
            break
        cursor = int(page[-1]["fundingTime"]) + 1
    return out


# ── Perp open interest (history ~30 days) ────────────────────────


async def fetch_open_interest(
    symbol: str, start_date: date, end_date: date, period: str = "1h",
) -> list[dict[str, Any]]:
    """USDT-perp open-interest history. Binance only serves ~30 days via
    /futures/data/openInterestHist, so deep backfill is impossible —
    this accumulates from go-live. Each row → `{symbol, ts (ISO),
    open_interest, open_interest_value}`. The date args bound the
    returned window client-side; Binance itself caps at 500 rows/period."""
    start_ms, end_ms = _date_to_ms(start_date), _date_to_ms(end_date, end=True)
    page = await _get(
        _FUT_BASE, "/futures/data/openInterestHist",
        {"symbol": symbol, "period": period, "startTime": start_ms,
         "endTime": end_ms, "limit": 500},
    )
    if not page:
        return []
    out: list[dict[str, Any]] = []
    for row in page:
        try:
            ts_ms = int(row["timestamp"])
        except (TypeError, ValueError, KeyError):
            continue
        out.append({
            "symbol": symbol,
            "ts": datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat(),
            "open_interest": row.get("sumOpenInterest"),
            "open_interest_value": row.get("sumOpenInterestValue"),
        })
    return out


# ── Exchange-info symbol discovery (universe mapping) ────────────


async def get_spot_usdt_symbols() -> set[str]:
    """Set of actively-TRADING spot USDT pairs (e.g. `BTCUSDT`). Used to
    map a CoinGecko coin to its Binance spot symbol for OHLCV."""
    body = await _get(_SPOT_BASE, "/api/v3/exchangeInfo", None)
    if not isinstance(body, dict):
        return set()
    return {
        s["symbol"]
        for s in body.get("symbols", [])
        if s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"
    }


async def get_perp_usdt_symbols() -> set[str]:
    """Set of actively-TRADING USDT-margined perpetual symbols — the
    universe for funding rate + open interest."""
    body = await _get(_FUT_BASE, "/fapi/v1/exchangeInfo", None)
    if not isinstance(body, dict):
        return set()
    return {
        s["symbol"]
        for s in body.get("symbols", [])
        if s.get("quoteAsset") == "USDT"
        and s.get("contractType") == "PERPETUAL"
        and s.get("status") == "TRADING"
    }
