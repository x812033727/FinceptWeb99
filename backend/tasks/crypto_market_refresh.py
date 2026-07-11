"""Crypto market background polling task.

Polls Kraken every 30s for symbols that have at least one active WebSocket
subscriber, normalizes, caches, and publishes via the internal pub/sub.
Also fans out to AlertService.check_and_fire so price alerts on crypto
trigger like equities.

Notes:
- No `_is_market_open` check — crypto trades 24/7
- 30s cadence is gentler than US (10s) since (a) we're polling without an
  API key (Kraken free tier rate limit ~1 req/sec) and (b) Phase 3 will
  switch to WebSocket streaming where real-time pushes replace polling
"""
from __future__ import annotations

import asyncio
import json
import logging

from api.websocket.manager import publish_update, _subscriptions
from cache.redis_cache import cache_set, key_quote
from data.crypto.kraken_connector import get_quote as kraken_quote
from services.crypto_market_service import TTL_QUOTE

logger = logging.getLogger(__name__)


def _subscribed_crypto_symbols() -> set[str]:
    """All crypto symbols any client is currently watching."""
    symbols: set[str] = set()
    for subs in _subscriptions.values():
        for key in subs:
            parts = key.split(":", 1)
            if len(parts) == 2 and parts[1] == "CRYPTO":
                symbols.add(parts[0])
    return symbols


async def refresh_crypto_quotes() -> None:
    """Polling job — called every 30s by the APScheduler.

    No-op when no client is subscribed; otherwise fan out to Kraken in
    parallel and publish each result through the existing WS broadcast.

    Deliberately NOT cross-worker locked — symbol set is this worker's
    own `_subscriptions`; see refresh_us_quotes for the full rationale.
    """
    symbols = _subscribed_crypto_symbols()
    if not symbols:
        return

    async def _fetch_and_publish(sym: str) -> None:
        try:
            quote = await kraken_quote(sym)
            if quote is None:
                return

            await cache_set(
                key_quote("crypto", sym), json.dumps(quote), TTL_QUOTE,
            )
            await publish_update(sym, "CRYPTO", {
                "price": quote["price"],
                "change_pct": quote["change_pct"],
                "volume": quote["volume"],
            })

            from db.session import AsyncSessionLocal
            from services.alert_service import AlertService
            async with AsyncSessionLocal() as db:
                await AlertService.check_and_fire(db, sym, "CRYPTO", quote["price"])
        except Exception as exc:
            logger.warning("crypto quote refresh failed for %s: %s", sym, exc)

    await asyncio.gather(*(_fetch_and_publish(s) for s in symbols))
