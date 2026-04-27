"""
Daily ETF yield pre-compute. Runs after market close (23:30 Asia/Taipei =
15:30 UTC) and once at startup if the cache is empty.

TWSE's BWIBBU_ALL endpoint that powers the screener's PE/PB/yield
filters covers individual stocks only — every TW ETF (00xxx) returns
None for dividend_yield, which silently filtered the entire ETF universe
out of the "高息 ETF" preset. This job rebuilds the missing data:

  1. Pull all TW prices in one TWSE call (already cached upstream)
  2. Pull each ETF's dividend history from FinMind (~250 calls, 42% of
     the daily 600-quota — dedicated to this once-daily refresh)
  3. TTM yield = sum(last-12-months cash dividends) / latest close × 100
  4. Persist the {symbol: yield_pct} dict in Redis for 24h

The screener's `_get_all_valuations_cached` merges this dict into the
BWIBBU result so ETF yields are filterable identically to stock yields.
"""
import asyncio
import json
import logging
from datetime import date, timedelta

from cache.redis_cache import cache_get, cache_set
import data.tw.twse_connector as twse
import data.tw.finmind_connector as finmind

logger = logging.getLogger(__name__)

ETF_YIELDS_KEY = "tw:etf_yields_all"
ETF_YIELDS_LAST_KEY = "tw:etf_yields_all:last_known"
ETF_YIELDS_TTL = 24 * 3600
ETF_YIELDS_LAST_TTL = 30 * 86400   # survives FinMind quota outages


def _sum_recent_cash(divs: list[dict], cutoff: str) -> float:
    """Sum cash-dividend components for rows whose date is on or after cutoff."""
    total = 0.0
    for r in divs:
        d = r.get("date") or r.get("CashExDividendTradingDate") or ""
        if not d or d < cutoff:
            continue
        try:
            total += float(r.get("CashEarningsDistribution") or 0)
            total += float(r.get("CashStatutorySurplus") or 0)
        except (TypeError, ValueError):
            continue
    return total


async def refresh_tw_etf_yields(skip_if_cached: bool = False) -> None:
    """Daily job. `skip_if_cached=True` is used on startup to avoid burning
    FinMind quota on every server restart when the cache is already warm."""
    if skip_if_cached:
        cached = await cache_get(ETF_YIELDS_KEY)
        if cached:
            return

    from services.tw_market_service import is_etf, _exchange_map

    etf_codes = [s for s in _exchange_map if is_etf(s)]
    if not etf_codes:
        logger.warning("ETF yield refresh: _exchange_map empty, skipping")
        return

    try:
        all_stocks = await twse.get_all_twse_symbols()
    except Exception as exc:
        logger.warning("ETF yield refresh: STOCK_DAY_ALL fetch failed: %s", exc)
        return

    prices: dict[str, float | None] = {}
    for s in all_stocks:
        code = (s.get("Code") or s.get("證券代號") or "").strip()
        if code:
            prices[code] = twse._tw_num(s.get("ClosingPrice") or s.get("收盤價"))

    cutoff = (date.today() - timedelta(days=365)).isoformat()
    start_date = (date.today() - timedelta(days=400)).isoformat()

    yields: dict[str, float] = {}
    for sym in etf_codes:
        price = prices.get(sym)
        if not price or price <= 0:
            continue
        try:
            divs = await finmind.get_dividends(sym, start_date=start_date)
        except Exception as exc:
            logger.debug("ETF yield refresh: FinMind failed for %s: %s", sym, exc)
            continue
        if not divs:
            continue
        cash = _sum_recent_cash(divs, cutoff)
        if cash > 0:
            yields[sym] = round(cash / price * 100, 2)

    if yields:
        payload = json.dumps(yields)
        await cache_set(ETF_YIELDS_KEY, payload, ETF_YIELDS_TTL)
        await cache_set(ETF_YIELDS_LAST_KEY, payload, ETF_YIELDS_LAST_TTL)
        logger.info("ETF yield refresh: wrote yields for %d ETFs", len(yields))
    else:
        # Don't overwrite; leave previous fresh cache (24h) and last-known
        # (30d) intact so the screener keeps filtering ETFs through a
        # transient FinMind / TWSE outage.
        logger.warning("ETF yield refresh: no yields computed; keeping existing cache")


async def warmup_tw_etf_yields() -> None:
    """Fire-and-forget startup hook. Refreshes only if cache is cold."""
    try:
        await refresh_tw_etf_yields(skip_if_cached=True)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("ETF yield startup warmup failed: %s", exc)
