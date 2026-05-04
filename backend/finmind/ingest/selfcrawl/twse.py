"""Phase B TWSE connector — replaces FinMind for the 3 most-used TW
datasets.

Wraps the existing, battle-tested `data.tw.twse_connector` (used by
the main app's market-data waterfall) into the `SourceClient` shape
the FinMind clone runner expects. The wrapper's job is just key
translation: TWSE's response columns → the FinMind column names that
each `DatasetMapping.column_map` already knows how to project onto
local-table columns.

That contract means **no mapping changes needed** for Phase B — flip
`dataset_sources.active_source` from 'finmind' to 'twse', and the
existing column_map + row_transform pipeline does the rest.

Datasets handled:

  - TaiwanStockPrice
       TWSE returns one symbol × one calendar month per request, so
       the wrapper iterates months across the requested date range
       and concatenates.
  - TaiwanStockInstitutionalInvestorsBuySell
       TWSE returns all symbols × one calendar day per request. The
       wrapper iterates days, optionally filters to the requested
       symbol, and concatenates.
  - TaiwanStockMarginPurchaseShortSale
       Same shape as institutional. Note: TWSE doesn't expose the
       MarginPurchaseSell / ShortSaleBuy columns that FinMind does;
       those land as None in the row, the column_map drops them,
       runner inherits NULL into the local row.
  - TaiwanStockInfo
       TWSE 上市公司基本資料 (t187ap03_L). One-shot snapshot — no
       date axis on the upstream, start/end ignored. Covers TWSE-
       listed equities only (TPEx needs a sibling endpoint).
  - TaiwanStockPER
       TWSE BWIBBU_ALL — today's PER / PBR / 殖利率 cross-section
       in one call. No historical depth, so deep backfill stays on
       FinMind; Phase B coverage here is for daily-cron freshness.
  - TaiwanStockTotalReturnIndex
       TWSE FMTQIK (price-index close) — NOT the dividend-reinvested
       TR index FinMind returns. Phase B fallback is "good enough
       for daily-cron freshness"; operators wanting the actual TR
       series should keep active_source='finmind' for this one.

Deferred (need NEW functions in `data.tw.twse_connector` before they
can be wrapped):
  - TaiwanStockShareholding (MI_QFIIS endpoint)
  - TaiwanStockDayTrading (TWTB4U endpoint)
  - TaiwanStockTotalMarginPurchaseShortSale (MI_MARGN_SUMMARY)
  - TaiwanStockTotalInstitutionalInvestors (BFI82U)
  - TaiwanStockMarketValueWeight (FMSRFK)
Each is mechanical (wrap an HTTP call + Big5/JSON parse) but needs
real-environment validation against TWSE's actual response shape
before the handler is trustworthy.

Caveats:
  - TWSE rate limit (`asyncio.Semaphore(1)` + 1.1s delay per request,
    enforced inside twse_connector) means a wide date range is slow.
    The runner is single-threaded by design (see backfill.py docstring)
    so this is fine — operators backfill per-symbol or per-day chunks.
  - TWSE has no equivalent of FinMind's per-symbol margin endpoint
    when querying for ONE symbol — it always returns market-wide.
    The wrapper filters in-process; cost is one HTTP per day regardless
    of how many symbols are needed.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

log = logging.getLogger("finmind.selfcrawl.twse")


def _month_starts(start: date, end: date) -> list[date]:
    """Yield the first day of every calendar month covered by
    [start, end]. TWSE's STOCK_DAY endpoint returns the entire
    month containing the supplied `date` parameter, so iterating
    by month is the natural chunking."""
    out: list[date] = []
    cur = start.replace(day=1)
    while cur <= end:
        out.append(cur)
        # Bump to next month's first day.
        if cur.month == 12:
            cur = cur.replace(year=cur.year + 1, month=1)
        else:
            cur = cur.replace(month=cur.month + 1)
    return out


def _days(start: date, end: date) -> list[date]:
    """Calendar days between start and end inclusive. Trading-day
    filtering is upstream's job — TWSE returns empty lists for
    weekends/holidays, which the wrapper passes through transparently
    (no rows = nothing to UPSERT)."""
    out: list[date] = []
    cur = start
    while cur <= end:
        out.append(cur)
        cur += timedelta(days=1)
    return out


# ── Per-dataset wrappers ─────────────────────────────────────────


async def _fetch_price(
    symbol: str | None, start: date, end: date
) -> list[dict[str, Any]]:
    """TaiwanStockPrice — per-symbol monthly chunks via STOCK_DAY."""
    if not symbol:
        # TWSE doesn't expose a market-wide single-day OHLCV endpoint
        # in a shape compatible with our schema. The runner caller
        # should fan out per symbol from `tw_stock_info` for a true
        # market-wide backfill; refusing here surfaces the requirement
        # loudly instead of silently returning an empty result.
        raise ValueError(
            "TaiwanStockPrice via TWSE requires a per-symbol call — "
            "fan out across symbols before calling fetch()"
        )

    from data.tw.twse_connector import get_daily_ohlcv

    out: list[dict[str, Any]] = []
    for month_start in _month_starts(start, end):
        rows = await get_daily_ohlcv(symbol, month_start)
        for r in rows:
            d = r.get("time")
            if not d:
                continue
            # TWSE returns the full month — clip to the requested window.
            try:
                d_obj = date.fromisoformat(str(d))
            except ValueError:
                continue
            if d_obj < start or d_obj > end:
                continue
            # Translate to FinMind's TaiwanStockPrice column shape so
            # the existing mapping.column_map projects correctly.
            out.append({
                "date": d_obj.isoformat(),
                "stock_id": symbol,
                "open": r.get("open"),
                "max": r.get("high"),
                "min": r.get("low"),
                "close": r.get("close"),
                "Trading_Volume": r.get("volume"),
            })
    return out


async def _fetch_institutional(
    symbol: str | None, start: date, end: date
) -> list[dict[str, Any]]:
    """TaiwanStockInstitutionalInvestorsBuySell — daily chunks via T86."""
    from data.tw.twse_connector import get_institutional

    out: list[dict[str, Any]] = []
    for d in _days(start, end):
        rows = await get_institutional(d)
        for r in rows:
            sym = r.get("symbol")
            if not sym:
                continue
            if symbol and sym != symbol:
                continue
            out.append({
                "date": d.isoformat(),
                "stock_id": sym,
                "Foreign_Investor_Buy": r.get("fini_buy"),
                "Foreign_Investor_Sell": r.get("fini_sell"),
                "Investment_Trust_Buy": r.get("sitc_buy"),
                "Investment_Trust_Sell": r.get("sitc_sell"),
                "Dealer_Buy": r.get("dealer_buy"),
                "Dealer_Sell": r.get("dealer_sell"),
            })
    return out


async def _fetch_margin(
    symbol: str | None, start: date, end: date
) -> list[dict[str, Any]]:
    """TaiwanStockMarginPurchaseShortSale — daily chunks via MI_MARGN."""
    from data.tw.twse_connector import get_margin

    out: list[dict[str, Any]] = []
    for d in _days(start, end):
        rows = await get_margin(d)
        for r in rows:
            sym = r.get("symbol")
            if not sym:
                continue
            if symbol and sym != symbol:
                continue
            # TWSE's MI_MARGN exposes 4 columns; FinMind exposes 6.
            # The two missing (MarginPurchaseSell / ShortSaleBuy) land
            # as omitted-from-row, which the column_map drops cleanly
            # — local table accepts NULL for those columns.
            out.append({
                "date": d.isoformat(),
                "stock_id": sym,
                "MarginPurchaseBuy": r.get("margin_purchase"),
                "MarginPurchaseTodayBalance": r.get("margin_balance"),
                "ShortSaleSell": r.get("short_sale"),
                "ShortSaleTodayBalance": r.get("short_balance"),
            })
    return out


async def _fetch_stock_info(
    symbol: str | None, start: date, end: date
) -> list[dict[str, Any]]:
    """TaiwanStockInfo — TWSE 上市公司基本資料 via t187ap03_L.

    One-shot snapshot (no date axis on the upstream); start/end are
    ignored by the wrapper. The mapping injects market='TWSE' since
    this endpoint only covers TWSE listings (TPEx companies need a
    sibling endpoint that's not yet exposed by twse_connector).

    Output translated to FinMind's TaiwanStockInfo schema
    (stock_id, stock_name, industry_category, type, date) so the
    existing column_map projects onto tw_stock_info."""
    del start, end  # one-shot endpoint
    from data.tw.twse_connector import get_listed_company_industries

    rows = await get_listed_company_industries()
    out: list[dict[str, Any]] = []
    for r in rows:
        sym = r.get("symbol")
        if not sym:
            continue
        if symbol and sym != symbol:
            continue
        out.append({
            "stock_id": sym,
            "stock_name": r.get("name_zh"),
            "industry_category": r.get("industry"),
            "type": "twse",
            # TWSE t187ap03_L doesn't expose listing date; the mapping's
            # _row_stock_info accepts None for listed_at via _to_date(None).
            "date": None,
        })
    return out


async def _fetch_per(
    symbol: str | None, start: date, end: date
) -> list[dict[str, Any]]:
    """TaiwanStockPER — TWSE BWIBBU_ALL gives the latest day's
    cross-section in one call. No historical depth available via this
    endpoint, so backfilling deep history requires FinMind for the
    archive; Phase B coverage here is for ongoing daily-cron freshness."""
    del start, end  # cross-section endpoint, no date range
    from data.tw.twse_connector import get_all_valuation_ratios

    cross_section = await get_all_valuation_ratios()
    today_iso = date.today().isoformat()
    out: list[dict[str, Any]] = []
    for sym, ratios in cross_section.items():
        if symbol and sym != symbol:
            continue
        out.append({
            "date": today_iso,
            "stock_id": sym,
            "PER": ratios.get("pe_ratio"),
            "PBR": ratios.get("pb_ratio"),
            "dividend_yield": ratios.get("dividend_yield"),
        })
    return out


async def _fetch_total_return_index(
    symbol: str | None, start: date, end: date
) -> list[dict[str, Any]]:
    """TaiwanStockTotalReturnIndex — proxied via TWSE FMTQIK.

    Honest caveat: FMTQIK is the price-index close (TAIEX), NOT the
    true total-return index that includes dividends. FinMind's
    TaiwanStockTotalReturnIndex returns the dividend-reinvested
    series. Phase B coverage here is "good enough for daily-cron
    freshness on the price index" — operators wanting the actual
    TR series should keep active_source='finmind' for this dataset.

    The wrapper iterates monthly chunks (FMTQIK returns one month
    per call) and clips to [start, end]. Output shape matches the
    existing TaiwanStockTotalReturnIndex row_transform input
    (date / stock_id / value)."""
    del symbol  # market-wide endpoint, no symbol filter (always TAIEX)
    from data.tw.twse_connector import get_taiex_history

    out: list[dict[str, Any]] = []
    for month_start in _month_starts(start, end):
        rows = await get_taiex_history(month_start)
        for r in rows:
            d = r.get("time")
            if not d:
                continue
            try:
                d_obj = date.fromisoformat(str(d))
            except ValueError:
                continue
            if d_obj < start or d_obj > end:
                continue
            out.append({
                "date": d_obj.isoformat(),
                "stock_id": "TAIEX",
                "value": r.get("close"),
            })
    return out


# ── Dispatch table ──────────────────────────────────────────────

_DISPATCH = {
    "TaiwanStockPrice": _fetch_price,
    "TaiwanStockInstitutionalInvestorsBuySell": _fetch_institutional,
    "TaiwanStockMarginPurchaseShortSale": _fetch_margin,
    "TaiwanStockInfo": _fetch_stock_info,
    "TaiwanStockPER": _fetch_per,
    "TaiwanStockTotalReturnIndex": _fetch_total_return_index,
}


def supported_datasets() -> list[str]:
    """Datasets the TWSE wrapper currently handles. Ops can grep this
    when planning a Phase A → B cutover by `active_source='twse'`."""
    return sorted(_DISPATCH.keys())


class TwseClient:
    """Implements `SourceClient` via the existing twse_connector.

    Routes by dataset_code → handler. Datasets without a handler raise
    NotImplementedError naming both the dataset and the source so an
    operator who flipped active_source prematurely sees exactly which
    handler still needs writing."""

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
                f"TwseClient has no handler for {dataset_code}. "
                f"Add a `_fetch_{dataset_code.lower()}` and register in "
                f"finmind/ingest/selfcrawl/twse.py:_DISPATCH, OR keep "
                f"active_source='finmind' for this dataset."
            )
        return await handler(symbol, start_date, end_date)
