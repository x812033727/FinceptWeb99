"""Tests for wide-format batch_transform mappings.

The 4 pivot datasets unlock financial statements + market-wide 三大
法人 backfill — they take long-format FinMind payloads (one row per
line item per period) and pivot them onto our wide local-table
schema with typed canonical columns + a `raw` JSONB catch-all."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

import pytest
from sqlalchemy import text

from finmind.ingest.mappings import (
    MAPPINGS,
    _normalize_period,
    _pivot_balance_sheet,
    _pivot_cash_flow,
    _pivot_income_statement,
    _pivot_total_institutional,
    find_mapping,
)
from finmind.ingest.runner import ingest_chunk
from finmind.scripts.init_db import seed_dataset_sources


@dataclass
class FakeClient:
    rows: list

    async def fetch(self, *args, **kwargs):
        return list(self.rows)


# ── _normalize_period helper ────────────────────────────────────


def test_normalize_period_handles_finmind_quarter_format():
    period, period_end = _normalize_period("2024-Q1")
    assert period == "2024Q1"
    assert period_end == date(2024, 3, 31)

    period, period_end = _normalize_period("2024Q3")
    assert period == "2024Q3"
    assert period_end == date(2024, 9, 30)


def test_normalize_period_derives_quarter_from_iso_date():
    period, period_end = _normalize_period("2024-03-31")
    assert period == "2024Q1"
    assert period_end == date(2024, 3, 31)

    period, period_end = _normalize_period("2024-08-15")
    assert period == "2024Q3"
    assert period_end == date(2024, 8, 15)


def test_normalize_period_returns_blank_for_garbage():
    assert _normalize_period("") == ("", None)
    assert _normalize_period("not-a-date") == ("", None)


# ── Pivot logic ─────────────────────────────────────────────────


def test_pivot_income_statement_groups_line_items_by_period():
    """5 FinMind rows for one (stock, quarter) → 1 local row with
    canonical columns populated + raw JSONB carrying every line item."""
    rows = [
        {"date": "2024-Q1", "stock_id": "2330",
         "type": "Revenue", "value": 600000000000},
        {"date": "2024-Q1", "stock_id": "2330",
         "type": "OperatingIncome", "value": 250000000000},
        {"date": "2024-Q1", "stock_id": "2330",
         "type": "NetIncome", "value": 200000000000},
        {"date": "2024-Q1", "stock_id": "2330",
         "type": "EPS", "value": 8.5},
        {"date": "2024-Q1", "stock_id": "2330",
         "type": "GrossProfit", "value": 300000000000},
    ]
    out = _pivot_income_statement(rows)
    assert len(out) == 1
    row = out[0]
    assert row["symbol"] == "2330"
    assert row["period"] == "2024Q1"
    assert row["period_end"] == date(2024, 3, 31)
    assert float(row["revenue"]) == 600_000_000_000.0
    assert float(row["operating_income"]) == 250_000_000_000.0
    assert float(row["net_income"]) == 200_000_000_000.0
    assert float(row["eps"]) == 8.5
    # GrossProfit isn't canonical — must still land in raw.
    assert row["raw"]["GrossProfit"] == 300_000_000_000


def test_pivot_income_statement_emits_one_row_per_period():
    """Multiple quarters in one chunk → one local row each."""
    rows = []
    for q in range(1, 5):
        for line_type in ("Revenue", "NetIncome"):
            rows.append({
                "date": f"2024-Q{q}",
                "stock_id": "2330",
                "type": line_type,
                "value": 100 * q,
            })
    out = _pivot_income_statement(rows)
    assert len(out) == 4
    periods = sorted(r["period"] for r in out)
    assert periods == ["2024Q1", "2024Q2", "2024Q3", "2024Q4"]


def test_pivot_balance_sheet_canonical_columns():
    rows = [
        {"date": "2024-Q1", "stock_id": "2330",
         "type": "TotalAssets", "value": 5000000000000},
        {"date": "2024-Q1", "stock_id": "2330",
         "type": "TotalLiabilities", "value": 1500000000000},
        {"date": "2024-Q1", "stock_id": "2330",
         "type": "Equity", "value": 3500000000000},
        {"date": "2024-Q1", "stock_id": "2330",
         "type": "Cash", "value": 800000000000},
    ]
    out = _pivot_balance_sheet(rows)
    assert len(out) == 1
    row = out[0]
    assert float(row["total_assets"]) == 5_000_000_000_000.0
    assert float(row["total_liabilities"]) == 1_500_000_000_000.0
    assert float(row["total_equity"]) == 3_500_000_000_000.0
    assert float(row["cash"]) == 800_000_000_000.0


def test_pivot_cash_flow_handles_finmind_camel_case():
    rows = [
        {"date": "2024-Q1", "stock_id": "2330",
         "type": "CashFlowsFromOperatingActivities", "value": 350000000000},
        {"date": "2024-Q1", "stock_id": "2330",
         "type": "CashFlowsFromInvestingActivities", "value": -200000000000},
        {"date": "2024-Q1", "stock_id": "2330",
         "type": "CashFlowsFromFinancingActivities", "value": -50000000000},
    ]
    out = _pivot_cash_flow(rows)
    assert len(out) == 1
    row = out[0]
    assert float(row["operating_cash_flow"]) == 350_000_000_000.0
    assert float(row["investing_cash_flow"]) == -200_000_000_000.0
    assert float(row["financing_cash_flow"]) == -50_000_000_000.0
    # Free cash flow not provided by FinMind — stays None, not missing.
    assert row["free_cash_flow"] is None


def test_pivot_total_institutional_aggregates_buy_minus_sell():
    """Three FinMind rows (外資 / 投信 / 自營商) for one date → one
    local row with foreign_net / sitc_net / dealer_net = buy - sell."""
    rows = [
        {"date": "2024-01-02",
         "name": "外資及陸資(不含外資自營商)", "buy": 50000, "sell": 30000},
        {"date": "2024-01-02",
         "name": "投信", "buy": 5000, "sell": 3000},
        {"date": "2024-01-02",
         "name": "自營商", "buy": 8000, "sell": 6000},
    ]
    out = _pivot_total_institutional(rows)
    assert len(out) == 1
    row = out[0]
    assert row["ts"] == date(2024, 1, 2)
    assert row["foreign_net"] == 20000  # 50000 - 30000
    assert row["sitc_net"] == 2000      # 5000 - 3000
    assert row["dealer_net"] == 2000    # 8000 - 6000


def test_pivot_total_institutional_handles_split_dealer_rows():
    """FinMind sometimes returns 自營商 split into hedging vs proprietary
    — verify both rows accumulate into a single dealer_net."""
    rows = [
        {"date": "2024-01-02", "name": "自營商(避險)",
         "buy": 5000, "sell": 1000},
        {"date": "2024-01-02", "name": "自營商(自行買賣)",
         "buy": 3000, "sell": 2000},
    ]
    out = _pivot_total_institutional(rows)
    assert len(out) == 1
    assert out[0]["dealer_net"] == 5000  # (5000-1000) + (3000-2000)


def test_pivot_total_institutional_groups_by_date():
    """Multiple dates in one chunk → one row each."""
    rows = [
        {"date": "2024-01-02", "name": "外資", "buy": 100, "sell": 50},
        {"date": "2024-01-03", "name": "外資", "buy": 200, "sell": 100},
    ]
    out = _pivot_total_institutional(rows)
    assert len(out) == 2
    by_ts = {r["ts"]: r["foreign_net"] for r in out}
    assert by_ts[date(2024, 1, 2)] == 50
    assert by_ts[date(2024, 1, 3)] == 100


# ── Mapping registration pin ────────────────────────────────────


def test_pivot_mappings_registered():
    """All 4 pivot mappings present + use batch_transform (not row_transform)."""
    for code in (
        "TaiwanStockFinancialStatements",
        "TaiwanStockBalanceSheet",
        "TaiwanStockCashFlowsStatement",
        "TaiwanStockTotalInstitutionalInvestors",
    ):
        m = find_mapping(code)
        assert m.batch_transform is not None, (
            f"{code} must use batch_transform for wide→long pivot"
        )

    # Headline-5 + extra-10 still on the row_transform path; only the
    # 4 wide-format ones should set batch_transform.
    assert MAPPINGS["TaiwanStockPrice"].batch_transform is None
    assert MAPPINGS["TaiwanStockFinancialStatements"].batch_transform is not None


# ── End-to-end ingest ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ingest_income_statement_pivots_and_writes(finmind_session):
    """Headline regression: long-format payload → 1 wide local row per
    quarter via the runner's batch_transform branch."""
    await seed_dataset_sources()

    fake = FakeClient([
        {"date": "2024-Q1", "stock_id": "2330",
         "type": "Revenue", "value": 600000000000},
        {"date": "2024-Q1", "stock_id": "2330",
         "type": "NetIncome", "value": 200000000000},
        {"date": "2024-Q1", "stock_id": "2330",
         "type": "EPS", "value": 8.5},
        {"date": "2024-Q1", "stock_id": "2330",
         "type": "GrossProfit", "value": 300000000000},
        {"date": "2024-Q2", "stock_id": "2330",
         "type": "Revenue", "value": 650000000000},
        {"date": "2024-Q2", "stock_id": "2330",
         "type": "NetIncome", "value": 220000000000},
    ])

    result = await ingest_chunk(
        finmind_session,
        dataset_code="TaiwanStockFinancialStatements",
        symbol="2330",
        range_start=date(2024, 1, 1),
        range_end=date(2024, 6, 30),
        client=fake,
    )
    assert result.status == "done"
    assert result.rows_written == 2  # Two quarters

    rows = (
        await finmind_session.execute(
            text(
                "SELECT symbol, period, revenue, net_income, eps, raw "
                "FROM tw_income_statement ORDER BY period"
            )
        )
    ).all()
    assert len(rows) == 2

    q1 = rows[0]
    assert q1[0] == "2330"
    assert q1[1] == "2024Q1"
    assert float(q1[2]) == 600_000_000_000.0
    assert float(q1[4]) == 8.5
    # raw JSONB stored as JSON string in SQLite; parse + verify the
    # non-canonical line item survived the round-trip.
    raw = json.loads(q1[5]) if isinstance(q1[5], str) else q1[5]
    assert raw["GrossProfit"] == 300_000_000_000

    q2 = rows[1]
    assert q2[1] == "2024Q2"
    # Q2's chunk had no EPS line item → column should be NULL.
    assert q2[4] is None


@pytest.mark.asyncio
async def test_ingest_total_institutional_writes_pivoted_row(finmind_session):
    await seed_dataset_sources()

    fake = FakeClient([
        {"date": "2024-01-02", "name": "外資及陸資(不含外資自營商)",
         "buy": 50000, "sell": 30000},
        {"date": "2024-01-02", "name": "投信",
         "buy": 5000, "sell": 3000},
        {"date": "2024-01-02", "name": "自營商",
         "buy": 8000, "sell": 6000},
    ])

    result = await ingest_chunk(
        finmind_session,
        dataset_code="TaiwanStockTotalInstitutionalInvestors",
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
                "SELECT market, ts, foreign_net, sitc_net, dealer_net "
                "FROM tw_total_inst_daily"
            )
        )
    ).one()
    assert row[0] == "TWSE"
    assert str(row[1]) == "2024-01-02"
    assert row[2] == 20000
    assert row[3] == 2000
    assert row[4] == 2000


@pytest.mark.asyncio
async def test_ingest_pivot_re_run_overwrites_in_place(finmind_session):
    """Idempotency invariant — re-running the same chunk overwrites
    the pivoted local row instead of duplicating. Same property as
    Phase-2 row_transform path; verify the batch path inherits it."""
    await seed_dataset_sources()

    fake_v1 = FakeClient([
        {"date": "2024-Q1", "stock_id": "2330",
         "type": "Revenue", "value": 600000000000},
        {"date": "2024-Q1", "stock_id": "2330",
         "type": "NetIncome", "value": 200000000000},
    ])
    fake_v2 = FakeClient([
        {"date": "2024-Q1", "stock_id": "2330",
         "type": "Revenue", "value": 605000000000},  # corrected upward
        {"date": "2024-Q1", "stock_id": "2330",
         "type": "NetIncome", "value": 202000000000},
    ])

    for fake in (fake_v1, fake_v2):
        result = await ingest_chunk(
            finmind_session,
            dataset_code="TaiwanStockFinancialStatements",
            symbol="2330",
            range_start=date(2024, 1, 1),
            range_end=date(2024, 3, 31),
            client=fake,
        )
        assert result.status == "done"

    rows = (
        await finmind_session.execute(
            text(
                "SELECT period, revenue FROM tw_income_statement"
            )
        )
    ).all()
    # Still 1 row (idempotent), with the corrected value.
    assert len(rows) == 1
    assert rows[0][0] == "2024Q1"
    assert float(rows[0][1]) == 605_000_000_000.0
