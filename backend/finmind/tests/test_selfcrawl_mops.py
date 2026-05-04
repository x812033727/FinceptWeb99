"""Tests for `finmind.ingest.selfcrawl.mops` — Phase B MOPS wrapper.

Mocks `data.tw.mops_connector.get_monthly_revenue_recent` so tests
don't need network. Covers:
  - resolve_client('mops') returns the wired client (not the stub)
  - supported_datasets pin
  - per-symbol fetch translates MOPS rows → FinMind shape
  - in-process date-range filter respected
  - per-symbol requirement enforced (raises on symbol=None)
  - unknown dataset → NotImplementedError naming the dataset
"""
from __future__ import annotations

from datetime import date

import pytest

from finmind.ingest.selfcrawl import resolve_client
from finmind.ingest.selfcrawl.mops import MopsClient, supported_datasets


def test_resolve_client_mops_returns_wired_client():
    client = resolve_client("mops")
    assert isinstance(client, MopsClient)


def test_supported_datasets_pin():
    assert "TaiwanStockMonthRevenue" in supported_datasets()


@pytest.mark.asyncio
async def test_fetch_monthly_revenue_translates_to_finmind_shape(monkeypatch):
    """MOPS returns rows with `symbol/date/revenue/revenue_yoy/
    revenue_mom`. Wrapper rewrites to FinMind's `date/stock_id/
    revenue/revenue_year/revenue_month` so the existing
    TaiwanStockMonthRevenue mapping projects unchanged."""
    async def fake_get_monthly_revenue_recent(symbol):
        return [
            {
                "symbol": "2330", "date": "2024-03-01",
                "revenue": 195_000_000_000,
                "revenue_yoy": 16.4, "revenue_mom": -0.6,
            },
            {
                "symbol": "2330", "date": "2024-04-01",
                "revenue": 226_300_000_000,
                "revenue_yoy": 59.6, "revenue_mom": 16.0,
            },
        ]

    monkeypatch.setattr(
        "data.tw.mops_connector.get_monthly_revenue_recent",
        fake_get_monthly_revenue_recent,
    )

    client = MopsClient()
    rows = await client.fetch(
        "TaiwanStockMonthRevenue", "2330",
        date(2024, 1, 1), date(2024, 12, 31),
    )

    assert len(rows) == 2
    r0 = rows[0]
    assert r0["date"] == "2024-03-01"
    assert r0["stock_id"] == "2330"
    assert r0["revenue"] == 195_000_000_000
    # YoY + MoM survive the rename.
    assert r0["revenue_year"] == 16.4
    assert r0["revenue_month"] == -0.6


@pytest.mark.asyncio
async def test_fetch_monthly_revenue_filters_by_date_window(monkeypatch):
    """MOPS returns ~24 months by default; the wrapper clips to the
    requested window so the runner sees consistent contract."""
    async def fake(symbol):
        return [
            {"symbol": "2330", "date": "2023-01-01", "revenue": 1, "revenue_yoy": 0, "revenue_mom": 0},
            {"symbol": "2330", "date": "2024-03-01", "revenue": 2, "revenue_yoy": 0, "revenue_mom": 0},
            {"symbol": "2330", "date": "2024-06-01", "revenue": 3, "revenue_yoy": 0, "revenue_mom": 0},
            {"symbol": "2330", "date": "2025-01-01", "revenue": 4, "revenue_yoy": 0, "revenue_mom": 0},
        ]

    monkeypatch.setattr(
        "data.tw.mops_connector.get_monthly_revenue_recent", fake,
    )

    client = MopsClient()
    rows = await client.fetch(
        "TaiwanStockMonthRevenue", "2330",
        date(2024, 1, 1), date(2024, 12, 31),
    )
    # Only 2024-03 and 2024-06 fall in the window.
    assert len(rows) == 2
    assert {r["date"] for r in rows} == {"2024-03-01", "2024-06-01"}


@pytest.mark.asyncio
async def test_fetch_requires_symbol():
    """MOPS is a per-symbol API — refuse market-wide call so operator
    sees the requirement loudly instead of getting an empty result."""
    client = MopsClient()
    with pytest.raises(ValueError, match="per-symbol call"):
        await client.fetch(
            "TaiwanStockMonthRevenue", None,
            date(2024, 1, 1), date(2024, 12, 31),
        )


@pytest.mark.asyncio
async def test_fetch_unknown_dataset_raises_with_clear_message():
    client = MopsClient()
    with pytest.raises(NotImplementedError) as exc:
        await client.fetch(
            "TaiwanStockFinancialStatements", "2330",
            date(2024, 1, 1), date(2024, 3, 31),
        )
    msg = str(exc.value)
    assert "TaiwanStockFinancialStatements" in msg
    assert "MopsClient" in msg
