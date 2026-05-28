"""TWSE MIS (Market Information System) intraday quote connector.

The TWSE OpenAPI `STOCK_DAY_ALL` endpoint that drives our existing
quote / screener reads is an end-of-day cross-section — it only
refreshes ~14:30 Taipei after that day's settlement publishes. Until
then it serves the previous trading day's close, which is what made
intraday discussion ctx report yesterday's price as if it were today's.

The MIS endpoint at `mis.twse.com.tw/stock/api/getStockInfo.jsp` is
the same backend the public TWSE quote page (`mis.twse.com.tw/stock/`)
calls every 5 seconds. It returns delayed real-time ticks (~20s lag)
during the regular session (09:00-13:30 Taipei). Quoting from the
response envelope:

  {
    "rtcode": "0000",
    "msgArray": [{
      "ch":    "2330.tw",
      "tlong": "1716797400000",   // last trade timestamp (ms)
      "z":     "1095.00",          // last trade price (- if no trade)
      "tv":    "1500",             // last-trade volume (個股最後一筆量)
      "v":     "12345600",         // cumulative day volume
      "o":     "1100.00",          // open
      "h":     "1105.00",          // high
      "l":     "1095.00",          // low
      "y":     "1090.00",          // previous close
      "n":     "台積電",
      "ex":    "tse"
    }]
  }

Used as Tier 0 in `fetch_quote_waterfall` ONLY during regular session.
Outside the session window the connector returns None directly so the
existing STOCK_DAY_ALL → FinMind → DB-snapshot tiers handle pre-open,
post-close, and weekend cleanly without burning an extra HTTP call.

Auth: no API key. The endpoint *does* require a `JSESSIONID` cookie
that gets seeded by visiting any MIS page first. Production
deployments may run into 403s if the cookie hasn't been warmed up;
we therefore tolerate any non-2xx by returning None and falling
through to the next tier — defensive degradation is the same
contract the rest of the TW connector layer uses.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_BASE = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
_REFERER = "https://mis.twse.com.tw/stock/fibest.jsp?stock=2330"
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# MIS serves both TWSE-listed (`tse_` prefix) and TPEx-listed (`otc_`
# prefix) under the same endpoint. Use the symbol's resolved exchange
# to pick the right prefix.
_EX_PREFIX = {"TWSE": "tse_", "TPEx": "otc_"}

# Concurrency cap. MIS is a public web endpoint; the TWSE site itself
# fans out batched calls (multiple symbols in one `ex_ch=`) every 5s
# from each open page. A handful of in-flight per-symbol requests is
# well within tolerance; a global cap of 4 leaves room for the
# background WS pump + discussion ctx fan-out without contention.
_local_sem = asyncio.Semaphore(4)
_REQUEST_TIMEOUT_S = 8.0


def _build_ex_ch(symbol: str, exchange: str) -> str:
    prefix = _EX_PREFIX.get(exchange, "tse_")
    return f"{prefix}{symbol}.tw"


def _to_float(s: Any) -> float | None:
    """MIS uses `"-"` for missing fields (no trades yet, suspended,
    etc.). Strings with commas are unlikely on this endpoint but we
    strip defensively to match the TWSE / FinMind convention."""
    if s is None:
        return None
    txt = str(s).strip()
    if not txt or txt == "-":
        return None
    try:
        return float(txt.replace(",", ""))
    except ValueError:
        return None


def _to_int(s: Any) -> int:
    if s is None:
        return 0
    txt = str(s).strip()
    if not txt or txt == "-":
        return 0
    try:
        return int(txt.replace(",", ""))
    except ValueError:
        return 0


async def get_realtime_quote(
    symbol: str, *, exchange: str = "TWSE",
) -> dict[str, Any] | None:
    """Fetch one delayed-realtime quote for `symbol` from TWSE MIS.

    Returns None when:
      - HTTP error (4xx, 5xx, network failure)
      - `rtcode != "0000"` (symbol not found, malformed `ex_ch`,
        rate-limited)
      - `msgArray` empty
      - `z` (last trade) is `"-"` AND no `y` (prev close) — endpoint
        has no useful data for this symbol right now

    Returns the normalised quote dict shaped like
    `fetch_quote_waterfall` expects on success.

    `_to_float` returns None for empty fields; callers downstream
    already tolerate None on `close` / `prev_close` (they fall
    through to the next waterfall tier).
    """
    ex_ch = _build_ex_ch(symbol, exchange)
    params = {
        "json": "1",
        "delay": "0",
        "ex_ch": ex_ch,
        # `_` cache-buster — TWSE checks it but doesn't validate the
        # value beyond presence. ms-precision epoch matches what the
        # public page emits.
        "_": str(int(time.time() * 1000)),
    }
    headers = {
        "User-Agent": _USER_AGENT,
        "Referer": _REFERER,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
    }
    async with _local_sem:
        try:
            async with httpx.AsyncClient(
                timeout=_REQUEST_TIMEOUT_S, follow_redirects=False,
            ) as client:
                r = await client.get(_BASE, params=params, headers=headers)
                if r.status_code != 200:
                    logger.warning(
                        "twse_mis.http_error symbol=%s status=%d",
                        symbol, r.status_code,
                    )
                    return None
                data = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(
                "twse_mis.request_failed symbol=%s err=%s", symbol, exc,
            )
            return None

    if not isinstance(data, dict):
        return None
    if data.get("rtcode") != "0000":
        logger.info(
            "twse_mis.non_zero_rtcode symbol=%s rtcode=%s",
            symbol, data.get("rtcode"),
        )
        return None
    arr = data.get("msgArray") or []
    if not isinstance(arr, list) or not arr:
        return None
    row = arr[0]
    close = _to_float(row.get("z"))
    prev_close = _to_float(row.get("y"))
    # No trade yet AND no prior reference → nothing useful. Let the
    # waterfall fall through to STOCK_DAY_ALL which has yesterday's
    # close at minimum.
    if close is None and prev_close is None:
        return None
    return {
        "symbol":     symbol,
        "name_zh":    row.get("n") or "",
        "close":      close,
        "prev_close": prev_close,
        "open":       _to_float(row.get("o")),
        "high":       _to_float(row.get("h")),
        "low":        _to_float(row.get("l")),
        "volume":     _to_int(row.get("v")),
        # `tlong` is the last trade's ms epoch. Surfaced so the
        # waterfall can verify this tick is actually from today's
        # session — stale ticks (no trade yet today) return
        # yesterday's last-trade time + None close, which the
        # `close is None` check above already drops.
        "tlong":      row.get("tlong"),
    }
