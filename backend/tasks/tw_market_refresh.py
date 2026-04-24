"""
TW market background polling tasks.
Polls TWSE every 60 seconds during market hours (09:00-13:30 CST).
"""
import asyncio
import json
from datetime import datetime, timezone

from api.websocket.manager import publish_update, _subscriptions
from cache.redis_cache import cache_set, key_quote
from services.tw_market_service import (
    _is_tw_market_open, _normalize_quote, TTL_QUOTE, refresh_symbol_map,
)
import data.tw.twse_connector as twse


def _subscribed_tw_symbols() -> set[str]:
    symbols: set[str] = set()
    for subs in _subscriptions.values():
        for key in subs:
            parts = key.split(":", 1)
            if len(parts) == 2 and parts[1] == "TW":
                symbols.add(parts[0])
    return symbols


async def refresh_tw_quotes() -> None:
    """
    Polling job — called every 60 seconds.
    TWSE data is ~3-5 min delayed so 60s polling is sufficient.
    Skips entirely outside market hours.
    """
    if not _is_tw_market_open():
        return

    symbols = _subscribed_tw_symbols()
    if not symbols:
        return

    async def _fetch_and_publish(sym: str) -> None:
        try:
            raw = await twse.get_realtime_quote(sym)
            if not raw:
                return
            result = _normalize_quote(sym, raw)

            await cache_set(key_quote("tw", sym), json.dumps(result), TTL_QUOTE)
            await publish_update(sym, "TW", {
                "price":      result["price"],
                "change":     result["change"],
                "change_pct": result["change_pct"],
                "volume":     result["volume"],
                "ts":         result["ts"],
                "tz":         "Asia/Taipei",
            })
        except Exception:
            pass

    # TWSE rate limit: 1 req/sec via semaphore in connector, so run sequentially
    for sym in symbols:
        await _fetch_and_publish(sym)
        # Small extra delay between symbols to be safe
        await asyncio.sleep(0.2)


async def refresh_tw_symbol_map() -> None:
    """Refreshes the in-process TWSE/TPEx symbol→exchange map once daily."""
    try:
        await refresh_symbol_map()
    except Exception:
        pass
