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
    """Live option-chain snapshot normalized to the shared chain shape.

    The old reference-contracts endpoint only exposes contract metadata;
    it cannot power IV/OI analytics. Polygon/Massive's documented chain
    snapshot includes details, quotes/trades, session volume, greeks, IV,
    OI and the underlying price. Cap pagination at 1,000 contracts so a
    request remains bounded; the analysis endpoint additionally limits the
    displayed expiry window.
    """
    params: dict = {"limit": 250}
    if expiration_date:
        params["expiration_date"] = expiration_date
    raw: list[dict[str, Any]] = []
    next_url: str | None = None
    async with _client() as c:
        path: str = f"/v3/snapshot/options/{ticker.upper()}"
        for _ in range(4):
            r = await c.get(path, params=params)
            r.raise_for_status()
            payload = r.json()
            raw.extend(payload.get("results") or [])
            next_url = payload.get("next_url")
            if not next_url or len(raw) >= 1000:
                break
            path = next_url
            params = {}

    out: list[dict[str, Any]] = []
    for row in raw[:1000]:
        details = row.get("details") or {}
        quote = row.get("last_quote") or {}
        trade = row.get("last_trade") or {}
        session = row.get("session") or {}
        greeks = row.get("greeks") or {}
        underlying = row.get("underlying_asset") or {}
        out.append({
            "ticker": row.get("ticker"),
            "underlying_ticker": underlying.get("ticker") or ticker.upper(),
            "underlying_price": underlying.get("price"),
            "contract_type": details.get("contract_type"),
            "expiration_date": details.get("expiration_date"),
            "strike_price": details.get("strike_price"),
            "last_price": trade.get("price") or session.get("close"),
            "bid": quote.get("bid"),
            "ask": quote.get("ask"),
            "volume": session.get("volume"),
            "open_interest": row.get("open_interest"),
            "implied_volatility": row.get("implied_volatility"),
            "delta": greeks.get("delta"),
            "gamma": greeks.get("gamma"),
            "theta": greeks.get("theta"),
            "vega": greeks.get("vega"),
            "break_even_price": row.get("break_even_price"),
            "chain_truncated": bool(next_url),
        })
    return out


async def get_snapshot_all(tickers: list[str] | None = None) -> list[dict[str, Any]]:
    """Bulk snapshot for screener. Optionally filter by tickers list."""
    params: dict = {"include_otc": "false"}
    if tickers:
        params["tickers"] = ",".join(t.upper() for t in tickers)
    async with _client() as c:
        r = await c.get("/v2/snapshot/locale/us/markets/stocks/tickers", params=params)
        r.raise_for_status()
    return r.json().get("tickers", [])
