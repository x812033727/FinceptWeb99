"""Phase B MOPS connector — monthly revenue (only).

MOPS (公開資訊觀測站) is the official data source for TW listed-company
financials. Wrapping it here unblocks the FinMind subscription cutover
for `TaiwanStockMonthRevenue` — the dataset's Phase A → B switch is
now a single PATCH to `dataset_sources.active_source='mops'`.

What's NOT here yet (and would need NEW scrapers in
`data.tw.mops_connector`, not just a wrapper):
  - TaiwanStockFinancialStatements (損益表)
  - TaiwanStockBalanceSheet (資產負債表)
  - TaiwanStockCashFlowsStatement (現金流量表)
  - TaiwanStockDividend (除權息)

The existing `data.tw.mops_connector` only exposes
`get_monthly_revenue_recent`. Adding the quarterly-statement scrapers
is a separate substantial piece of work (HTML parsing + pagination
across years) — deferred.

Cadence note: MOPS only serves "recent" (~24 months) by default. For
deep-history backfill on the MOPS path, the operator should keep
`active_source='finmind'` until the Phase A subscription truly expires,
then accept that pre-2-years-ago revenue rows won't backfill from
MOPS. Daily-cron incremental updates work fine through MOPS.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

log = logging.getLogger("finmind.selfcrawl.mops")


async def _fetch_monthly_revenue(
    symbol: str | None, start: date, end: date
) -> list[dict[str, Any]]:
    """TaiwanStockMonthRevenue — per-symbol via MOPS.

    MOPS doesn't accept date-range params; it returns "recent N months"
    in one call. The wrapper filters in-process to [start, end] so the
    runner sees a consistent contract regardless of source.
    """
    if not symbol:
        # Same constraint as TaiwanStockPrice on TWSE — MOPS is a
        # per-symbol API, no market-wide endpoint. Surface loudly so
        # the operator knows to fan out across `tw_stock_info`.
        raise ValueError(
            "TaiwanStockMonthRevenue via MOPS requires a per-symbol "
            "call — fan out across symbols before calling fetch()"
        )

    from data.tw.mops_connector import get_monthly_revenue_recent

    rows = await get_monthly_revenue_recent(symbol)
    out: list[dict[str, Any]] = []
    for r in rows:
        d_str = r.get("date")
        if not d_str:
            continue
        try:
            d_obj = date.fromisoformat(str(d_str)[:10])
        except ValueError:
            continue
        if d_obj < start or d_obj > end:
            continue
        # mops_connector already returns FinMind-shaped keys
        # (symbol/date/revenue/revenue_yoy/revenue_mom) — translate
        # to the column names the existing TaiwanStockMonthRevenue
        # mapping expects (date, stock_id, revenue, revenue_year,
        # revenue_month — see _row_revenue in mappings.py).
        out.append({
            "date": d_obj.isoformat(),
            "stock_id": symbol,
            "revenue": r.get("revenue"),
            "revenue_year": r.get("revenue_yoy"),
            "revenue_month": r.get("revenue_mom"),
        })
    return out


_DISPATCH = {
    "TaiwanStockMonthRevenue": _fetch_monthly_revenue,
}


def supported_datasets() -> list[str]:
    """Datasets the MOPS wrapper currently handles."""
    return sorted(_DISPATCH.keys())


class MopsClient:
    """Implements `SourceClient` via the existing
    `data.tw.mops_connector`. Only `TaiwanStockMonthRevenue` for now —
    quarterly statements stay on the FinMind path until someone
    writes the MOPS HTML scrapers."""

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
                f"MopsClient has no handler for {dataset_code}. "
                f"Add a `_fetch_{dataset_code.lower()}` and register "
                f"in finmind/ingest/selfcrawl/mops.py:_DISPATCH, OR "
                f"keep active_source='finmind' for this dataset."
            )
        return await handler(symbol, start_date, end_date)
