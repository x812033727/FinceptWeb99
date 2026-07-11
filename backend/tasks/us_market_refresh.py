"""
US market background polling tasks.
Fetches quotes for symbols with active WebSocket subscribers,
publishes updates to the Redis Pub/Sub channel.
"""
import asyncio
import json
import logging
from datetime import datetime

from api.websocket.manager import get_global_subscribed, publish_update
from cache.redis_cache import cache_set, key_quote
from services.us_market_service import (
    TTL_QUOTE,
    _is_market_open,
    _normalize_quote,
    fetch_quote_waterfall,
)

logger = logging.getLogger(__name__)


async def _subscribed_us_symbols() -> set[str]:
    """All US symbols subscribed by any client on ANY worker (Redis
    registry union; falls back to this process's local view when Redis
    is down). Lets a dedicated scheduler process — which holds zero
    WebSocket connections itself — poll for every worker's clients."""
    return await get_global_subscribed("US")


async def refresh_us_quotes() -> None:
    """
    Polling job — called every 10 seconds.
    Only runs during US market hours to avoid unnecessary API calls.
    Falls back to 5-minute interval outside market hours by early-returning
    (APScheduler still calls every 10s but we skip work).

    Runs in the dedicated scheduler process under the compose topology
    (SCHEDULER_ENABLED=false on web workers), so no cross-worker lock
    is needed — there is exactly one poller, and it reads the global
    subscription registry to cover every worker's clients.
    """
    market_open = _is_market_open()
    symbols = await _subscribed_us_symbols()

    if not symbols:
        return

    # Outside market hours: slow down by only refreshing every 5 min
    # We track last-run time in a module-level variable
    if not market_open and not _should_run_offhours():
        return

    async def _fetch_and_publish(sym: str) -> None:
        try:
            # Use the shared waterfall (Polygon → yfinance → Stooq) — without
            # this, a Polygon rate-limit pause froze every subscriber's live
            # price until the upstream recovered. Bypasses the 15 s quote
            # cache because we want fresh ticks on every 10 s poll.
            raw, source = await fetch_quote_waterfall(sym)
            result = _normalize_quote(sym, raw)
            result["data_source"] = source

            await cache_set(key_quote("us", sym), json.dumps(result), TTL_QUOTE)

            await publish_update(sym, "US", {
                "price":       result["price"],
                "change":      result["change"],
                "change_pct":  result["change_pct"],
                "volume":      result["volume"],
                "ts":          result["ts"],
                "data_source": source,
            })

            from db.session import AsyncSessionLocal
            from services.alert_service import AlertService
            async with AsyncSessionLocal() as db:
                await AlertService.check_and_fire(db, sym, "US", result["price"])
        except Exception as exc:
            logger.warning("US quote refresh failed for %s: %s", sym, exc)

    # Process all subscribed symbols concurrently
    await asyncio.gather(*[_fetch_and_publish(sym) for sym in symbols])


# ── Off-hours throttle ────────────────────────────────────────────
_last_offhours_run: datetime | None = None


def _should_run_offhours() -> bool:
    global _last_offhours_run
    from datetime import timezone
    now = datetime.now(timezone.utc)
    if _last_offhours_run is None or (now - _last_offhours_run).total_seconds() >= 300:
        _last_offhours_run = now
        return True
    return False


# ── S&P 500 universe ──────────────────────────────────────────────

async def refresh_sp500_universe() -> None:
    """Force-refresh the S&P 500 ticker cache once daily.

    Populates the same module-level cache that
    `services/us_market_service::_get_sp500_tickers` reads, so the daily
    scheduled work is actually visible to request handlers.
    """
    from data.us.sp500_universe import get_sp500_tickers
    await get_sp500_tickers(force_refresh=True)


# ── Screener cache warm ────────────────────────────────────────────

async def refresh_us_screener() -> None:
    """Pre-warm the default `/api/us/screener?limit=100` cache.

    The fully-fallback path (Polygon → yfinance → Stooq) takes 20+ s
    when Yahoo blocks our cloud IPs and we have to walk the curated
    universe through Stooq's single-stream CSV endpoint. That's longer
    than any reverse-proxy timeout, so user requests would 503 even
    though the data WOULD eventually arrive. Running the full waterfall
    here on a 5-min schedule means request handlers always hit the
    cached result.

    `full_stooq_batch=True` overrides the request-path cap so this task
    fills in the entire universe even when Stooq is the only working
    source. Errors are swallowed — a missed tick just means the next
    user-driven request triggers the (capped) sync fallback.

    Cross-worker dedup: the warm result lands in the shared Redis
    cache, so with N uvicorn workers each running its own scheduler,
    N-1 of the runs are pure upstream waste (the Stooq walk alone is
    ~20 s of it). A lock held for just under the 5-min interval lets
    exactly one worker do the walk per window. No release on purpose —
    expiry IS the schedule.
    """
    from cache.redis_cache import acquire_lock
    from services.us_market_service import get_screener
    if not await acquire_lock("lock:us_screener_warm", ttl_seconds=270):
        return
    try:
        await get_screener(limit=100, full_stooq_batch=True)
    except Exception as exc:
        logger.warning("US screener warm refresh failed: %s", exc)
