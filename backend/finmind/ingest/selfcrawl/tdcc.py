"""Phase B TDCC connector — 集保戶股權分散表 (share-ownership
distribution) self-crawl.

Wraps `data.tw.tdcc_connector` into the `SourceClient` shape. Market-
wide: TDCC serves one open-data CSV covering every stock's weekly
bracket distribution, so the handler ignores `symbol` and the scheduler
routes this as a single market-wide chunk (see
`dispatcher._MARKET_WIDE_SOURCES`) despite the FinMind per_symbol flag.

Datasets handled:
  - TaiwanStockHoldingSharesPer   weekly 15-bracket holder/share dist.

Pre-cutover: reconcile with FinMind via `dry_run_cutover --values` —
TDCC exposes only the current week, so a historical value diff must use
overlapping recent weeks.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from data.tw import tdcc_connector as _tdcc


async def _fetch_holding_shares_per(
    symbol: str | None, start_date: date, end_date: date,
) -> list[dict[str, Any]]:
    # Market-wide — `symbol` ignored (one CSV covers every stock).
    return await _tdcc.get_holding_shares_per(start_date, end_date)


_DISPATCH = {
    "TaiwanStockHoldingSharesPer": _fetch_holding_shares_per,
}


def supported_datasets() -> list[str]:
    return sorted(_DISPATCH.keys())


class TdccClient:
    """Implements `SourceClient` via `data.tw.tdcc_connector`."""

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
                f"TdccClient has no handler for {dataset_code}. "
                f"Add a `_fetch_*` + downloader in data.tw.tdcc_connector, "
                f"then register in finmind/ingest/selfcrawl/tdcc.py:_DISPATCH."
            )
        return await handler(symbol, start_date, end_date)
