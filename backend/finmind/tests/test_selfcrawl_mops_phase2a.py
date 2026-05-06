"""Tests for the Phase 2A MOPS quarterly statement batch wired into
`finmind.ingest.selfcrawl.mops`:

  - TaiwanStockFinancialStatements   (綜合損益表, t164sb04_ifrs)
  - TaiwanStockBalanceSheet          (資產負債表, t164sb03_ifrs)
  - TaiwanStockCashFlowsStatement    (現金流量表, t164sb05_ifrs)

Each handler fans the runner's [start, end] date range out into
individual (year, quarter) tuples and concatenates per-quarter MOPS
fetches. Mocks `data.tw.mops_connector.get_*_statement` so tests run
without network.
"""
from __future__ import annotations

from datetime import date

import pytest

from finmind.ingest.selfcrawl import covers_dataset
from finmind.ingest.selfcrawl.mops import (
    MopsClient,
    _quarters_in_range,
    supported_datasets,
)


# ── supported_datasets pin ──────────────────────────────────────


def test_supported_datasets_includes_phase_2a_batch():
    s = supported_datasets()
    assert "TaiwanStockFinancialStatements" in s
    assert "TaiwanStockBalanceSheet" in s
    assert "TaiwanStockCashFlowsStatement" in s


def test_covers_dataset_predicate_recognises_new_handlers():
    for code in (
        "TaiwanStockFinancialStatements",
        "TaiwanStockBalanceSheet",
        "TaiwanStockCashFlowsStatement",
    ):
        assert covers_dataset("mops", code) is True, code


# ── _quarters_in_range helper ───────────────────────────────────


def test_quarters_in_range_full_year():
    """Jan 1 - Dec 31 of a single year → 4 quarters."""
    out = _quarters_in_range(date(2024, 1, 1), date(2024, 12, 31))
    assert out == [(2024, 1), (2024, 2), (2024, 3), (2024, 4)]


def test_quarters_in_range_clips_at_window_boundary():
    """Window that starts mid-quarter should still pick up that
    quarter (cutoff date Mar 31 falls inside a Feb-Apr window)."""
    out = _quarters_in_range(date(2024, 2, 15), date(2024, 8, 1))
    # Q1 cutoff 2024-03-31 falls in window → keep
    # Q2 cutoff 2024-06-30 falls in window → keep
    # Q3 cutoff 2024-09-30 > end → drop
    assert out == [(2024, 1), (2024, 2)]


def test_quarters_in_range_crosses_year_boundary():
    """Q4 2023 (cutoff 2023-12-31) + Q1-Q2 2024 → 3 quarters."""
    out = _quarters_in_range(date(2023, 12, 1), date(2024, 7, 1))
    assert out == [(2023, 4), (2024, 1), (2024, 2)]


def test_quarters_in_range_empty_when_no_cutoff_in_window():
    """Window entirely between two quarter ends → empty list."""
    out = _quarters_in_range(date(2024, 4, 1), date(2024, 5, 30))
    assert out == []


# ── TaiwanStockFinancialStatements (損益表) ─────────────────────


@pytest.mark.asyncio
async def test_fetch_income_statement_concatenates_multi_quarter(monkeypatch):
    """[2024-01-01, 2024-09-30] → 3 quarters → 3 MOPS calls. Wrapper
    concatenates the FinMind-shaped rows, runner's pivot then groups
    by (stock_id, date)."""
    seen: list = []

    async def fake_get_income_statement(symbol, year, quarter):
        seen.append((symbol, year, quarter))
        # Return one canonical line item per quarter so the test can
        # confirm the concatenation works without fixture clutter.
        return [
            {"stock_id": symbol,
             "date":   {1: "2024-03-31", 2: "2024-06-30", 3: "2024-09-30"}[quarter],
             "type":   "OperatingRevenue",
             "value":  100_000 + quarter * 1_000.0},
        ]

    monkeypatch.setattr(
        "data.tw.mops_connector.get_income_statement",
        fake_get_income_statement,
    )

    client = MopsClient()
    rows = await client.fetch(
        "TaiwanStockFinancialStatements", "2330",
        date(2024, 1, 1), date(2024, 9, 30),
    )

    assert seen == [("2330", 2024, 1), ("2330", 2024, 2), ("2330", 2024, 3)]
    assert len(rows) == 3
    assert {r["date"] for r in rows} == {
        "2024-03-31", "2024-06-30", "2024-09-30",
    }
    assert all(r["type"] == "OperatingRevenue" for r in rows)
    assert all(r["stock_id"] == "2330" for r in rows)


@pytest.mark.asyncio
async def test_fetch_income_statement_swallows_per_quarter_errors(monkeypatch):
    """One quarter's MOPS call throws → wrapper logs and continues so
    the rest of the window still backfills. Mirrors the per-day error
    contract used in TWSE handlers."""
    failures = {(2024, 2)}

    async def fake_get_income_statement(symbol, year, quarter):
        if (year, quarter) in failures:
            raise RuntimeError("simulated MOPS rate-limit")
        return [{"stock_id": symbol,
                 "date": "2024-03-31" if quarter == 1 else "2024-09-30",
                 "type": "Revenue", "value": 1.0}]

    monkeypatch.setattr(
        "data.tw.mops_connector.get_income_statement",
        fake_get_income_statement,
    )

    client = MopsClient()
    rows = await client.fetch(
        "TaiwanStockFinancialStatements", "2330",
        date(2024, 1, 1), date(2024, 9, 30),
    )
    # Q1 + Q3 succeed (Q2 errored).
    assert {r["date"] for r in rows} == {"2024-03-31", "2024-09-30"}


@pytest.mark.asyncio
async def test_fetch_income_statement_requires_symbol():
    """MOPS is per-symbol-only — refuse market-wide call so the
    requirement is loud rather than silently returning []."""
    client = MopsClient()
    with pytest.raises(ValueError, match="per-symbol call"):
        await client.fetch(
            "TaiwanStockFinancialStatements", None,
            date(2024, 1, 1), date(2024, 12, 31),
        )


# ── TaiwanStockBalanceSheet (資產負債表) ────────────────────────


@pytest.mark.asyncio
async def test_fetch_balance_sheet_passes_through_to_connector(monkeypatch):
    """Smoke test: symbol + window → connector called per quarter
    → returned rows pass through to runner unchanged. The pivot
    machinery is tested separately at the connector + mappings
    layer; here we just confirm wiring."""
    captured = []

    async def fake_get_balance_sheet(symbol, year, quarter):
        captured.append((symbol, year, quarter))
        return [{"stock_id": symbol, "date": "2024-03-31",
                 "type": "TotalAssets", "value": 1_000_000.0}]

    monkeypatch.setattr(
        "data.tw.mops_connector.get_balance_sheet",
        fake_get_balance_sheet,
    )

    client = MopsClient()
    rows = await client.fetch(
        "TaiwanStockBalanceSheet", "2330",
        date(2024, 1, 1), date(2024, 3, 31),
    )
    assert captured == [("2330", 2024, 1)]
    assert rows[0]["type"] == "TotalAssets"


# ── TaiwanStockCashFlowsStatement (現金流量表) ──────────────────


@pytest.mark.asyncio
async def test_fetch_cash_flow_statement_passes_through_to_connector(monkeypatch):
    captured = []

    async def fake_get_cash_flow_statement(symbol, year, quarter):
        captured.append((symbol, year, quarter))
        return [{"stock_id": symbol, "date": "2024-06-30",
                 "type": "CashFlowsFromOperatingActivities",
                 "value": 50_000.0}]

    monkeypatch.setattr(
        "data.tw.mops_connector.get_cash_flow_statement",
        fake_get_cash_flow_statement,
    )

    client = MopsClient()
    rows = await client.fetch(
        "TaiwanStockCashFlowsStatement", "2330",
        date(2024, 4, 1), date(2024, 6, 30),
    )
    assert captured == [("2330", 2024, 2)]
    assert rows[0]["type"] == "CashFlowsFromOperatingActivities"
