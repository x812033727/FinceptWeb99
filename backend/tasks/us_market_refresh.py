"""
US market background polling tasks.
Fetches quotes for symbols with active WebSocket subscribers,
publishes updates to the Redis Pub/Sub channel.
"""
import asyncio
import json
import logging
from datetime import datetime
import pytz

from api.websocket.manager import publish_update, _subscriptions
from cache.redis_cache import cache_set, key_quote
from config import settings
import data.us.polygon_connector as polygon
import data.us.yfinance_connector as yfinance
from services.us_market_service import _is_market_open, TTL_QUOTE

logger = logging.getLogger(__name__)

_ET = pytz.timezone("America/New_York")

# In-process S&P 500 ticker list (populated by refresh_sp500_universe)
_sp500: list[str] = []


def _subscribed_us_symbols() -> set[str]:
    """Return all US symbols currently subscribed by any WebSocket client."""
    symbols: set[str] = set()
    for subs in _subscriptions.values():
        for key in subs:
            parts = key.split(":", 1)
            if len(parts) == 2 and parts[1] == "US":
                symbols.add(parts[0])
    return symbols


async def refresh_us_quotes() -> None:
    """
    Polling job — called every 10 seconds.
    Only runs during US market hours to avoid unnecessary API calls.
    Falls back to 5-minute interval outside market hours by early-returning
    (APScheduler still calls every 10s but we skip work).
    """
    market_open = _is_market_open()
    symbols = _subscribed_us_symbols()

    if not symbols:
        return

    # Outside market hours: slow down by only refreshing every 5 min
    # We track last-run time in a module-level variable
    if not market_open and not _should_run_offhours():
        return

    use_polygon = bool(settings.POLYGON_API_KEY)

    async def _fetch_and_publish(sym: str) -> None:
        try:
            if use_polygon:
                raw = await polygon.get_quote(sym)
            else:
                raw = await yfinance.get_quote(sym)

            from services.us_market_service import _normalize_quote
            result = _normalize_quote(sym, raw)

            await cache_set(key_quote("us", sym), json.dumps(result), TTL_QUOTE)

            await publish_update(sym, "US", {
                "price":      result["price"],
                "change":     result["change"],
                "change_pct": result["change_pct"],
                "volume":     result["volume"],
                "ts":         result["ts"],
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
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    if _last_offhours_run is None or (now - _last_offhours_run).total_seconds() >= 300:
        _last_offhours_run = now
        return True
    return False


# ── S&P 500 universe ──────────────────────────────────────────────

async def refresh_sp500_universe() -> None:
    """Fetches and caches the S&P 500 ticker list once daily."""
    global _sp500
    import httpx
    import re
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
        tickers = re.findall(r'<td><a[^>]+>([A-Z]{1,5})</a></td>', r.text)
        _sp500 = list(dict.fromkeys(tickers))[:505]
    except Exception:
        pass


def get_sp500() -> list[str]:
    return _sp500
