"""Phase B MOPS connector — monthly revenue + quarterly statements.

MOPS (公開資訊觀測站) is the official data source for TW listed-company
financials. Wrapping it here unblocks the FinMind subscription cutover
for the per-symbol financial datasets — each Phase A → B switch is a
single PATCH to `dataset_sources.active_source='mops'`.

Wired today:
  - TaiwanStockMonthRevenue            (月營收, t05st10_ifrs)
  - TaiwanStockFinancialStatements     (綜合損益表, t164sb04_ifrs)
  - TaiwanStockBalanceSheet            (資產負債表, t164sb03_ifrs)
  - TaiwanStockCashFlowsStatement      (現金流量表, t164sb05_ifrs)

Still on FinMind:
  - TaiwanStockDividend (除權息) — needs a separate MOPS endpoint
    that's not yet wrapped.

Cadence + history caveats:
  - MOPS API serves "recent" data — ~24 months for monthly revenue,
    typically ~5 years for quarterly statements (depends on company).
    Operators wanting deep history should run the FinMind backfill
    once before the subscription expires, then let MOPS handle
    ongoing increments after.
  - Quarterly statements are filed in Mar/May/Aug/Nov — calling
    MOPS for a quarter that hasn't been published yet returns
    `code=406` ("查無相符資料"), which the connector treats as
    "no data" (empty list, no error).
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

log = logging.getLogger("finmind.selfcrawl.mops")


def _quarters_in_range(start: date, end: date) -> list[tuple[int, int]]:
    """Enumerate every (year, quarter) tuple whose statement cutoff
    date (Q1 = Mar 31, Q2 = Jun 30, Q3 = Sep 30, Q4 = Dec 31) falls
    in [start, end]. Used to fan a runner-supplied date range into
    individual MOPS quarterly fetches.

    Stays inclusive on both ends so a `start=2024-01-01,
    end=2024-12-31` window picks up Q1-Q4 2024 (4 fetches).
    """
    out: list[tuple[int, int]] = []
    cur_year = start.year
    cur_q = (start.month - 1) // 3 + 1
    while True:
        # Statement cutoff for (cur_year, cur_q)
        month = cur_q * 3
        day = 31 if month in (3, 12) else 30
        cutoff = date(cur_year, month, day)
        if cutoff > end:
            break
        if cutoff >= start:
            out.append((cur_year, cur_q))
        # Advance to next quarter
        cur_q += 1
        if cur_q > 4:
            cur_q = 1
            cur_year += 1
        # Safety brake — should never trip in practice.
        if cur_year > end.year + 1:
            break
    return out


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


async def _fetch_quarterly_statement(
    symbol: str | None, start: date, end: date,
    *, fetcher,
) -> list[dict[str, Any]]:
    """Generic quarterly fetcher — fans the [start, end] date range
    out into individual (year, quarter) tuples and concatenates the
    per-quarter MOPS responses.

    Each fetcher (`get_income_statement` / `get_balance_sheet` /
    `get_cash_flow_statement`) takes `(symbol, year, quarter)` and
    returns FinMind-shaped rows directly, so the wrapper is just
    quarter enumeration + per-quarter error swallowing.

    Raises `ValueError` when called market-wide (symbol=None) — MOPS
    is per-symbol-only, mirrors the monthly-revenue wrapper.
    """
    if not symbol:
        raise ValueError(
            "MOPS quarterly statements require a per-symbol call — "
            "fan out across symbols before calling fetch()"
        )
    out: list[dict[str, Any]] = []
    for year, quarter in _quarters_in_range(start, end):
        try:
            rows = await fetcher(symbol, year, quarter)
        except Exception as exc:
            log.warning(
                "mops.statement.per_quarter_failure",
                extra={"symbol": symbol, "year": year, "quarter": quarter,
                       "error": str(exc)},
            )
            continue
        out.extend(rows)
    return out


async def _fetch_income_statement(
    symbol: str | None, start: date, end: date,
) -> list[dict[str, Any]]:
    """TaiwanStockFinancialStatements — 綜合損益表 via MOPS
    `t164sb04_ifrs`."""
    from data.tw.mops_connector import get_income_statement
    return await _fetch_quarterly_statement(
        symbol, start, end, fetcher=get_income_statement,
    )


async def _fetch_balance_sheet(
    symbol: str | None, start: date, end: date,
) -> list[dict[str, Any]]:
    """TaiwanStockBalanceSheet — 資產負債表 via MOPS `t164sb03_ifrs`."""
    from data.tw.mops_connector import get_balance_sheet
    return await _fetch_quarterly_statement(
        symbol, start, end, fetcher=get_balance_sheet,
    )


async def _fetch_cash_flow_statement(
    symbol: str | None, start: date, end: date,
) -> list[dict[str, Any]]:
    """TaiwanStockCashFlowsStatement — 現金流量表 via MOPS
    `t164sb05_ifrs`."""
    from data.tw.mops_connector import get_cash_flow_statement
    return await _fetch_quarterly_statement(
        symbol, start, end, fetcher=get_cash_flow_statement,
    )


_DISPATCH = {
    "TaiwanStockMonthRevenue":          _fetch_monthly_revenue,
    # Phase 2A — quarterly statements
    "TaiwanStockFinancialStatements":   _fetch_income_statement,
    "TaiwanStockBalanceSheet":          _fetch_balance_sheet,
    "TaiwanStockCashFlowsStatement":    _fetch_cash_flow_statement,
}


def supported_datasets() -> list[str]:
    """Datasets the MOPS wrapper currently handles."""
    return sorted(_DISPATCH.keys())


class MopsClient:
    """Implements `SourceClient` via the existing
    `data.tw.mops_connector`. Wraps the new SPA JSON API for the
    headline financial datasets — see module docstring for the list."""

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
