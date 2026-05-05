"""Tests for the Phase 1A chip-flow batch wired into
`finmind.ingest.selfcrawl.twse`:

  - TaiwanStockShareholding             (MI_QFIIS)
  - TaiwanStockTotalInstitutionalInvestors (BFI82U)
  - TaiwanStockTotalMarginPurchaseShortSale (derived from MI_MARGN sum)
  - TaiwanStockDayTrading               (TWTB4U)

The wrappers translate TWSE column names → FinMind raw key names so
the existing `mappings.column_map` + `row_transform` / `batch_transform`
project unchanged. We mock the underlying `data.tw.twse_connector`
functions so tests run without network / TWSE rate limiting.
"""
from __future__ import annotations

from datetime import date

import pytest

from finmind.ingest.selfcrawl import covers_dataset
from finmind.ingest.selfcrawl.twse import TwseClient, supported_datasets


# ── supported_datasets pin ──────────────────────────────────────


def test_supported_datasets_includes_phase_1a_batch():
    """Pin the Phase 1A handlers in the dispatch table so a future
    refactor doesn't accidentally drop one."""
    s = supported_datasets()
    assert "TaiwanStockShareholding" in s
    assert "TaiwanStockTotalInstitutionalInvestors" in s
    assert "TaiwanStockTotalMarginPurchaseShortSale" in s
    assert "TaiwanStockDayTrading" in s


def test_covers_dataset_predicate_recognises_new_handlers():
    """The selfcrawl-registry view (used by the AdminPage flip
    gatekeeper) must agree with the per-module dispatch table."""
    for code in (
        "TaiwanStockShareholding",
        "TaiwanStockTotalInstitutionalInvestors",
        "TaiwanStockTotalMarginPurchaseShortSale",
        "TaiwanStockDayTrading",
    ):
        assert covers_dataset("twse", code) is True, code


# ── TaiwanStockShareholding (MI_QFIIS) ──────────────────────────


@pytest.mark.asyncio
async def test_fetch_shareholding_translates_to_finmind_shape(monkeypatch):
    """MI_QFIIS rows arrive with `symbol/foreign_pct/foreign_shares/
    issued_shares`; the wrapper rewrites to FinMind's
    `date/stock_id/ForeignInvestmentRemainingRatio/ForeignInvestmentRemain
    Shares/NumberOfSharesIssued` so the existing column_map projects."""
    async def fake_get_foreign_shareholding(query_date):
        return [
            {"symbol": "2330", "name_zh": "台積電",
             "issued_shares": 25_930_000_000,
             "foreign_shares": 19_500_000_000,
             "foreign_pct": 75.21},
            {"symbol": "2317", "name_zh": "鴻海",
             "issued_shares": 13_866_500_000,
             "foreign_shares": 6_240_000_000,
             "foreign_pct": 45.00},
        ]

    monkeypatch.setattr(
        "data.tw.twse_connector.get_foreign_shareholding",
        fake_get_foreign_shareholding,
    )

    client = TwseClient()
    rows = await client.fetch(
        "TaiwanStockShareholding", "2330",
        date(2024, 1, 2), date(2024, 1, 2),
    )

    assert len(rows) == 1
    r = rows[0]
    assert r["date"] == "2024-01-02"
    assert r["stock_id"] == "2330"
    assert r["ForeignInvestmentRemainingRatio"] == 75.21
    assert r["ForeignInvestmentRemainShares"] == 19_500_000_000
    assert r["NumberOfSharesIssued"] == 25_930_000_000


@pytest.mark.asyncio
async def test_fetch_shareholding_returns_all_when_no_symbol(monkeypatch):
    """Market-wide call returns every symbol's row, so a single TWSE
    HTTP per day backfills the whole exchange."""
    async def fake_get_foreign_shareholding(query_date):
        return [
            {"symbol": "2330", "name_zh": "台積電",
             "issued_shares": 1, "foreign_shares": 1, "foreign_pct": 1.0},
            {"symbol": "2317", "name_zh": "鴻海",
             "issued_shares": 2, "foreign_shares": 2, "foreign_pct": 2.0},
        ]

    monkeypatch.setattr(
        "data.tw.twse_connector.get_foreign_shareholding",
        fake_get_foreign_shareholding,
    )

    client = TwseClient()
    rows = await client.fetch(
        "TaiwanStockShareholding", None,
        date(2024, 1, 2), date(2024, 1, 2),
    )

    assert {r["stock_id"] for r in rows} == {"2330", "2317"}


@pytest.mark.asyncio
async def test_fetch_shareholding_iterates_days_and_swallows_errors(monkeypatch):
    """Multi-day window → one HTTP per day. A single-day failure must
    NOT abort the chunk — the wrapper logs and continues so the rest
    of the window still backfills."""
    seen: list = []
    failures = {date(2024, 1, 3)}

    async def fake_get_foreign_shareholding(query_date):
        seen.append(query_date)
        if query_date in failures:
            raise RuntimeError("simulated TWSE 500")
        return [
            {"symbol": "2330", "name_zh": "台積電",
             "issued_shares": 1, "foreign_shares": 1, "foreign_pct": 75.0},
        ]

    monkeypatch.setattr(
        "data.tw.twse_connector.get_foreign_shareholding",
        fake_get_foreign_shareholding,
    )

    client = TwseClient()
    rows = await client.fetch(
        "TaiwanStockShareholding", "2330",
        date(2024, 1, 2), date(2024, 1, 4),
    )

    # 3 days called, 1 errored, 2 succeeded.
    assert len(seen) == 3
    assert len(rows) == 2
    assert {r["date"] for r in rows} == {"2024-01-02", "2024-01-04"}


# ── TaiwanStockTotalInstitutionalInvestors (BFI82U) ─────────────


@pytest.mark.asyncio
async def test_fetch_total_institutional_emits_three_rows_per_date(monkeypatch):
    """BFI82U returns one row per institutional unit (foreign / sitc /
    dealer). The wrapper passes them through unchanged but adds the
    `date` field so the runner's `_pivot_total_institutional`
    batch_transform can group by date."""
    async def fake_get_total_institutional(query_date):
        return [
            {"name": "外資及陸資(不含外資自營商)", "buy": 100_000_000, "sell": 80_000_000},
            {"name": "投信",                       "buy":  20_000_000, "sell": 25_000_000},
            {"name": "自營商",                     "buy":  10_000_000, "sell":  8_000_000},
        ]

    monkeypatch.setattr(
        "data.tw.twse_connector.get_total_institutional",
        fake_get_total_institutional,
    )

    client = TwseClient()
    rows = await client.fetch(
        "TaiwanStockTotalInstitutionalInvestors", None,
        date(2024, 1, 2), date(2024, 1, 2),
    )

    assert len(rows) == 3
    assert all(r["date"] == "2024-01-02" for r in rows)
    names = [r["name"] for r in rows]
    assert "外資及陸資(不含外資自營商)" in names
    assert "投信" in names
    assert "自營商" in names
    # `_pivot_total_institutional` substring-matches against
    # _INST_NAME_TO_COL — the names above must all contain the
    # configured needles so the pivot picks them up.
    foreign = next(r for r in rows if "外資" in r["name"])
    assert foreign["buy"] == 100_000_000
    assert foreign["sell"] == 80_000_000


@pytest.mark.asyncio
async def test_fetch_total_institutional_drops_symbol_argument(monkeypatch):
    """Market-wide endpoint — the symbol arg must NOT cause an
    upstream filter (would always return 0 rows). Passing a symbol
    should silently produce the same output as None."""
    async def fake_get_total_institutional(query_date):
        return [{"name": "外資及陸資", "buy": 1, "sell": 2}]

    monkeypatch.setattr(
        "data.tw.twse_connector.get_total_institutional",
        fake_get_total_institutional,
    )

    client = TwseClient()
    rows_a = await client.fetch(
        "TaiwanStockTotalInstitutionalInvestors", "2330",
        date(2024, 1, 2), date(2024, 1, 2),
    )
    rows_b = await client.fetch(
        "TaiwanStockTotalInstitutionalInvestors", None,
        date(2024, 1, 2), date(2024, 1, 2),
    )
    assert rows_a == rows_b


# ── TaiwanStockTotalMarginPurchaseShortSale (sum of MI_MARGN) ──


@pytest.mark.asyncio
async def test_fetch_total_margin_sums_per_symbol_into_market_total(monkeypatch):
    """The dataset shape is the daily MARKET total; we derive it by
    summing the per-symbol rows from the existing `get_margin` so the
    self-crawl source needs only one HTTP per day. MarginPurchaseSell /
    ShortSaleBuy aren't exposed by MI_MARGN so they pass through as
    None — the column_map drops them and the local row keeps NULL."""
    async def fake_get_margin(query_date):
        return [
            {"symbol": "2330", "name_zh": "台積電",
             "margin_purchase": 1_000, "margin_balance": 50_000,
             "short_sale": 100,         "short_balance": 1_000},
            {"symbol": "2317", "name_zh": "鴻海",
             "margin_purchase": 2_000, "margin_balance": 80_000,
             "short_sale": 200,         "short_balance": 2_000},
        ]

    monkeypatch.setattr(
        "data.tw.twse_connector.get_margin", fake_get_margin,
    )

    client = TwseClient()
    rows = await client.fetch(
        "TaiwanStockTotalMarginPurchaseShortSale", None,
        date(2024, 1, 2), date(2024, 1, 2),
    )

    assert len(rows) == 1
    r = rows[0]
    assert r["date"] == "2024-01-02"
    assert r["MarginPurchaseBuy"] == 3_000
    assert r["MarginPurchaseTodayBalance"] == 130_000
    assert r["ShortSaleSell"] == 300
    assert r["ShortSaleTodayBalance"] == 3_000
    # MI_MARGN doesn't expose these — preserved as None so column_map
    # drops them and local row gets NULL.
    assert r["MarginPurchaseSell"] is None
    assert r["ShortSaleBuy"] is None


@pytest.mark.asyncio
async def test_fetch_total_margin_skips_empty_days(monkeypatch):
    """Weekend / holiday → MI_MARGN returns []. The wrapper must NOT
    emit a zero-row for those days (would pollute the local table
    with a synthetic 0-balance row that never existed)."""
    async def fake_get_margin(query_date):
        # Only return data for Tuesday; Saturday / Sunday → empty.
        if query_date == date(2024, 1, 2):
            return [
                {"symbol": "2330", "name_zh": "台積電",
                 "margin_purchase": 1, "margin_balance": 1,
                 "short_sale": 1, "short_balance": 1},
            ]
        return []

    monkeypatch.setattr(
        "data.tw.twse_connector.get_margin", fake_get_margin,
    )

    client = TwseClient()
    rows = await client.fetch(
        "TaiwanStockTotalMarginPurchaseShortSale", None,
        date(2023, 12, 30), date(2024, 1, 2),
    )

    assert len(rows) == 1
    assert rows[0]["date"] == "2024-01-02"


# ── TaiwanStockDayTrading (TWTB4U) ──────────────────────────────


@pytest.mark.asyncio
async def test_fetch_day_trading_emits_buy_equals_sell_volume(monkeypatch):
    """Day-trade volume is single-valued (every buy paired with a
    same-day sell). Verify the wrapper assigns the same volume to
    BuyAfterSale and SellAfterBuy so FinMind's column_map projects
    both fields, and that the amount split (price × volume differs
    across the day) is genuinely two-sided."""
    async def fake_get_day_trading(query_date):
        return [
            {"symbol": "2330", "name_zh": "台積電",
             "volume": 5_000_000,
             "buy_amount":  3_000_000_000.0,
             "sell_amount": 3_010_000_000.0},
            {"symbol": "2317", "name_zh": "鴻海",
             "volume": 1_000_000,
             "buy_amount":  100_000_000.0,
             "sell_amount": 100_500_000.0},
        ]

    monkeypatch.setattr(
        "data.tw.twse_connector.get_day_trading", fake_get_day_trading,
    )

    client = TwseClient()
    rows = await client.fetch(
        "TaiwanStockDayTrading", "2330",
        date(2024, 1, 2), date(2024, 1, 2),
    )

    assert len(rows) == 1
    r = rows[0]
    assert r["stock_id"] == "2330"
    assert r["BuyAfterSale"] == 5_000_000
    assert r["SellAfterBuy"] == 5_000_000
    assert r["BuyAfterSaleAmount"] == 3_000_000_000.0
    assert r["SellAfterBuyAmount"] == 3_010_000_000.0


@pytest.mark.asyncio
async def test_fetch_day_trading_iterates_days(monkeypatch):
    """Range 4 days → 4 TWSE calls (TWTB4U serves one date per req)."""
    seen: list = []

    async def fake_get_day_trading(query_date):
        seen.append(query_date)
        return []

    monkeypatch.setattr(
        "data.tw.twse_connector.get_day_trading", fake_get_day_trading,
    )

    client = TwseClient()
    await client.fetch(
        "TaiwanStockDayTrading", None,
        date(2024, 1, 2), date(2024, 1, 5),
    )

    assert seen == [
        date(2024, 1, 2), date(2024, 1, 3),
        date(2024, 1, 4), date(2024, 1, 5),
    ]
