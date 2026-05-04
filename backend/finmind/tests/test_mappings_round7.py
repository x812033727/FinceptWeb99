"""Tests for the 5 mappings landed in this batch (依序 round 7):

  - TaiwanStockMarketValueWeight
  - TaiwanStockPriceLimit
  - TaiwanStockSuspended
  - TaiwanBusinessIndicator
  - TaiwanStockDelisting

Each gets a column-rename smoke + an end-to-end ingest_chunk that
writes into the local table, except for the sparse event ones
(Suspended / Delisting) where the smoke alone is enough — they
exercise the same UPSERT path as the other event-shaped mappings
already covered (Buyback / DividendResult).
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


def test_round7_mappings_registered():
    expected = {
        "TaiwanStockMarketValueWeight",
        "TaiwanStockPriceLimit",
        "TaiwanStockSuspended",
        "TaiwanBusinessIndicator",
        "TaiwanStockDelisting",
    }
    assert expected.issubset(MAPPINGS.keys())


# ── MarketValueWeight ──────────────────────────────────────────


def test_market_value_weight_mapping_round_trips():
    fm = {
        "date": "2024-01-02", "stock_id": "2330",
        "weight_per": "0.30",
        "TotalMarketValue": "15000000000000",
    }
    out = transform_row(fm, find_mapping("TaiwanStockMarketValueWeight"))
    assert out["symbol"] == "2330"
    assert out["ts"] == date(2024, 1, 2)
    assert float(out["weight"]) == 0.30
    assert float(out["market_cap"]) == 15_000_000_000_000.0


@pytest.mark.asyncio
async def test_ingest_market_value_weight_writes(finmind_session):
    await seed_dataset_sources()
    fake = FakeClient([{
        "date": "2024-01-02", "stock_id": "2330",
        "weight_per": "0.30", "TotalMarketValue": "15000000000000",
    }])
    result = await ingest_chunk(
        finmind_session,
        dataset_code="TaiwanStockMarketValueWeight",
        symbol="2330",
        range_start=date(2024, 1, 1),
        range_end=date(2024, 1, 31),
        client=fake,
    )
    assert result.status == "done"
    assert result.rows_written == 1


# ── PriceLimit ─────────────────────────────────────────────────


def test_price_limit_mapping_round_trips():
    fm = {
        "date": "2024-01-02", "stock_id": "2330",
        "PriceUpLimit": "660", "PriceDownLimit": "540",
    }
    out = transform_row(fm, find_mapping("TaiwanStockPriceLimit"))
    assert float(out["upper_limit"]) == 660.0
    assert float(out["lower_limit"]) == 540.0


# ── Suspended (sparse) ─────────────────────────────────────────


def test_suspended_mapping_round_trips():
    fm = {
        "stock_id": "9999",
        "suspend_date": "2024-04-01",
        "resume_date": "2024-04-15",
        "reason": "查核",
    }
    out = transform_row(fm, find_mapping("TaiwanStockSuspended"))
    assert out["symbol"] == "9999"
    assert out["suspended_at"] == date(2024, 4, 1)
    assert out["resumed_at"] == date(2024, 4, 15)
    assert out["reason"] == "查核"


def test_suspended_mapping_handles_open_ended_suspension():
    """Active suspensions don't have a resume_date yet — must coerce
    to None rather than raising."""
    fm = {
        "stock_id": "9998",
        "suspend_date": "2024-05-01",
        "reason": "公告處置",
    }
    out = transform_row(fm, find_mapping("TaiwanStockSuspended"))
    assert out["resumed_at"] is None
    assert out["reason"] == "公告處置"


# ── BusinessIndicator ──────────────────────────────────────────


def test_business_indicator_mapping_round_trips():
    fm = {
        "date": "2024-04-01", "score": "30", "signal": "綠燈",
    }
    out = transform_row(fm, find_mapping("TaiwanBusinessIndicator"))
    assert out["ts"] == date(2024, 4, 1)
    assert out["score"] == 30
    assert out["signal"] == "綠燈"


@pytest.mark.asyncio
async def test_ingest_business_indicator_writes(finmind_session):
    """Market-wide dataset (no per-symbol axis) — ingest_chunk gets
    symbol=None and the runner handles that cleanly."""
    await seed_dataset_sources()
    fake = FakeClient([
        {"date": "2024-03-01", "score": "26", "signal": "黃藍燈"},
        {"date": "2024-04-01", "score": "30", "signal": "綠燈"},
    ])
    result = await ingest_chunk(
        finmind_session,
        dataset_code="TaiwanBusinessIndicator",
        symbol=None,
        range_start=date(2024, 1, 1),
        range_end=date(2024, 12, 31),
        client=fake,
    )
    assert result.status == "done"
    assert result.rows_written == 2

    rows = (
        await finmind_session.execute(
            text("SELECT ts, score, signal FROM tw_business_indicator ORDER BY ts")
        )
    ).all()
    assert len(rows) == 2
    assert rows[0][1] == 26


# ── Delisting (sparse) ─────────────────────────────────────────


def test_delisting_mapping_round_trips():
    fm = {
        "stock_id": "1234", "date": "2024-12-31",
        "reason": "下市",
    }
    out = transform_row(fm, find_mapping("TaiwanStockDelisting"))
    assert out["symbol"] == "1234"
    assert out["delisted_at"] == date(2024, 12, 31)


@pytest.mark.asyncio
async def test_ingest_delisting_writes(finmind_session):
    await seed_dataset_sources()
    fake = FakeClient([{
        "stock_id": "1234", "date": "2024-12-31",
        "reason": "下市",
    }])
    result = await ingest_chunk(
        finmind_session,
        dataset_code="TaiwanStockDelisting",
        symbol=None,
        range_start=date(2024, 1, 1),
        range_end=date(2024, 12, 31),
        client=fake,
    )
    assert result.status == "done"
    assert result.rows_written == 1
