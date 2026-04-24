"""
Polygon.io REST connector.
All public methods return raw normalized dicts — no caching here.
Raises httpx.HTTPStatusError on 4xx/5xx.
"""
import httpx
from typing import Any
from config import settings

_BASE = "https://api.polygon.io"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=_BASE,
        params={"apiKey": settings.POLYGON_API_KEY},
        timeout=10.0,
    )


async def get_quote(ticker: str) -> dict[str, Any]:
    """Snapshot — last trade + day aggregate for one ticker."""
    async with _client() as c:
        r = await c.get(f"/v2/snapshot/locale/us/markets/stocks/tickers/{ticker.upper()}")
        r.raise_for_status()
        data = r.json()
    snap = data.get("ticker", {})
    day = snap.get("day", {})
    prev = snap.get("prevDay", {})
    last = snap.get("lastTrade", {})
    price = last.get("p") or day.get("c") or prev.get("c") or 0
    prev_close = prev.get("c") or 0
    change = round(price - prev_close, 4) if prev_close else 0
    change_pct = round(change / prev_close * 100, 4) if prev_close else 0
    return {
        "symbol": ticker.upper(),
        "price": price,
        "change": change,
        "change_pct": change_pct,
        "volume": day.get("v") or 0,
        "open": day.get("o"),
        "high": day.get("h"),
        "low": day.get("l"),
        "prev_close": prev_close,
        "market_cap": snap.get("details", {}).get("market_cap"),
        "ts": snap.get("updated"),
    }


async def get_aggs(
    ticker: str,
    from_date: str,
    to_date: str,
    timespan: str = "day",
    multiplier: int = 1,
    adjusted: bool = True,
) -> list[dict[str, Any]]:
    """OHLCV bars. timespan: minute | hour | day | week | month."""
    async with _client() as c:
        r = await c.get(
            f"/v2/aggs/ticker/{ticker.upper()}/range/{multiplier}/{timespan}/{from_date}/{to_date}",
            params={"adjusted": str(adjusted).lower(), "sort": "asc", "limit": 5000},
        )
        r.raise_for_status()
        data = r.json()
    return [
        {
            "time": bar["t"],   # Unix ms UTC
            "open": bar["o"],
            "high": bar["h"],
            "low": bar["l"],
            "close": bar["c"],
            "volume": bar["v"],
        }
        for bar in data.get("results", [])
    ]


async def get_ticker_details(ticker: str) -> dict[str, Any]:
    async with _client() as c:
        r = await c.get(f"/v3/reference/tickers/{ticker.upper()}")
        r.raise_for_status()
    return r.json().get("results", {})


async def get_financials(ticker: str) -> dict[str, Any]:
    """Latest annual + quarterly financials."""
    async with _client() as c:
        r = await c.get(
            "/vX/reference/financials",
            params={"ticker": ticker.upper(), "limit": 5, "sort": "filing_date"},
        )
        r.raise_for_status()
    return r.json().get("results", [])


async def get_options_chain(ticker: str, expiration_date: str | None = None) -> list[dict[str, Any]]:
    params: dict = {"underlying_ticker": ticker.upper(), "limit": 250, "sort": "expiration_date"}
    if expiration_date:
        params["expiration_date"] = expiration_date
    async with _client() as c:
        r = await c.get("/v3/reference/options/contracts", params=params)
        r.raise_for_status()
    return r.json().get("results", [])


async def get_snapshot_all(tickers: list[str] | None = None) -> list[dict[str, Any]]:
    """Bulk snapshot for screener. Optionally filter by tickers list."""
    params: dict = {"include_otc": "false"}
    if tickers:
        params["tickers"] = ",".join(t.upper() for t in tickers)
    async with _client() as c:
        r = await c.get("/v2/snapshot/locale/us/markets/stocks/tickers", params=params)
        r.raise_for_status()
    return r.json().get("tickers", [])
