"""
TW market background polling tasks.
Polls TWSE every 60 seconds during market hours (09:00-13:30 CST).
"""
import asyncio
import json
import logging
from datetime import UTC, datetime

from api.websocket.manager import get_global_subscribed, publish_update
from cache.redis_cache import cache_set, key_quote
from services.ingest.repository import (
    QuoteSnapshotRow,
    insert_quote_snapshot_autosession,
)
from services.tw_market_service import (
    TTL_QUOTE,
    _is_tw_market_open,
    _normalize_quote,
    fetch_quote_waterfall,
    refresh_symbol_map,
)

logger = logging.getLogger(__name__)


async def _subscribed_tw_symbols() -> set[str]:
    """Global (all-worker) TW subscription union — see the US variant."""
    return await get_global_subscribed("TW")


async def refresh_tw_quotes(*, force: bool = False) -> None:
    """
    Polling job — called every 60 seconds during market hours.

    `force=True` bypasses the market-hours gate; used by the EOD
    refresh task (`refresh_tw_quotes_eod`) which fires once at 16:30
    Asia/Taipei (3 h after close) so the cached quotes reflect the
    final EOD prices instead of whatever was last seen at 13:30.

    Single poller under the compose topology (scheduler process);
    reads the global subscription registry — see refresh_us_quotes.
    """
    if not force and not _is_tw_market_open():
        return

    symbols = await _subscribed_tw_symbols()
    if not symbols:
        return

    async def _fetch_and_publish(sym: str) -> None:
        try:
            # Use the shared waterfall (TWSE realtime → FinMind 7-day) — without
            # this the polling task only tried TWSE and gave up, freezing
            # every subscriber's live price whenever TWSE OpenAPI hiccupped.
            raw, source = await fetch_quote_waterfall(sym)
            if not raw:
                return
            result = _normalize_quote(sym, raw)
            result["data_source"] = source

            await cache_set(key_quote("tw", sym), json.dumps(result), TTL_QUOTE)

            # Persist to the quote_snapshots archive so the read path can
            # serve a recent last-price during upstream outages, and the
            # screener / analytics can query historical ticks. Best-effort:
            # DB write failures must not block the WS publish below.
            await insert_quote_snapshot_autosession(QuoteSnapshotRow(
                market="TW",
                symbol=sym,
                ts=datetime.now(UTC),
                last_price=result.get("price"),
                change_pct=result.get("change_pct"),
                prev_close=raw.get("prev_close") if raw else None,
                volume=result.get("volume"),
                source=source,
            ))

            await publish_update(sym, "TW", {
                "price":       result["price"],
                "change":      result["change"],
                "change_pct":  result["change_pct"],
                "volume":      result["volume"],
                "ts":          result["ts"],
                "tz":          "Asia/Taipei",
                "data_source": source,
            })

            from db.session import AsyncSessionLocal
            from services.alert_service import AlertService
            async with AsyncSessionLocal() as db:
                await AlertService.check_and_fire(db, sym, "TW", result["price"])
        except Exception as exc:
            logger.warning("TW quote refresh failed for %s: %s", sym, exc)

    # TWSE rate limit: 1 req/sec via semaphore in connector, so run sequentially
    for sym in symbols:
        await _fetch_and_publish(sym)
        # Small extra delay between symbols to be safe
        await asyncio.sleep(0.2)


async def refresh_tw_quotes_eod() -> None:
    """End-of-day refresh — called once at 16:30 Asia/Taipei (3 h
    after TW close). Bypasses the market-hours gate that the live
    polling job relies on, so the cached quote payload finally
    reflects the post-close settled price instead of the last live
    tick at 13:30. Without this, watchlist views opened at 17:00
    would otherwise show 3.5 h-old data — fine while the live
    polling is active, stale once the scheduler stops calling.
    """
    await refresh_tw_quotes(force=True)


async def refresh_tw_symbol_map() -> None:
    """Refreshes the in-process TWSE/TPEx symbol→exchange map once daily."""
    try:
        await refresh_symbol_map()
    except Exception as exc:
        logger.warning("TW symbol map refresh failed: %s", exc)
