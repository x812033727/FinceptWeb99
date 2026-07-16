"""Tests for the statement-derived payload half of ingest_fundamentals_tw.

TWSE's BWIBBU_ALL has no ROE and no cash flow, so `payload` used to be
hard-coded None and the chip_quality strategy could never qualify a
candidate. These cover the FinMind market-wide statement path that fills
it.
"""
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from tasks.ingest_fundamentals_tw import _load_statement_payloads, _recent_quarter_ends


def test_recent_quarter_ends_walks_back_from_the_last_closed_quarter():
    ends = _recent_quarter_ends(date(2026, 7, 16), 6)

    assert ends == [
        date(2025, 3, 31), date(2025, 6, 30), date(2025, 9, 30),
        date(2025, 12, 31), date(2026, 3, 31), date(2026, 6, 30),
    ]


def test_recent_quarter_ends_excludes_a_quarter_that_has_not_ended():
    # Mid-Q1: 2026-03-31 hasn't happened yet, so the window ends at Q4.
    ends = _recent_quarter_ends(date(2026, 2, 10), 3)

    assert ends == [date(2025, 6, 30), date(2025, 9, 30), date(2025, 12, 31)]
    assert all(e <= date(2026, 2, 10) for e in ends)


def _session_cm():
    """`AsyncSessionLocal()` stand-in for the paths that only need the
    session to exist, not to hold data."""
    class _CM:
        async def __aenter__(self):
            return AsyncMock()

        async def __aexit__(self, *exc):
            return False

    return lambda: _CM()


def _fact(symbol: str, d: str, type_: str, value: float) -> dict:
    return {"date": d, "stock_id": symbol, "type": type_, "value": value}


def _statements_for(symbol: str) -> tuple[list, list, list]:
    """Five quarters of a plainly profitable, cash-generative company."""
    quarters = ["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31", "2026-03-31"]
    income, balance, cash = [], [], []
    for i, q in enumerate(quarters, 1):
        income.append(_fact(symbol, q, "Revenue", 1_000_000.0))
        income.append(_fact(symbol, q, "IncomeAfterTaxes", 100_000.0))
        balance.append(_fact(symbol, q, "TotalAssets", 5_000_000.0))
        balance.append(_fact(symbol, q, "Liabilities", 2_000_000.0))
        balance.append(_fact(symbol, q, "Equity", 3_000_000.0))
        # Cash-flow facts are calendar-year YTD in Taiwan — cumulative
        # within the year so the quarterizer has something to difference.
        ytd = 120_000.0 * (i if i <= 4 else 1)
        cash.append(_fact(symbol, q, "CashFlowsFromOperatingActivities", ytd))
    return income, balance, cash


@pytest.mark.asyncio
async def test_payload_carries_roe_ocf_and_debt_ratio():
    income, balance, cash = _statements_for("2330")

    def _by_quarter(rows):
        async def _fetch(quarter_end):
            return [r for r in rows if r["date"] == quarter_end]
        return _fetch

    with patch(
        "tasks.ingest_fundamentals_tw.finmind.get_financials_market_wide",
        _by_quarter(income),
    ), patch(
        "tasks.ingest_fundamentals_tw.finmind.get_balance_sheet_market_wide",
        _by_quarter(balance),
    ), patch(
        "tasks.ingest_fundamentals_tw.finmind.get_cash_flow_market_wide",
        _by_quarter(cash),
    ):
        payloads = await _load_statement_payloads()

    assert "2330" in payloads
    payload = payloads["2330"]
    assert payload["roe"] > 0
    assert payload["operating_cash_flow"] > 0
    assert payload["debt_ratio"] == pytest.approx(40.0)  # 2M / 5M


@pytest.mark.asyncio
async def test_underived_keys_are_omitted_not_written_as_none():
    """`auto_run_discussion` reads `payload.get("debt_ratio", 100)`. An
    explicit None satisfies the key and returns None, which downstream
    coerces to 0.0 — flipping the missing-data default from worst-case
    to best-case and flattering the score. Omit instead."""
    income, _, cash = _statements_for("2454")

    async def _empty(quarter_end):
        return []

    def _by_quarter(rows):
        async def _fetch(quarter_end):
            return [r for r in rows if r["date"] == quarter_end]
        return _fetch

    with patch(
        "tasks.ingest_fundamentals_tw.finmind.get_financials_market_wide",
        _by_quarter(income),
    ), patch(
        "tasks.ingest_fundamentals_tw.finmind.get_balance_sheet_market_wide",
        _empty,  # no balance sheet → no equity → no ROE, no debt ratio
    ), patch(
        "tasks.ingest_fundamentals_tw.finmind.get_cash_flow_market_wide",
        _by_quarter(cash),
    ):
        payloads = await _load_statement_payloads()

    payload = payloads.get("2454", {})
    assert "roe" not in payload
    assert "debt_ratio" not in payload
    assert None not in payload.values()


@pytest.mark.asyncio
async def test_statement_failure_still_lets_ratios_through():
    """FinMind being down must not cost us the TWSE PE/PB/yield write —
    the statements are a bonus, not the job's contract."""
    from tasks import ingest_fundamentals_tw

    with patch(
        "tasks.ingest_fundamentals_tw.twse.get_all_valuation_ratios",
        AsyncMock(return_value={"2330": {"pe_ratio": 20.0, "pb_ratio": 5.0}}),
    ), patch(
        "tasks.ingest_fundamentals_tw._load_statement_payloads",
        AsyncMock(side_effect=RuntimeError("finmind down")),
    ), patch(
        "tasks.ingest_fundamentals_tw._carry_forward_payloads",
        AsyncMock(return_value={}),
    ), patch(
        "tasks.ingest_fundamentals_tw.upsert_fundamentals_snapshots",
        AsyncMock(return_value=1),
    ) as upsert, patch(
        "tasks.ingest_fundamentals_tw.AsyncSessionLocal", _session_cm(),
    ):
        written, as_of = await ingest_fundamentals_tw._do_run()

    assert written == 1
    assert as_of == date.today()
    rows = upsert.await_args.args[1]
    assert rows[0].pe_ratio == 20.0
    assert rows[0].payload is None


@pytest.mark.asyncio
async def test_carry_forward_keeps_last_payload_when_statements_unavailable(
    db_session,
):
    """A quota-exhausted FinMind must not wipe a good payload — this job
    rewrites every row daily, so None would un-qualify every
    chip_quality candidate until the next successful pull."""
    from models.fundamentals_snapshot import FundamentalsSnapshot
    from tasks.ingest_fundamentals_tw import _carry_forward_payloads

    db_session.add(FundamentalsSnapshot(
        market="TW", symbol="2330", as_of=date.today() - timedelta(days=2),
        pe_ratio=20.0, payload={"roe": 25.0, "operating_cash_flow": 1.0},
        source="twse",
    ))
    db_session.add(FundamentalsSnapshot(
        market="TW", symbol="2330", as_of=date.today() - timedelta(days=1),
        pe_ratio=21.0, payload={"roe": 26.0, "operating_cash_flow": 2.0},
        source="twse",
    ))
    await db_session.commit()

    carried = await _carry_forward_payloads(db_session)

    assert carried["2330"]["roe"] == 26.0, "must carry the newest payload"


@pytest.mark.asyncio
async def test_carry_forward_ignores_payloads_older_than_the_window(db_session):
    from models.fundamentals_snapshot import FundamentalsSnapshot
    from tasks.ingest_fundamentals_tw import _CARRY_FORWARD_DAYS, _carry_forward_payloads

    db_session.add(FundamentalsSnapshot(
        market="TW", symbol="9999",
        as_of=date.today() - timedelta(days=_CARRY_FORWARD_DAYS + 1),
        pe_ratio=8.0, payload={"roe": 3.0}, source="twse",
    ))
    await db_session.commit()

    carried = await _carry_forward_payloads(db_session)

    assert "9999" not in carried
