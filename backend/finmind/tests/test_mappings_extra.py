"""Tests for the additional 10 dataset mappings landed after the
Phase-2 headline-5.

Each test pins one mapping's contract: FinMind row schema → expected
local-table row dict, plus an end-to-end ingest verification for the
two highest-traffic ones (PER + futures) so a regression in the
column-name plumbing fails loudly."""
from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import text

from finmind.ingest.mappings import MAPPINGS, find_mapping, transform_row
from finmind.ingest.runner import ingest_chunk
from finmind.scripts.init_db import seed_dataset_sources


# ── Mapping coverage pin ────────────────────────────────────────


def test_followup_mappings_extend_phase2_headline_set():
    """The follow-up batch must keep the Phase-2 headline-5 PLUS the
    10 new datasets — 15 total. If a future commit drops one of these,
    this test should fail so the breakage is intentional."""
    expected = {
        # Phase-2 headline 5
        "TaiwanStockPrice",
        "TaiwanStockMarginPurchaseShortSale",
        "TaiwanStockInstitutionalInvestorsBuySell",
        "TaiwanStockMonthRevenue",
        "TaiwanStockTotalMarginPurchaseShortSale",
        # Follow-up 10
        "TaiwanStockPER",
        "TaiwanStockPriceAdj",
        "TaiwanStockShareholding",
        "TaiwanStockMarketValue",
        "TaiwanStockDividend",
        "TaiwanStockBuyBack",
        "TaiwanStockInfo",
        "TaiwanFuturesDaily",
        "TaiwanOptionDaily",
        "TaiwanStockNews",
    }
    assert expected.issubset(MAPPINGS.keys())


# ── Per-mapping unit tests ──────────────────────────────────────


def test_per_mapping_round_trips():
    fm = {"date": "2024-01-02", "stock_id": "2330", "PER": "20.5",
          "PBR": "5.1", "dividend_yield": "1.5"}
    out = transform_row(fm, find_mapping("TaiwanStockPER"))
    assert out["symbol"] == "2330"
    assert out["ts"] == date(2024, 1, 2)
    assert float(out["per"]) == 20.5
    assert float(out["pbr"]) == 5.1
    assert float(out["dividend_yield"]) == 1.5


def test_shareholding_mapping_handles_finmind_camelcase():
    fm = {
        "date": "2024-01-02",
        "stock_id": "2330",
        "ForeignInvestmentRemainingRatio": "75.42",
        "ForeignInvestmentRemainShares": "19500000000",
        "NumberOfSharesIssued": "25930000000",
    }
    out = transform_row(fm, find_mapping("TaiwanStockShareholding"))
    assert out["symbol"] == "2330"
    assert float(out["foreign_holding_pct"]) == 75.42
    assert out["foreign_holding_shares"] == 19_500_000_000


def test_market_value_mapping():
    fm = {"date": "2024-01-02", "stock_id": "2330",
          "MarketValue": "15000000000000", "shares_issued": "25000000000"}
    out = transform_row(fm, find_mapping("TaiwanStockMarketValue"))
    assert float(out["market_cap"]) == 15_000_000_000_000.0
    assert out["issued_shares"] == 25_000_000_000


def test_dividend_mapping_keeps_announce_date_in_pk():
    """PK is (symbol, announce_date) — verify both are produced."""
    fm = {
        "date": "2024-06-15",
        "stock_id": "2330",
        "CashEarningsDistribution": "12.5",
        "StockEarningsDistribution": "0",
        "CashExDividendTradingDate": "2024-07-15",
    }
    out = transform_row(fm, find_mapping("TaiwanStockDividend"))
    assert out["symbol"] == "2330"
    assert out["announce_date"] == date(2024, 6, 15)
    assert float(out["cash_dividend"]) == 12.5
    assert out["ex_dividend_date"] == date(2024, 7, 15)


def test_futures_daily_mapping_uses_contract_in_pk():
    fm = {
        "date": "2024-01-02",
        "futures_id": "TX",
        "open": "17500", "max": "17600", "min": "17400", "close": "17550",
        "volume": "120000",
        "open_interest": "85000",
        "settlement_price": "17545",
    }
    out = transform_row(fm, find_mapping("TaiwanFuturesDaily"))
    assert out["contract"] == "TX"
    assert out["ts"] == date(2024, 1, 2)
    assert float(out["close"]) == 17550.0
    assert out["open_interest"] == 85000


def test_option_daily_mapping_carries_strike_and_call_put():
    fm = {
        "date": "2024-01-02",
        "option_id": "TXO",
        "strike_price": "17500",
        "call_put": "C",
        "open": "200", "max": "250", "min": "180", "close": "230",
        "volume": "5000",
        "open_interest": "3000",
    }
    out = transform_row(fm, find_mapping("TaiwanOptionDaily"))
    assert out["contract"] == "TXO"
    assert float(out["strike"]) == 17500.0
    assert out["call_put"] == "C"
    assert out["ts"] == date(2024, 1, 2)


def test_stock_info_mapping_normalizes_market():
    """FinMind's `type` field carries 'twse' / 'tpex'; we normalize
    to 'TWSE' / 'OTC'."""
    fm_twse = {
        "stock_id": "2330",
        "stock_name": "台積電",
        "industry_category": "半導體業",
        "type": "twse",
        "date": "1994-09-05",
    }
    out = transform_row(fm_twse, find_mapping("TaiwanStockInfo"))
    assert out["market"] == "TWSE"
    assert out["name_zh"] == "台積電"
    assert out["industry_category"] == "半導體業"
    assert out["listed_at"] == date(1994, 9, 5)

    fm_tpex = {**fm_twse, "type": "tpex"}
    out = transform_row(fm_tpex, find_mapping("TaiwanStockInfo"))
    assert out["market"] == "OTC"


def test_buyback_mapping():
    fm = {
        "date": "2024-03-01",
        "stock_id": "2330",
        "BuyBackStartDate": "2024-03-15",
        "BuyBackEndDate": "2024-05-14",
        "BuyBackPlanQuantity": "10000000",
        "BuyBackActualQuantity": "5000000",
        "BuyBackAveragePrice": "650.5",
    }
    out = transform_row(fm, find_mapping("TaiwanStockBuyBack"))
    assert out["symbol"] == "2330"
    assert out["announced_at"] == date(2024, 3, 1)
    assert out["plan_shares"] == 10_000_000
    assert out["actual_shares"] == 5_000_000
    assert float(out["avg_price"]) == 650.5


def test_news_mapping_computes_sha256():
    fm = {
        "date": "2024-01-02",
        "stock_id": "2330",
        "title": "台積電法說會 Q4 表現亮眼",
        "link": "https://example.com/news/123",
        "description": "summary text",
    }
    out = transform_row(fm, find_mapping("TaiwanStockNews"))
    assert out["sha256"] is not None
    assert len(out["sha256"]) == 64
    assert out["title"].startswith("台積電法說會")
    assert isinstance(out["published_at"], datetime)
    assert out["published_at"].tzinfo is not None  # tz-aware

    # Same title+link → same sha256 (idempotency invariant for
    # cross-source dedup).
    out2 = transform_row(fm, find_mapping("TaiwanStockNews"))
    assert out["sha256"] == out2["sha256"]


def test_news_mapping_handles_missing_published_at():
    fm = {"date": None, "stock_id": "2330", "title": "x", "link": "y"}
    out = transform_row(fm, find_mapping("TaiwanStockNews"))
    assert out["published_at"] is None


def test_price_adj_mapping():
    fm = {"date": "2024-01-02", "stock_id": "2330", "close": "598.5"}
    out = transform_row(fm, find_mapping("TaiwanStockPriceAdj"))
    assert out["symbol"] == "2330"
    assert out["ts"] == date(2024, 1, 2)
    assert float(out["adj_close"]) == 598.5


# ── End-to-end ingest checks ────────────────────────────────────


@pytest.mark.asyncio
async def test_ingest_per_writes_rows(finmind_session):
    """Headline regression test for one of the new mappings."""
    from dataclasses import dataclass

    @dataclass
    class FakeClient:
        rows: list

        async def fetch(self, *args, **kwargs):
            return list(self.rows)

    await seed_dataset_sources()

    fake = FakeClient([
        {"date": "2024-01-02", "stock_id": "2330",
         "PER": "20.5", "PBR": "5.1", "dividend_yield": "1.5"},
        {"date": "2024-01-03", "stock_id": "2330",
         "PER": "20.8", "PBR": "5.2", "dividend_yield": "1.5"},
    ])

    result = await ingest_chunk(
        finmind_session,
        dataset_code="TaiwanStockPER",
        symbol="2330",
        range_start=date(2024, 1, 1),
        range_end=date(2024, 1, 31),
        client=fake,
    )
    assert result.status == "done"
    assert result.rows_written == 2

    rows = (
        await finmind_session.execute(
            text(
                "SELECT symbol, ts, per, pbr, dividend_yield "
                "FROM tw_stock_per_daily ORDER BY ts"
            )
        )
    ).all()
    assert len(rows) == 2
    assert float(rows[0][2]) == 20.5
    assert float(rows[1][2]) == 20.8


@pytest.mark.asyncio
async def test_ingest_news_dedupes_via_sha256(finmind_session):
    """Re-ingesting the same article (same title+link) must NOT create
    a duplicate row — the sha256 unique constraint plus the runner's
    ON CONFLICT means this is a no-op the second time."""
    from dataclasses import dataclass

    @dataclass
    class FakeClient:
        rows: list

        async def fetch(self, *args, **kwargs):
            return list(self.rows)

    await seed_dataset_sources()

    fake = FakeClient([
        {
            "date": "2024-01-02",
            "stock_id": "2330",
            "title": "台積電法說會 Q4 表現亮眼",
            "link": "https://example.com/news/123",
            "description": "summary",
        },
    ])

    for _ in range(2):
        result = await ingest_chunk(
            finmind_session,
            dataset_code="TaiwanStockNews",
            symbol="2330",
            range_start=date(2024, 1, 1),
            range_end=date(2024, 1, 31),
            client=fake,
        )
        assert result.status == "done"

    n = (
        await finmind_session.execute(
            text("SELECT COUNT(*) FROM tw_news_articles")
        )
    ).scalar_one()
    assert n == 1


@pytest.mark.asyncio
async def test_ingest_futures_writes_with_contract_pk(finmind_session):
    """tw_futures_daily PK is (contract, ts), no symbol — verify the
    runner's PK-aware NULL filter doesn't accidentally drop these rows
    just because `symbol` is absent from the schema."""
    from dataclasses import dataclass

    @dataclass
    class FakeClient:
        rows: list

        async def fetch(self, *args, **kwargs):
            return list(self.rows)

    await seed_dataset_sources()

    fake = FakeClient([
        {
            "date": "2024-01-02", "futures_id": "TX",
            "open": "17500", "max": "17600", "min": "17400", "close": "17550",
            "volume": "120000", "open_interest": "85000",
            "settlement_price": "17545",
        },
    ])

    # Note: passing symbol=None because TaiwanFuturesDaily uses
    # `contract` not `symbol` — see DatasetMapping.pk_columns.
    result = await ingest_chunk(
        finmind_session,
        dataset_code="TaiwanFuturesDaily",
        symbol=None,
        range_start=date(2024, 1, 1),
        range_end=date(2024, 1, 31),
        client=fake,
    )
    assert result.status == "done"
    assert result.rows_written == 1

    row = (
        await finmind_session.execute(
            text(
                "SELECT contract, ts, close, open_interest "
                "FROM tw_futures_daily"
            )
        )
    ).one()
    assert row[0] == "TX"
    assert float(row[2]) == 17550.0
    assert row[3] == 85000
