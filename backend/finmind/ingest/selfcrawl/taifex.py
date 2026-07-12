"""Phase B TAIFEX connector — replaces FinMind for the daily futures /
options market reports.

Wraps `data.tw.taifex_connector`'s daily-report downloaders into the
`SourceClient` shape. Unlike the TWSE per-symbol wrappers these are
**market-wide**: one CSV download returns every contract for the date
range, so the handlers ignore `symbol` and the scheduler routes these
datasets as a single market-wide chunk (see
`dispatcher._MARKET_WIDE_SOURCES`) even though their FinMind spec is
per_symbol.

The connector already emits FinMind's raw column names, so the existing
`TaiwanFuturesDaily` / `TaiwanOptionDaily` DatasetMappings project them
onto tw_futures_daily / tw_option_daily with no mapping change.

Datasets handled:
  - TaiwanFuturesDaily                    near-month day futures OHLCV
  - TaiwanOptionDaily                     near-month day option OHLCV
  - TaiwanFuturesInstitutionalInvestors   三大法人 futures OI, pivoted

Deferred (option institutional / large-trader / dealer / settlement):
need the corresponding TAIFEX endpoints wired in
`data.tw.taifex_connector` first.

Pre-cutover: run `dry_run_cutover --values` against a live FinMind token
to confirm the near-month + call_put-label choices line up before
flipping `active_source` to 'taifex'.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from data.tw import taifex_connector as _taifex


async def _fetch_futures_daily(
    symbol: str | None, start_date: date, end_date: date,
) -> list[dict[str, Any]]:
    # Market-wide — `symbol` ignored (one download covers all contracts).
    return await _taifex.get_futures_daily(start_date, end_date)


async def _fetch_option_daily(
    symbol: str | None, start_date: date, end_date: date,
) -> list[dict[str, Any]]:
    return await _taifex.get_option_daily(start_date, end_date)


async def _fetch_futures_institutional(
    symbol: str | None, start_date: date, end_date: date,
) -> list[dict[str, Any]]:
    # Market-wide — one download covers every product; pivoted to the
    # FinMind wide row per contract in the connector.
    return await _taifex.get_futures_institutional(start_date, end_date)


_DISPATCH = {
    "TaiwanFuturesDaily": _fetch_futures_daily,
    "TaiwanOptionDaily": _fetch_option_daily,
    "TaiwanFuturesInstitutionalInvestors": _fetch_futures_institutional,
}


def supported_datasets() -> list[str]:
    """Datasets the TAIFEX wrapper currently handles."""
    return sorted(_DISPATCH.keys())


class TaifexClient:
    """Implements `SourceClient` via `data.tw.taifex_connector`."""

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
                f"TaifexClient has no handler for {dataset_code}. "
                f"Add a `_fetch_*` + underlying downloader in "
                f"data.tw.taifex_connector, then register in "
                f"finmind/ingest/selfcrawl/taifex.py:_DISPATCH."
            )
        return await handler(symbol, start_date, end_date)
