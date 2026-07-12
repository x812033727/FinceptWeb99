"""Phase B FRED connector — US macro series (bond yields, crude oil)
self-crawl for the FinMind clone.

Wraps `data.us.fred_connector.get_series_csv` (the KEYLESS fredgraph CSV
path) into the `SourceClient` shape. Both datasets are market-wide —
one fetch per underlying series, then merged into FinMind's raw column
shape so the existing `GovernmentBondsYield` / `CrudeOilPrices` mappings
project them onto us_bond_yield / commodity_price unchanged.

Datasets handled:
  - GovernmentBondsYield   US Treasury constant-maturity yields by tenor
  - CrudeOilPrices         WTI + Brent spot

Both emit a `name` label (tenor / commodity) that the mapping projects
onto the pk. These labels are FinceptWeb's own convention — reconcile
with FinMind via `dry_run_cutover --values` before flipping
`active_source` to 'fred' in production.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from data.us import fred_connector as _fred

# FRED constant-maturity Treasury series → tenor label. The label
# matches FinMind's verbose `name` convention (see the us_bond_yield
# migration note) so self-crawled rows are PK-compatible with any Phase
# A history already in the table.
_TREASURY_SERIES: dict[str, str] = {
    "DGS1MO": "United States 1-Month",
    "DGS3MO": "United States 3-Month",
    "DGS6MO": "United States 6-Month",
    "DGS1": "United States 1-Year",
    "DGS2": "United States 2-Year",
    "DGS5": "United States 5-Year",
    "DGS10": "United States 10-Year",
    "DGS30": "United States 30-Year",
}

# FRED spot crude series → commodity label.
_OIL_SERIES: dict[str, str] = {
    "DCOILWTICO": "WTI",
    "DCOILBRENTEU": "Brent",
}


def _iso(d: date) -> str:
    return d.isoformat()


async def _fetch_bond_yields(
    symbol: str | None, start_date: date, end_date: date,
) -> list[dict[str, Any]]:
    """FinMind GovernmentBondsYield raw shape: {date, name, value}. One
    FRED series per tenor, concatenated."""
    out: list[dict[str, Any]] = []
    for series_id, tenor in _TREASURY_SERIES.items():
        rows = await _fred.get_series_csv(series_id, _iso(start_date), _iso(end_date))
        for r in rows:
            out.append({"date": r["date"], "name": tenor, "value": r["value"]})
    return out


async def _fetch_crude_oil(
    symbol: str | None, start_date: date, end_date: date,
) -> list[dict[str, Any]]:
    """FinMind CrudeOilPrices raw shape: {date, name, price}."""
    out: list[dict[str, Any]] = []
    for series_id, commodity in _OIL_SERIES.items():
        rows = await _fred.get_series_csv(series_id, _iso(start_date), _iso(end_date))
        for r in rows:
            out.append({"date": r["date"], "name": commodity, "price": r["value"]})
    return out


_DISPATCH = {
    "GovernmentBondsYield": _fetch_bond_yields,
    "CrudeOilPrices": _fetch_crude_oil,
}


def supported_datasets() -> list[str]:
    return sorted(_DISPATCH.keys())


class FredClient:
    """Implements `SourceClient` via `data.us.fred_connector`."""

    async def fetch(
        self,
        dataset_code: str,
        symbol: str | None,
        start_date: date,
        end_date: date,
    ) -> list[dict[str, Any]]:
        handler = _DISPATCH.get(dataset_code)
        if handler is None:
            raise NotImplementedError(
                f"FredClient has no handler for {dataset_code}. "
                f"Add a `_fetch_*` and register in "
                f"finmind/ingest/selfcrawl/fred.py:_DISPATCH."
            )
        return await handler(symbol, start_date, end_date)
