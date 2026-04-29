"""Crypto market service — Kraken-backed quote / history / screener.

Mirrors `services/us_market_service.py` minus:
  - waterfall (single source: Kraken)
  - market-hours throttle (crypto trades 24/7)
  - fundamentals / options / news (Kraken doesn't expose these)

Cache TTLs are tighter than equities because crypto moves faster:
  quote   = 10s
  history = 5min
  screener = 60s
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from cache.redis_cache import cache_get, cache_set, key_history, key_quote
from data.crypto.kraken_connector import get_history as _k_history
from data.crypto.kraken_connector import get_quote as _k_quote
from data.crypto.symbols import TOP20

log = logging.getLogger(__name__)

TTL_QUOTE = 10
TTL_HISTORY = 5 * 60
TTL_SCREENER = 60


async def get_quote(symbol: str, *, bypass_cache: bool = False) -> dict[str, Any]:
    """Spot quote for one canonical crypto symbol. Hits Redis cache
    first unless `bypass_cache` is set — caller forces a fresh Kraken
    REST fetch when the user opens a view that promises live prices."""
    sym = symbol.upper()
    key = key_quote("crypto", sym)
    if not bypass_cache:
        cached = await cache_get(key)
        if cached:
            return json.loads(cached)

    quote = await _k_quote(sym)
    if quote is None:
        # Unknown / unsupported symbol — return a stub so the API endpoint
        # gives a clean 404 path rather than 500.
        return {"symbol": sym, "market": "CRYPTO", "price": None, "change_pct": None,
                "currency": "USD", "error": "symbol not supported on Kraken",
                "data_source": "unavailable"}

    quote["data_source"] = "kraken"
    await cache_set(key, json.dumps(quote), TTL_QUOTE)
    return quote


async def get_history(
    symbol: str, interval: str = "1d", limit: int = 365,
) -> list[dict[str, Any]]:
    sym = symbol.upper()
    key = key_history("crypto", sym, interval)
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)

    bars = await _k_history(sym, interval=interval, limit=limit)
    await cache_set(key, json.dumps(bars), TTL_HISTORY)
    return bars


async def get_screener(limit: int = 20) -> list[dict[str, Any]]:
    """Return Top 20 with their latest quote — small enough to fan out."""
    key = "screener:crypto:top20"
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)[:limit]

    quotes = await asyncio.gather(*(get_quote(s) for s in TOP20))
    rows = [
        {
            "symbol": q["symbol"],
            "market": "CRYPTO",
            "name": q["symbol"],
            "price": q.get("price") or 0,
            "change_pct": q.get("change_pct") or 0,
            "volume": q.get("volume") or 0,
            "high_24h": q.get("high_24h"),
            "low_24h": q.get("low_24h"),
            "data_source": "kraken",
        }
        for q in quotes if q.get("price") is not None
    ]
    rows.sort(key=lambda r: r["volume"], reverse=True)
    # Don't cache when every Kraken call failed — otherwise a transient
    # outage locks the user into 60 s of an empty grid.
    if rows:
        await cache_set(key, json.dumps(rows), TTL_SCREENER)
    else:
        log.warning("crypto.screener.all_quotes_failed")
    return rows[:limit]


async def search(q: str, limit: int = 10) -> list[dict[str, Any]]:
    """Substring match against the static Top 20 universe."""
    needle = q.upper().strip()
    if not needle:
        return []
    matches = [s for s in TOP20 if needle in s][:limit]
    return [{"symbol": s, "market": "CRYPTO", "name": s} for s in matches]


# ── News ──────────────────────────────────────────────────────────

TTL_NEWS = 5 * 60


async def _google_news_rss(query: str, limit: int) -> list[dict[str, Any]]:
    """Google News RSS in en-US — same shape as the TW/US helpers."""
    import httpx
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime

    params = {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as c:
        r = await c.get("https://news.google.com/rss/search", params=params)
        r.raise_for_status()
        xml_text = r.text

    root = ET.fromstring(xml_text)
    items: list[dict[str, Any]] = []
    for el in root.findall(".//item")[:limit]:
        title = (el.findtext("title") or "").strip()
        link = (el.findtext("link") or "").strip()
        pub_date_raw = el.findtext("pubDate") or ""
        source_el = el.find("source")
        publisher = source_el.text.strip() if (source_el is not None and source_el.text) else ""
        try:
            published_at = parsedate_to_datetime(pub_date_raw).isoformat()
        except (TypeError, ValueError):
            published_at = pub_date_raw
        if not title or not link:
            continue
        items.append({
            "title":        title,
            "publisher":    publisher,
            "link":         link,
            "published_at": published_at,
            "thumbnail":    None,
        })
    return items


async def get_news(symbol: str, limit: int = 10) -> list[dict[str, Any]]:
    """
    Crypto headlines via Google News RSS (en-US). Query is `{symbol}
    {full name} crypto` — full name pulled from the curated NAMES map
    so a search for `AVAX` becomes `AVAX Avalanche crypto` and surfaces
    coverage from CoinDesk / The Block / Decrypt / Cointelegraph as
    well as mainstream financial outlets.
    """
    sym = symbol.upper()
    key = f"crypto:news:{sym}"
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)

    from data.crypto.symbols import NAMES
    name = NAMES.get(sym, "")
    query = f"{sym} {name} crypto".strip() if name else f"{sym} cryptocurrency"

    try:
        items = await _google_news_rss(query, limit=limit)
    except Exception as exc:
        log.warning("crypto.news.rss_failed", extra={"symbol": sym, "error": str(exc)})
        items = []

    if items:
        await cache_set(key, json.dumps(items), TTL_NEWS)
    return items
