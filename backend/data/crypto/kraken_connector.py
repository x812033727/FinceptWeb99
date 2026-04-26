"""Kraken public REST connector — spot quote + OHLC.

Public endpoints don't need an API key. We always quote against USD; the
service layer treats USDT/USDC/DAI as USD when computing portfolio value.

Reference:
- Ticker:  https://api.kraken.com/0/public/Ticker?pair=XBTUSD
- OHLC:    https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=60
- Pairs:   https://api.kraken.com/0/public/AssetPairs

Kraken responses are wrapped in {error: [...], result: {...}}. We treat
non-empty error arrays as failures and fall back to None.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from data.crypto.symbols import (
    TOP20,
    from_kraken_response_key,
    to_kraken_pair,
)

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.kraken.com/0/public"
_HTTP_TIMEOUT = 10.0


# Kraken OHLC interval values (minutes). Mapped from our generic interval strings.
_KRAKEN_INTERVAL = {
    "1m": 1, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "4h": 240,
    "1d": 1440, "1w": 10080,
}


async def _get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Hit Kraken public endpoint; return result dict or None on error."""
    url = f"{_BASE_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(url, params=params)
            if resp.status_code != 200:
                logger.warning("kraken.http_error", extra={
                    "path": path, "status": resp.status_code,
                })
                return None
            data = resp.json()
            if data.get("error"):
                logger.warning("kraken.api_error", extra={
                    "path": path, "errors": data["error"],
                })
                return None
            return data.get("result")
    except Exception as exc:  # noqa: BLE001 — network jitter shouldn't crash service
        logger.warning("kraken.fetch_failed", extra={"path": path, "error": str(exc)})
        return None


async def get_quote(symbol: str) -> dict[str, Any] | None:
    """Latest spot quote for a canonical symbol (BTC, ETH, ...).

    Returns:
        {symbol, price, change_pct, volume, high_24h, low_24h, currency}
        or None if Kraken doesn't list the pair / API call failed.
    """
    pair = to_kraken_pair(symbol)
    if pair is None:
        return None

    result = await _get("/Ticker", {"pair": pair})
    if not result:
        return None

    # Kraken returns {result: {<canonical_pair_key>: {<fields>}}}
    raw_key, ticker = next(iter(result.items()))
    canonical = from_kraken_response_key(raw_key) or symbol.upper()

    # Field shape (per Kraken docs):
    #   c = [last_price, last_volume]
    #   o = today's opening price
    #   v = [volume_today, volume_24h]
    #   h = [high_today, high_24h]
    #   l = [low_today, low_24h]
    try:
        last = float(ticker["c"][0])
        opening = float(ticker["o"])
        volume_24h = float(ticker["v"][1])
        high_24h = float(ticker["h"][1])
        low_24h = float(ticker["l"][1])
    except (KeyError, ValueError, IndexError) as exc:
        logger.warning("kraken.parse_failed", extra={
            "symbol": symbol, "error": str(exc),
        })
        return None

    change_pct = ((last - opening) / opening * 100.0) if opening else 0.0

    return {
        "symbol": canonical,
        "market": "CRYPTO",
        "price": last,
        "change_pct": change_pct,
        "volume": volume_24h,
        "high_24h": high_24h,
        "low_24h": low_24h,
        "currency": "USD",
    }


async def get_history(
    symbol: str, interval: str = "1d", limit: int = 365,
) -> list[dict[str, Any]]:
    """OHLCV bars for a symbol. Returns most recent `limit` bars.

    Kraken caps OHLC at 720 bars per request regardless of interval.
    """
    pair = to_kraken_pair(symbol)
    if pair is None:
        return []
    kraken_interval = _KRAKEN_INTERVAL.get(interval, 1440)

    result = await _get("/OHLC", {"pair": pair, "interval": kraken_interval})
    if not result:
        return []

    # First key is the pair, second is "last" (next-tick token, ignored).
    raw_key = next((k for k in result if k != "last"), None)
    if raw_key is None:
        return []
    rows = result[raw_key]

    bars: list[dict[str, Any]] = []
    for row in rows[-limit:]:
        try:
            ts, o, h, lo, c, _, vol, _ = row
            bars.append({
                "date": datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat(),
                "open": float(o),
                "high": float(h),
                "low": float(lo),
                "close": float(c),
                "volume": float(vol),
            })
        except (ValueError, IndexError):
            continue
    return bars


async def get_top_pairs() -> list[str]:
    """Static helper — return our supported canonical symbols.

    A future iteration could call /AssetPairs and filter by USD quote +
    24h volume to auto-discover the universe; for now we keep it static.
    """
    return list(TOP20)
