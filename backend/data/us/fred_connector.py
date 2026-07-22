"""
FRED (Federal Reserve Economic Data) connector.
Free API — just needs a key at fred.stlouisfed.org/docs/api/api_key.html

Key resolution: DB-managed via `services.market_key_service.resolve_key
("fred")` which checks the admin-configurable `market_provider_keys`
table first and falls back to `settings.FRED_API_KEY`. Lets ops save
a key from the AdminPage without env edits.

Resilience: when no FRED key is configured we fall back to yfinance for
the four indicator series that have public Yahoo tickers (10Y yield,
short rate, dollar index, TWD/USD). Series that are economic releases
rather than market quotes (CPI, GDP, Unemployment, the 2Y yield, the
T10Y2Y curve) genuinely require FRED and stay empty.
"""
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

import data.us.yfinance_connector as yfinance

log = logging.getLogger(__name__)

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

# yfinance proxies for the tradable series. `scale` is applied to the
# Yahoo close price to align units with FRED.
#   - ^TNX / ^IRX yields are reported by Yahoo directly as percent
#     (4.25 = 4.25%), so scale=1.
#   - Dollar index and FX cross are spot quotes, scale=1.
# 13-week T-bill (^IRX) is used as a Fed-Funds proxy: it tracks the
# short rate within a few basis points and works without an API key.
_YF_FALLBACK: dict[str, tuple[str, float]] = {
    "DGS10":    ("^TNX",       1.0),
    "FEDFUNDS": ("^IRX",       1.0),
    "DTWEXBGS": ("DX-Y.NYB",   1.0),
    "DEXTW":    ("TWDUSD=X",   1.0),
}


def _bars_to_observations(bars: list[dict], scale: float) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for b in bars:
        ts = b.get("time")
        close = b.get("close")
        if ts is None or close is None:
            continue
        date_str = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        out.append({"date": date_str, "value": float(close) * scale})
    return out


async def _yfinance_fallback(
    series_id: str, *, end_date: str | None = None,
) -> list[dict[str, Any]]:
    """yfinance proxy for a FRED series, clamped at `end_date`.

    `end_date` matters for backtests. Yahoo has no observation-window
    parameter that matches FRED's `observation_end`, so the clamp is
    applied here after the fetch. Without it this function silently
    returned today's quote for a historical anchor: `get_series`
    takes this branch whenever no FRED key is configured, and that is
    the default deployment. A backtest anchored at 2026-04-24 was
    reading July macro — a real look-ahead leak, observed in a replay
    context whose `macro.*.latest_date` read `2026-07-01`.
    """
    mapping = _YF_FALLBACK.get(series_id)
    if mapping is None:
        return []
    ticker, scale = mapping
    try:
        bars = await yfinance.get_history(ticker, period="5y", interval="1mo")
    except Exception as exc:
        log.warning("fred.yfinance_fallback_failed",
                    extra={"series": series_id, "ticker": ticker, "error": str(exc)})
        return []
    obs = _bars_to_observations(bars, scale)
    if end_date:
        # ISO dates compare correctly as strings; both sides are
        # YYYY-MM-DD (`_bars_to_observations` formats them that way).
        obs = [o for o in obs if o["date"] <= end_date]
    return obs


# Keyless public CSV endpoint — no API key, no rate-limit sign-up.
# Used by the FinMind-clone macro self-crawl (fred source) which must
# work on any deployment without the operator provisioning a FRED key.
_FREDGRAPH_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"


async def get_series_csv(
    series_id: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch one FRED series via the KEYLESS fredgraph CSV endpoint.

    Returns ``[{date: ISO, value: float}, ...]``, skipping FRED's ``.``
    missing-observation marker (holidays / no-print days). Empty list on
    any HTTP / parse failure — the macro self-crawl treats that as
    "skip this series this run" rather than erroring the whole chunk.

    Unlike :func:`get_series` this needs no API key, so it's the path
    the self-crawl connectors use for cutover-off-FinMind."""
    import csv
    import io

    params: dict[str, Any] = {"id": series_id}
    if start_date:
        params["cosd"] = start_date
    if end_date:
        params["coed"] = end_date
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.get(_FREDGRAPH_CSV, params=params)
            r.raise_for_status()
    except Exception as exc:
        log.warning("fred.csv.fetch_failed", extra={"series": series_id, "error": str(exc)})
        return []

    out: list[dict[str, Any]] = []
    reader = csv.reader(io.StringIO(r.text))
    rows = list(reader)
    if len(rows) < 2:
        return []
    # Header: observation_date,<SERIES_ID>. Value column is index 1.
    for line in rows[1:]:
        if len(line) < 2:
            continue
        raw_date, raw_val = line[0].strip(), line[1].strip()
        if not raw_date or raw_val in ("", "."):
            continue
        try:
            value = float(raw_val)
        except ValueError:
            continue
        out.append({"date": raw_date, "value": value})
    return out


async def get_series(series_id: str, start_date: str | None = None, end_date: str | None = None) -> list[dict[str, Any]]:
    from services.market_key_service import resolve_key
    api_key = await resolve_key("fred")
    if not api_key:
        return await _yfinance_fallback(series_id, end_date=end_date)
    params: dict = {
        "series_id": series_id,
        "api_key": api_key,
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
