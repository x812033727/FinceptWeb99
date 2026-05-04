"""Tests for `finmind.ingest.selfcrawl.twse` — the wired Phase B
TWSE client.

Mocks the underlying `data.tw.twse_connector` functions so tests run
without network / TWSE rate limiting. Covers:

  - Per-dataset key translation (TWSE shape → FinMind shape)
  - Date range chunking (monthly for price, daily for inst/margin)
  - In-process symbol filtering for market-wide TWSE endpoints
  - End-to-end ingest via active_source='twse' writes the same local
    rows as the FinMind path would — proving the Phase A→B switch
    is truly transparent to the read API."""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text

from finmind.ingest.runner import ingest_chunk
from finmind.ingest.selfcrawl import resolve_client
from finmind.ingest.selfcrawl.twse import (
    TwseClient,
    _days,
    _month_starts,
    supported_datasets,
)
from finmind.models.dataset_source import DatasetSource
from finmind.scripts.init_db import seed_dataset_sources


# ── Date-iterator helpers ───────────────────────────────────────


def test_month_starts_walks_first_of_each_month():
    out = _month_starts(date(2024, 1, 15), date(2024, 4, 5))
    assert out == [
        date(2024, 1, 1),
        date(2024, 2, 1),
        date(2024, 3, 1),
        date(2024, 4, 1),
    ]


def test_month_starts_handles_year_boundary():
    out = _month_starts(date(2024, 11, 15), date(2025, 2, 5))
    assert out == [
        date(2024, 11, 1),
        date(2024, 12, 1),
        date(2025, 1, 1),
        date(2025, 2, 1),
    ]


def test_days_inclusive_both_ends():
    out = _days(date(2024, 1, 1), date(2024, 1, 3))
    assert out == [date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3)]


# ── Connector wired into selfcrawl.resolve_client ───────────────


def test_resolve_client_twse_returns_real_twse_client():
    """`twse` must resolve to the wired TwseClient, not the
    NotWiredYet stub."""
    client = resolve_client("twse")
    assert isinstance(client, TwseClient)


def test_supported_datasets_pins_phase_b_set():
    """Pin the contract — datasets we promise Phase B coverage for.
    If a future commit drops one, this test should fail so the
    breakage is intentional."""
    expected = {
        "TaiwanStockPrice",
        "TaiwanStockInstitutionalInvestorsBuySell",
        "TaiwanStockMarginPurchaseShortSale",
        "TaiwanStockInfo",
        "TaiwanStockPER",
    }
    assert expected.issubset(set(supported_datasets()))


# ── Per-dataset key translation ─────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_price_translates_twse_shape_to_finmind_shape(monkeypatch):
    """TWSE returns rows with `time / open / high / low / close / volume`.
    The wrapper rewrites them to FinMind's `date / stock_id / open / max
    / min / close / Trading_Volume` so the existing column_map applies."""
    captured_calls: list[tuple] = []

    async def fake_get_daily_ohlcv(symbol, query_date):
        captured_calls.append((symbol, query_date))
        return [
            {"time": "2024-01-02", "open": 600.0, "high": 605.0,
             "low": 598.0, "close": 603.0, "volume": 12345},
            {"time": "2024-01-03", "open": 603.0, "high": 608.0,
             "low": 602.0, "close": 606.0, "volume": 23456},
            # Out-of-window row — should be filtered.
            {"time": "2024-02-15", "open": 700.0, "high": 705.0,
             "low": 698.0, "close": 703.0, "volume": 99999},
        ]

    monkeypatch.setattr(
        "data.tw.twse_connector.get_daily_ohlcv", fake_get_daily_ohlcv
    )

    client = TwseClient()
    rows = await client.fetch(
        "TaiwanStockPrice", "2330", date(2024, 1, 1), date(2024, 1, 31)
    )

    # Only the in-window rows survive.
    assert len(rows) == 2
    assert rows[0]["date"] == "2024-01-02"
    assert rows[0]["stock_id"] == "2330"
    assert rows[0]["open"] == 600.0
    assert rows[0]["max"] == 605.0
    assert rows[0]["min"] == 598.0
    assert rows[0]["Trading_Volume"] == 12345

    # Called once per month — the test range is 1 month so 1 call.
    assert len(captured_calls) == 1
    assert captured_calls[0][0] == "2330"


@pytest.mark.asyncio
async def test_fetch_price_iterates_months_across_range(monkeypatch):
    """Range Jan-Mar → 3 monthly TWSE calls."""
    call_dates: list = []

    async def fake_get_daily_ohlcv(symbol, query_date):
        call_dates.append(query_date)
        return []

    monkeypatch.setattr(
        "data.tw.twse_connector.get_daily_ohlcv", fake_get_daily_ohlcv
    )

    client = TwseClient()
    await client.fetch(
        "TaiwanStockPrice", "2330", date(2024, 1, 15), date(2024, 3, 5)
    )

    assert call_dates == [
        date(2024, 1, 1),
        date(2024, 2, 1),
        date(2024, 3, 1),
    ]


@pytest.mark.asyncio
async def test_fetch_price_requires_symbol():
    """Per-symbol design — refuse market-wide call so the operator
    sees the requirement loudly instead of an empty result."""
    client = TwseClient()
    with pytest.raises(ValueError, match="per-symbol call"):
        await client.fetch(
            "TaiwanStockPrice", None, date(2024, 1, 1), date(2024, 1, 31)
        )


@pytest.mark.asyncio
async def test_fetch_institutional_filters_to_requested_symbol(monkeypatch):
    """TWSE returns ALL symbols per day — the wrapper filters in-process
    when a specific symbol is requested. Cost is 1 HTTP per day either way."""
    async def fake_get_institutional(query_date):
        return [
            {"symbol": "2330", "name_zh": "台積電",
             "fini_buy": 100, "fini_sell": 50,
             "sitc_buy": 10, "sitc_sell": 5,
             "dealer_buy": 8, "dealer_sell": 3},
            {"symbol": "2317", "name_zh": "鴻海",
             "fini_buy": 80, "fini_sell": 60,
             "sitc_buy": 5, "sitc_sell": 5,
             "dealer_buy": 2, "dealer_sell": 1},
        ]

    monkeypatch.setattr(
        "data.tw.twse_connector.get_institutional", fake_get_institutional
    )

    client = TwseClient()
    rows = await client.fetch(
        "TaiwanStockInstitutionalInvestorsBuySell",
        "2330",
        date(2024, 1, 2),
        date(2024, 1, 2),
    )

    # Filtered to 2330 only.
    assert len(rows) == 1
    r = rows[0]
    assert r["stock_id"] == "2330"
    assert r["Foreign_Investor_Buy"] == 100
    assert r["Foreign_Investor_Sell"] == 50
    assert r["Investment_Trust_Buy"] == 10
    assert r["Dealer_Buy"] == 8


@pytest.mark.asyncio
async def test_fetch_institutional_returns_all_when_no_symbol(monkeypatch):
    """Market-wide call (symbol=None) returns every symbol's row."""
    async def fake_get_institutional(query_date):
        return [
            {"symbol": "2330", "name_zh": "台積電", "fini_buy": 100,
             "fini_sell": 50, "sitc_buy": 0, "sitc_sell": 0,
             "dealer_buy": 0, "dealer_sell": 0},
            {"symbol": "2317", "name_zh": "鴻海", "fini_buy": 80,
             "fini_sell": 60, "sitc_buy": 0, "sitc_sell": 0,
             "dealer_buy": 0, "dealer_sell": 0},
        ]

    monkeypatch.setattr(
        "data.tw.twse_connector.get_institutional", fake_get_institutional
    )

    client = TwseClient()
    rows = await client.fetch(
        "TaiwanStockInstitutionalInvestorsBuySell",
        None,
        date(2024, 1, 2),
        date(2024, 1, 2),
    )

    assert len(rows) == 2
    assert {r["stock_id"] for r in rows} == {"2330", "2317"}


@pytest.mark.asyncio
async def test_fetch_margin_omits_columns_twse_doesnt_expose(monkeypatch):
    """TWSE's MI_MARGN exposes 4 columns; FinMind 6. Verify the
    wrapper omits the missing two cleanly so column_map produces NULL
    for them in the local row."""
    async def fake_get_margin(query_date):
        return [
            {"symbol": "2330", "name_zh": "台積電",
             "margin_purchase": 5000, "margin_balance": 100000,
             "short_sale": 1000, "short_balance": 5000},
        ]

    monkeypatch.setattr(
        "data.tw.twse_connector.get_margin", fake_get_margin
    )

    client = TwseClient()
    rows = await client.fetch(
        "TaiwanStockMarginPurchaseShortSale", "2330",
        date(2024, 1, 2), date(2024, 1, 2),
    )

    assert len(rows) == 1
    r = rows[0]
    assert r["MarginPurchaseBuy"] == 5000
    assert r["MarginPurchaseTodayBalance"] == 100000
    assert r["ShortSaleSell"] == 1000
    assert r["ShortSaleTodayBalance"] == 5000
    # The missing columns should NOT appear (drops out of the FinMind
    # column_map; runner UPSERTs only the columns present, leaving
    # the local table's MarginPurchaseSell / ShortSaleBuy NULL).
    assert "MarginPurchaseSell" not in r
    assert "ShortSaleBuy" not in r


@pytest.mark.asyncio
async def test_fetch_unknown_dataset_raises_with_clear_message():
    client = TwseClient()
    with pytest.raises(NotImplementedError) as exc:
        await client.fetch(
            "TaiwanFutOptDailyInfo", None,
            date(2024, 1, 1), date(2024, 1, 31),
        )
    msg = str(exc.value)
    assert "TaiwanFutOptDailyInfo" in msg
    assert "TwseClient" in msg


@pytest.mark.asyncio
async def test_fetch_stock_info_translates_twse_master_data(monkeypatch):
    """t187ap03_L returns symbol/name_zh/industry; the wrapper
    translates to FinMind's stock_id/stock_name/industry_category/type
    so the existing column_map projects onto tw_stock_info."""
    async def fake_get_listed_company_industries():
        return [
            {"symbol": "2330", "name_zh": "台積電", "industry": "半導體業"},
            {"symbol": "2317", "name_zh": "鴻海", "industry": "其他電子業"},
        ]

    monkeypatch.setattr(
        "data.tw.twse_connector.get_listed_company_industries",
        fake_get_listed_company_industries,
    )

    client = TwseClient()
    rows = await client.fetch(
        "TaiwanStockInfo", None,
        date(2024, 1, 1), date(2024, 1, 31),
    )

    assert len(rows) == 2
    by_id = {r["stock_id"]: r for r in rows}
    assert by_id["2330"]["stock_name"] == "台積電"
    assert by_id["2330"]["industry_category"] == "半導體業"
    assert by_id["2330"]["type"] == "twse"
    # listing date isn't exposed by t187ap03_L → None (mapping handles).
    assert by_id["2330"]["date"] is None


@pytest.mark.asyncio
async def test_fetch_stock_info_filters_by_symbol(monkeypatch):
    async def fake_get_listed_company_industries():
        return [
            {"symbol": "2330", "name_zh": "台積電", "industry": "半導體業"},
            {"symbol": "2317", "name_zh": "鴻海", "industry": "其他電子業"},
        ]

    monkeypatch.setattr(
        "data.tw.twse_connector.get_listed_company_industries",
        fake_get_listed_company_industries,
    )

    client = TwseClient()
    rows = await client.fetch(
        "TaiwanStockInfo", "2330",
        date(2024, 1, 1), date(2024, 1, 31),
    )
    assert len(rows) == 1
    assert rows[0]["stock_id"] == "2330"


@pytest.mark.asyncio
async def test_fetch_per_translates_bwibbu_cross_section(monkeypatch):
    """BWIBBU_ALL returns {symbol: {pe_ratio, pb_ratio, dividend_yield}}.
    Wrapper flattens into one row per symbol with FinMind's PER/PBR/
    dividend_yield column names + today's date stamped onto each row."""
    async def fake_get_all_valuation_ratios():
        return {
            "2330": {"pe_ratio": 20.5, "pb_ratio": 5.1, "dividend_yield": 1.5},
            "2317": {"pe_ratio": 12.0, "pb_ratio": 1.8, "dividend_yield": 4.2},
        }

    monkeypatch.setattr(
        "data.tw.twse_connector.get_all_valuation_ratios",
        fake_get_all_valuation_ratios,
    )

    client = TwseClient()
    rows = await client.fetch(
        "TaiwanStockPER", None,
        date(2024, 1, 1), date(2024, 1, 31),
    )

    assert len(rows) == 2
    by_id = {r["stock_id"]: r for r in rows}
    assert by_id["2330"]["PER"] == 20.5
    assert by_id["2330"]["PBR"] == 5.1
    assert by_id["2330"]["dividend_yield"] == 1.5
    # All rows stamped with today (BWIBBU_ALL is a same-day snapshot).
    assert by_id["2330"]["date"] == date.today().isoformat()
    assert by_id["2317"]["date"] == date.today().isoformat()


@pytest.mark.asyncio
async def test_fetch_per_filters_by_symbol(monkeypatch):
    async def fake_get_all_valuation_ratios():
        return {
            "2330": {"pe_ratio": 20.5, "pb_ratio": 5.1, "dividend_yield": 1.5},
            "2317": {"pe_ratio": 12.0, "pb_ratio": 1.8, "dividend_yield": 4.2},
        }

    monkeypatch.setattr(
        "data.tw.twse_connector.get_all_valuation_ratios",
        fake_get_all_valuation_ratios,
    )

    client = TwseClient()
    rows = await client.fetch(
        "TaiwanStockPER", "2330",
        date(2024, 1, 1), date(2024, 1, 31),
    )
    assert len(rows) == 1
    assert rows[0]["stock_id"] == "2330"


# ── End-to-end: runner via active_source='twse' ─────────────────


@pytest.mark.asyncio
async def test_e2e_ingest_via_active_source_twse(finmind_session, monkeypatch):
    """Full Phase B headline test — flip active_source on the dataset
    row, run ingest_chunk WITHOUT supplying a client, verify rows land
    in the same local table that the FinMind path would have written
    to. This is the behavior contract that makes the FinMind sub
    expiry a non-event for the read API."""
    await seed_dataset_sources()

    # Operator action: flip Phase A → B for this one dataset.
    row = await finmind_session.get(DatasetSource, "TaiwanStockPrice")
    row.active_source = "twse"
    await finmind_session.commit()

    async def fake_get_daily_ohlcv(symbol, query_date):
        return [
            {"time": "2024-01-02", "open": 600.0, "high": 605.0,
             "low": 598.0, "close": 603.0, "volume": 12345},
            {"time": "2024-01-03", "open": 603.0, "high": 608.0,
             "low": 602.0, "close": 606.0, "volume": 23456},
        ]

    monkeypatch.setattr(
        "data.tw.twse_connector.get_daily_ohlcv", fake_get_daily_ohlcv
    )

    # No client= — runner picks via resolve_client(active_source='twse')
    # → wired TwseClient → mocked get_daily_ohlcv.
    result = await ingest_chunk(
        finmind_session,
        dataset_code="TaiwanStockPrice",
        symbol="2330",
        range_start=date(2024, 1, 1),
        range_end=date(2024, 1, 31),
    )

    assert result.status == "done"
    assert result.rows_written == 2

    # Same local table, same shape — read side has no idea the source
    # changed.
    rows = (
        await finmind_session.execute(
            text(
                "SELECT symbol, ts, close FROM ohlcv_daily ORDER BY ts"
            )
        )
    ).all()
    assert len(rows) == 2
    assert rows[0][0] == "2330"
    assert str(rows[0][1]) == "2024-01-02"
    assert float(rows[0][2]) == 603.0
