"""Tests for the 6 mappings added in Item 4 (依序完成 batch 4):

  - TaiwanStockDividendResult
  - TaiwanStockSplitPrice
  - TaiwanStockDayTrading
  - TaiwanStockSecuritiesLending
  - TaiwanFuturesInstitutionalInvestors          (day session)
  - TaiwanFuturesInstitutionalInvestorsAfterHours (night session)

Each mapping gets a column-rename smoke test plus an end-to-end
ingest_chunk with a fake FinMind row to verify the value lands in
the local table with the right type coercion.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import pytest
from sqlalchemy import text

from finmind.ingest.mappings import MAPPINGS, find_mapping, transform_row
from finmind.ingest.runner import ingest_chunk
from finmind.scripts.init_db import seed_dataset_sources


@dataclass
class FakeClient:
    rows: list[dict[str, Any]]

    async def fetch(self, *args, **kwargs):
        return list(self.rows)


def test_item4_mappings_registered():
    expected = {
        "TaiwanStockDividendResult",
        "TaiwanStockSplitPrice",
        "TaiwanStockDayTrading",
        "TaiwanStockSecuritiesLending",
        "TaiwanFuturesInstitutionalInvestors",
        "TaiwanFuturesInstitutionalInvestorsAfterHours",
    }
    assert expected.issubset(MAPPINGS.keys())


# ── DividendResult ──────────────────────────────────────────────


def test_dividend_result_mapping_round_trips():
    fm = {
        "date": "2024-07-15", "stock_id": "2330",
        "before_price": "650", "after_price": "640",
        "stock_or_cache_dividend_price": "10",
        "stock_dividend_price": "0",
    }
    out = transform_row(fm, find_mapping("TaiwanStockDividendResult"))
    assert out["symbol"] == "2330"
    assert out["ex_date"] == date(2024, 7, 15)
    assert float(out["before_price"]) == 650.0
    assert float(out["after_price"]) == 640.0
    assert float(out["cash_dividend"]) == 10.0


@pytest.mark.asyncio
async def test_ingest_dividend_result_writes_row(finmind_session):
    await seed_dataset_sources()
    fake = FakeClient([{
        "date": "2024-07-15", "stock_id": "2330",
        "before_price": "650", "after_price": "640",
        "stock_or_cache_dividend_price": "10",
        "stock_dividend_price": "0",
    }])
    result = await ingest_chunk(
        finmind_session,
        dataset_code="TaiwanStockDividendResult",
        symbol="2330",
        range_start=date(2024, 1, 1),
        range_end=date(2024, 12, 31),
        client=fake,
    )
    assert result.status == "done"
    assert result.rows_written == 1

    row = (
        await finmind_session.execute(
            text("SELECT symbol, ex_date, after_price FROM tw_dividend_result")
        )
    ).one()
    assert row[0] == "2330"


# ── Split ───────────────────────────────────────────────────────


def test_split_mapping_round_trips():
    fm = {
        "date": "2024-08-15", "stock_id": "9999",
        "before_price": "100", "after_price": "20",
        "split_ratio": "5",
    }
    out = transform_row(fm, find_mapping("TaiwanStockSplitPrice"))
    assert out["symbol"] == "9999"
    assert out["ex_date"] == date(2024, 8, 15)
    assert float(out["split_ratio"]) == 5.0


# ── DayTrading ──────────────────────────────────────────────────


def test_day_trade_mapping_round_trips():
    fm = {
        "date": "2024-01-02", "stock_id": "2330",
        "BuyAfterSale": "12345",
        "SellAfterBuy": "12000",
        "BuyAfterSaleAmount": "7400000",
        "SellAfterBuyAmount": "7200000",
    }
    out = transform_row(fm, find_mapping("TaiwanStockDayTrading"))
    assert out["symbol"] == "2330"
    assert out["ts"] == date(2024, 1, 2)
    assert out["buy_volume"] == 12345
    assert out["sell_volume"] == 12000
    assert float(out["buy_amount"]) == 7_400_000.0


@pytest.mark.asyncio
async def test_ingest_day_trade_writes_row(finmind_session):
    await seed_dataset_sources()
    fake = FakeClient([{
        "date": "2024-01-02", "stock_id": "2330",
        "BuyAfterSale": "12345", "SellAfterBuy": "12000",
        "BuyAfterSaleAmount": "7400000",
        "SellAfterBuyAmount": "7200000",
    }])
    result = await ingest_chunk(
        finmind_session,
        dataset_code="TaiwanStockDayTrading",
        symbol="2330",
        range_start=date(2024, 1, 1),
        range_end=date(2024, 1, 31),
        client=fake,
    )
    assert result.status == "done"
    assert result.rows_written == 1


# ── SecuritiesLending ───────────────────────────────────────────


def test_securities_lending_mapping_defaults_transaction_type():
    """transaction_type is part of the PK — when FinMind doesn't
    populate it, default to '_' so the INSERT doesn't fail on a
    NULL primary-key column."""
    fm = {
        "date": "2024-01-02", "stock_id": "2330",
        "volume": "10000", "fee_rate": "0.5",
    }
    out = transform_row(fm, find_mapping("TaiwanStockSecuritiesLending"))
    assert out["transaction_type"] == "_"
    assert out["volume"] == 10000


def test_securities_lending_mapping_keeps_explicit_type():
    fm = {
        "date": "2024-01-02", "stock_id": "2330",
        "transaction_type": "borrowed",
        "volume": "5000",
    }
    out = transform_row(fm, find_mapping("TaiwanStockSecuritiesLending"))
    assert out["transaction_type"] == "borrowed"


# ── FuturesInstitutionalInvestors (day + night) ─────────────────


def test_futures_inst_day_session_injected():
    """Day-session mapping has extra={'session': 'day'} so the local
    row gets the right discriminator value."""
    fm = {
        "date": "2024-01-02", "futures_id": "TX",
        "long_open_interest_balance_volume_foreign_investment": "5000",
        "short_open_interest_balance_volume_foreign_investment": "3000",
    }
    out = transform_row(
        fm, find_mapping("TaiwanFuturesInstitutionalInvestors"),
    )
    assert out["session"] == "day"
    assert out["contract"] == "TX"
    assert out["foreign_long_open_interest"] == 5000


def test_futures_inst_night_session_injected():
    fm = {
        "date": "2024-01-02", "futures_id": "TX",
        "long_open_interest_balance_volume_foreign_investment": "200",
    }
    out = transform_row(
        fm,
        find_mapping("TaiwanFuturesInstitutionalInvestorsAfterHours"),
    )
    assert out["session"] == "night"


@pytest.mark.asyncio
async def test_ingest_futures_inst_day_and_night_coexist(finmind_session):
    """Both sessions land in the same tw_futures_inst_daily table for
    the same (contract, ts) — discriminated by `session`. Verify the
    PK doesn't collide."""
    await seed_dataset_sources()

    day_fake = FakeClient([{
        "date": "2024-01-02", "futures_id": "TX",
        "long_open_interest_balance_volume_foreign_investment": "5000",
    }])
    night_fake = FakeClient([{
        "date": "2024-01-02", "futures_id": "TX",
        "long_open_interest_balance_volume_foreign_investment": "200",
    }])

    r1 = await ingest_chunk(
        finmind_session,
        dataset_code="TaiwanFuturesInstitutionalInvestors",
        symbol=None,
        range_start=date(2024, 1, 1),
        range_end=date(2024, 1, 31),
        client=day_fake,
    )
    r2 = await ingest_chunk(
        finmind_session,
        dataset_code="TaiwanFuturesInstitutionalInvestorsAfterHours",
        symbol=None,
        range_start=date(2024, 1, 1),
        range_end=date(2024, 1, 31),
        client=night_fake,
    )

    assert r1.status == "done"
    assert r2.status == "done"

    rows = (
        await finmind_session.execute(
            text(
                "SELECT contract, session, foreign_long_open_interest "
                "FROM tw_futures_inst_daily ORDER BY session"
            )
        )
    ).all()
    assert len(rows) == 2
    by_session = {r[1]: r for r in rows}
    assert by_session["day"][2] == 5000
    assert by_session["night"][2] == 200
