"""Tests for scripts.backfill_fundamentals_history.

The script exists because `fundamentals_snapshots` had only ten days of
history (BWIBBU_ALL is a today-only cross-section), which silently
emptied `chip_quality`'s candidate pool for every historical replay.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.fundamentals_snapshot import FundamentalsSnapshot
from scripts import backfill_fundamentals_history as bf


@pytest.fixture(autouse=True)
def _patch_sessions(db_session: AsyncSession):
    class _CM:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *exc):
            return False

    with patch.object(bf, "AsyncSessionLocal", return_value=_CM()), \
         patch(
             "services.ingest.repo.tw_fundamentals.AsyncSessionLocal",
             return_value=_CM(),
         ), \
         patch.object(bf.asyncio, "sleep", AsyncMock()):
        yield


@pytest.fixture(autouse=True)
async def _isolate(db_session: AsyncSession):
    await db_session.execute(delete(FundamentalsSnapshot))
    await db_session.commit()
    yield


def _ratios(pe: float) -> dict:
    return {
        "2330": {"pe_ratio": pe, "pb_ratio": 9.9, "dividend_yield": 1.0},
        "1101": {"pe_ratio": 12.0, "pb_ratio": 0.8, "dividend_yield": 3.3},
    }


@pytest.mark.asyncio
async def test_writes_one_row_per_symbol_per_session(db_session: AsyncSession):
    with patch.object(
        bf.twse, "get_all_valuation_ratios",
        AsyncMock(side_effect=lambda d: _ratios(30.0)),
    ), patch.object(
        bf, "_load_statement_payloads",
        AsyncMock(return_value={"2330": {"roe": 0.28}}),
    ):
        stats = await bf.backfill(
            date(2026, 5, 11), date(2026, 5, 12), dry_run=False, force=False,
        )

    assert stats["written_sessions"] == 2
    rows = (await db_session.scalars(select(FundamentalsSnapshot))).all()
    assert len(rows) == 4
    by_key = {(r.symbol, r.as_of): r for r in rows}
    assert by_key[("2330", date(2026, 5, 11))].payload == {"roe": 0.28}
    # Only what was derived is written: a symbol with no statement data
    # gets no payload at all rather than an explicit None, which readers
    # using `payload.get(key, default)` would resolve to the wrong side.
    assert by_key[("1101", date(2026, 5, 11))].payload is None


@pytest.mark.asyncio
async def test_statements_are_fetched_once_per_quarter_not_per_day():
    """Statements change four times a year. Re-fetching them daily would
    burn ~18 FinMind calls a day for identical answers."""
    calls: list[date] = []

    async def _load(as_of=None):
        calls.append(as_of)
        return {}

    with patch.object(
        bf.twse, "get_all_valuation_ratios",
        AsyncMock(side_effect=lambda d: _ratios(30.0)),
    ), patch.object(bf, "_load_statement_payloads", _load):
        await bf.backfill(
            date(2026, 5, 11), date(2026, 5, 15), dry_run=False, force=False,
        )

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_quarter_boundary_gets_its_own_statement_set():
    calls: list[date] = []

    async def _load(as_of=None):
        calls.append(as_of)
        return {}

    with patch.object(
        bf.twse, "get_all_valuation_ratios",
        AsyncMock(side_effect=lambda d: _ratios(30.0)),
    ), patch.object(bf, "_load_statement_payloads", _load):
        # Spans the 2026-03-31 quarter end.
        await bf.backfill(
            date(2026, 3, 30), date(2026, 4, 1), dry_run=False, force=False,
        )

    assert len(calls) == 2


@pytest.mark.asyncio
async def test_non_trading_days_are_skipped_without_a_write(
    db_session: AsyncSession,
):
    """TWSE answers a holiday with no rows. That is not a failure and
    must not write an empty session."""
    with patch.object(
        bf.twse, "get_all_valuation_ratios", AsyncMock(return_value={}),
    ), patch.object(bf, "_load_statement_payloads", AsyncMock(return_value={})):
        stats = await bf.backfill(
            date(2026, 5, 11), date(2026, 5, 12), dry_run=False, force=False,
        )

    assert stats["non_trading"] == 2
    assert stats["written_sessions"] == 0
    assert (await db_session.scalars(select(FundamentalsSnapshot))).all() == []


@pytest.mark.asyncio
async def test_weekends_are_never_requested():
    fetch = AsyncMock(side_effect=lambda d: _ratios(30.0))
    with patch.object(bf.twse, "get_all_valuation_ratios", fetch), \
         patch.object(bf, "_load_statement_payloads", AsyncMock(return_value={})):
        # 2026-05-16 is a Saturday, 05-17 a Sunday.
        await bf.backfill(
            date(2026, 5, 15), date(2026, 5, 18), dry_run=False, force=False,
        )

    asked = {c.args[0] for c in fetch.await_args_list}
    assert asked == {date(2026, 5, 15), date(2026, 5, 18)}


@pytest.mark.asyncio
async def test_already_archived_sessions_are_skipped(db_session: AsyncSession):
    db_session.add(FundamentalsSnapshot(
        market="TW", symbol="2330", as_of=date(2026, 5, 11),
        pe_ratio=30.0, source="twse",
    ))
    await db_session.commit()

    fetch = AsyncMock(side_effect=lambda d: _ratios(30.0))
    with patch.object(bf.twse, "get_all_valuation_ratios", fetch), \
         patch.object(bf, "_load_statement_payloads", AsyncMock(return_value={})):
        stats = await bf.backfill(
            date(2026, 5, 11), date(2026, 5, 12), dry_run=False, force=False,
        )

    assert stats["skipped_present"] == 1
    assert {c.args[0] for c in fetch.await_args_list} == {date(2026, 5, 12)}


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(db_session: AsyncSession):
    with patch.object(
        bf.twse, "get_all_valuation_ratios",
        AsyncMock(side_effect=lambda d: _ratios(30.0)),
    ), patch.object(bf, "_load_statement_payloads", AsyncMock(return_value={})):
        stats = await bf.backfill(
            date(2026, 5, 11), date(2026, 5, 12), dry_run=True, force=False,
        )

    assert stats["written_sessions"] == 2
    assert stats["written_rows"] == 0
    assert (await db_session.scalars(select(FundamentalsSnapshot))).all() == []


@pytest.mark.asyncio
async def test_one_failing_session_does_not_abort_the_window():
    async def _flaky(d):
        if d == date(2026, 5, 11):
            raise RuntimeError("twse hiccup")
        return _ratios(30.0)

    with patch.object(bf.twse, "get_all_valuation_ratios", AsyncMock(side_effect=_flaky)), \
         patch.object(bf, "_load_statement_payloads", AsyncMock(return_value={})):
        stats = await bf.backfill(
            date(2026, 5, 11), date(2026, 5, 12), dry_run=False, force=False,
        )

    assert stats["failed"] == 1
    assert stats["written_sessions"] == 1


# ── CLI ───────────────────────────────────────────────────────────


def test_args_require_a_window():
    with pytest.raises(SystemExit):
        bf._parse_args([])


def test_days_resolves_to_a_window_ending_today():
    args = bf._parse_args(["--days", "90"])
    assert args.days == 90 and args.start is None


def test_explicit_window_is_parsed_as_dates():
    args = bf._parse_args(["--start", "2026-04-20", "--end", "2026-07-11"])
    assert args.start == date(2026, 4, 20)
    assert args.end == date(2026, 7, 11)


@pytest.mark.asyncio
async def test_main_rejects_an_inverted_window():
    rc = await bf._main(["--start", "2026-07-11", "--end", "2026-04-20"])
    assert rc == 2


@pytest.mark.asyncio
async def test_main_reports_success_for_a_normal_run():
    with patch.object(
        bf.twse, "get_all_valuation_ratios",
        AsyncMock(side_effect=lambda d: _ratios(30.0)),
    ), patch.object(bf, "_load_statement_payloads", AsyncMock(return_value={})):
        rc = await bf._main(["--start", "2026-05-11", "--end", "2026-05-12"])
    assert rc == 0


@pytest.mark.asyncio
async def test_main_exits_nonzero_when_every_session_failed():
    """A total upstream outage must be distinguishable from a quiet
    window by the exit code, not just by reading the log."""
    with patch.object(
        bf.twse, "get_all_valuation_ratios",
        AsyncMock(side_effect=RuntimeError("twse down")),
    ), patch.object(bf, "_load_statement_payloads", AsyncMock(return_value={})):
        rc = await bf._main(["--start", "2026-05-11", "--end", "2026-05-12"])
    assert rc == 1


@pytest.mark.asyncio
async def test_statement_failure_does_not_lose_the_valuation_ratios(
    db_session: AsyncSession,
):
    """Statements are a bonus; PE/PB/yield must still land when FinMind
    is down or out of quota — the same contract the daily job keeps."""
    with patch.object(
        bf.twse, "get_all_valuation_ratios",
        AsyncMock(side_effect=lambda d: _ratios(30.0)),
    ), patch.object(
        bf, "_load_statement_payloads",
        AsyncMock(side_effect=RuntimeError("finmind quota")),
    ):
        stats = await bf.backfill(
            date(2026, 5, 11), date(2026, 5, 11), dry_run=False, force=False,
        )

    assert stats["written_sessions"] == 1
    rows = (await db_session.scalars(select(FundamentalsSnapshot))).all()
    assert len(rows) == 2
    assert all(r.payload is None for r in rows)
    assert rows[0].pe_ratio is not None
