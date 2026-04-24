"""
FRED (Federal Reserve Economic Data) connector.
Free API — just needs a key at fred.stlouisfed.org/docs/api/api_key.html
"""
import httpx
from typing import Any
from config import settings

_BASE = "https://api.stlouisfed.org/fred"

# Common macro series IDs
SERIES = {
    "fed_funds_rate": "FEDFUNDS",
    "unemployment": "UNRATE",
    "cpi": "CPIAUCSL",
    "gdp": "GDP",
    "10y_yield": "DGS10",
    "2y_yield": "DGS2",
    "10y_minus_2y": "T10Y2Y",
    "usd_index": "DTWEXBGS",
    "twd_usd": "DEXTW",        # TWD/USD — used for portfolio FX
}


async def get_series(series_id: str, start_date: str | None = None, end_date: str | None = None) -> list[dict[str, Any]]:
    if not settings.FRED_API_KEY:
        return []
    params: dict = {
        "series_id": series_id,
        "api_key": settings.FRED_API_KEY,
        "file_type": "json",
        "sort_order": "asc",
        "limit": 1000,
    }
    if start_date:
        params["observation_start"] = start_date
    if end_date:
        params["observation_end"] = end_date

    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.get(f"{_BASE}/series/observations", params=params)
        r.raise_for_status()

    return [
        {"date": obs["date"], "value": float(obs["value"]) if obs["value"] != "." else None}
        for obs in r.json().get("observations", [])
    ]


async def get_latest(series_id: str) -> float | None:
    rows = await get_series(series_id)
    for row in reversed(rows):
        if row["value"] is not None:
            return row["value"]
    return None
