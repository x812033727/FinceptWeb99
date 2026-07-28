"""Tests for tasks.extend_daily_closes (the D10 reference lens feeder).

D5 stays the primary verdict window; this task only lengthens stored
`daily_close_prices` arrays (append-only) once the archive holds the
full 10 sessions from a decided row's anchor.
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.discussion import Discussion
from models.ohlcv_daily import OhlcvDaily
from models.user import User, UserRole
from tasks.extend_daily_closes import TARGET_DAYS, _do_run, extend_arrays


# ── pure append step ─────────────────────────────────────────────

def test_extend_appends_only_the_missing_tail():
    daily = {"2330": [100.0, 101.0, 102.0, 103.0, 104.0]}
    closes = {"2330": [100.0, 101.0, 102.0, 103.0, 104.0,
                       105.0, 106.0, 107.0, 108.0, 109.0]}
    out = extend_arrays(daily, closes)
    assert out == {"2330": [100.0, 101.0, 102.0, 103.0, 104.0,
                            105.0, 106.0, 107.0, 108.0, 109.0]}
    # Existing entries are copied verbatim, even where the archive
    # now disagrees (corrections never rewrite the stored baseline).
    daily2 = {"2330": [999.0, 999.0, 999.0, 999.0, 999.0]}
    out2 = extend_arrays(daily2, closes)
    assert out2["2330"][:5] == [999.0] * 5
    assert out2["2330"][5:] == [105.0, 106.0, 107.0, 108.0, 109.0]


def test_extend_noop_when_already_at_target_or_immature():
    full = {"2330": [float(i) for i in range(TARGET_DAYS)]}
    assert extend_arrays(full, {"2330": [float(i) for i in range(12)]}) is None
    # Archive has only 8 of the 10 sessions → wait for maturity.
    short = {"2330": [100.0] * 5}
    assert extend_arrays(short, {"2330": [100.0] * 8}) is None


def test_extend_partial_coverage_extends_only_mature_symbols():
    daily = {"2330": [100.0] * 5, "1101": [50.0] * 5}
    closes = {"2330": [float(100 + i) for i in range(10)], "1101": [50.0] * 7}
    out = extend_arrays(daily, closes)
    assert len(out["2330"]) == TARGET_DAYS
    assert out["1101"] == [50.0] * 5  # untouched, retried tomorrow


# ── DB pass ──────────────────────────────────────────────────────

@pytest.fixture
def patch_session(db_session: AsyncSession):
    class _CM:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *exc):
            return False

    with patch(
        "tasks.extend_daily_closes.AsyncSessionLocal",
        return_value=_CM(),
    ):
        yield


async def _seed(db, *, verdict="win", days_in_archive=10, entries=5,
                age_days=20, symbol="2330"):
    u = User(id=uuid.uuid4(), email=f"d10-{uuid.uuid4().hex[:8]}@example.com",
             hashed_password="x", role=UserRole.viewer)
    db.add(u)
    await db.flush()
    created = datetime.now(UTC) - timedelta(days=age_days)
    anchor = date(2026, 7, 1)
    d = Discussion(
        id=uuid.uuid4(), owner_id=u.id, topic="t", rules="r",
        persona_ids=["buffett"], market="TW", status="done",
        current_round=5,
        conclusion={"recommended_symbols": [symbol]},
        verdict=verdict,
        as_of_date=anchor,  # fixed-date anchor — no wall-clock windows
        daily_close_prices={symbol: [float(100 + i) for i in range(entries)]},
        created_at=created, updated_at=created,
    )
    db.add(d)
    for i in range(days_in_archive):
        db.add(OhlcvDaily(
            market="TW", symbol=symbol, ts=date(2026, 7, 1 + i),
            open=100.0 + i, high=101.0 + i, low=99.0 + i,
            close=100.0 + i, volume=0, source="test",
        ))
    await db.commit()
    return d


@pytest.mark.asyncio
async def test_do_run_extends_decided_row_and_is_idempotent(
    patch_session, db_session,
):
    d = await _seed(db_session)
    assert await _do_run() == 1
    await db_session.refresh(d)
    assert d.daily_close_prices["2330"] == [float(100 + i) for i in range(10)]
    # Second run: nothing left to extend.
    assert await _do_run() == 0


@pytest.mark.asyncio
async def test_do_run_skips_undecided_and_immature_rows(
    patch_session, db_session,
):
    await _seed(db_session, verdict="abstain")
    assert await _do_run() == 0

    d = await _seed(db_session, days_in_archive=8, symbol="1101")
    # A decided row whose archive lacks the full 10 sessions waits.
    assert await _do_run() == 0
    await db_session.refresh(d)
    assert len(d.daily_close_prices["1101"]) == 5


def test_scheduler_registers_the_job():
    from tasks.scheduler import scheduler, setup_jobs

    with patch.object(scheduler, "add_job") as add_job:
        setup_jobs()
    jobs = {
        call.kwargs["id"]: call
        for call in add_job.call_args_list if "id" in call.kwargs
    }
    assert "extend_daily_closes" in jobs
    trigger = jobs["extend_daily_closes"].kwargs["trigger"]
    trig = str(trigger)
    assert "hour='12'" in trig and "minute='10'" in trig, trig
    assert str(trigger.timezone) == "UTC"


@pytest.mark.asyncio
async def test_body_reports_idle_and_extended_counts():
    from tasks import extend_daily_closes as mod

    with patch.object(mod, "_do_run", return_value=0):
        outcome = await mod._body()
    assert outcome.row_count == 0
    assert outcome.status == "idle: nothing to extend"

    with patch.object(mod, "_do_run", return_value=3):
        outcome = await mod._body()
    assert outcome.row_count == 3
    assert outcome.status is None

    assert mod._format_error(RuntimeError("boom")) == "boom"


@pytest.mark.asyncio
async def test_run_wires_the_shared_runner(patch_session, db_session):
    """run() goes through the shared lock/backoff/health skeleton and
    records a health row off the body's outcome."""
    from unittest.mock import AsyncMock

    from tasks import extend_daily_closes as mod

    with patch.object(mod, "acquire_lock", AsyncMock(return_value=True)), \
         patch.object(mod, "release_lock", AsyncMock()) as release, \
         patch.object(mod, "backoff_remaining_seconds", AsyncMock(return_value=0)), \
         patch.object(mod, "record_health", AsyncMock()) as health, \
         patch.object(mod, "clear_failures", AsyncMock()):
        await mod.run()
    health.assert_awaited_once()
    assert health.await_args.kwargs.get("ok") is True
    release.assert_awaited_once()


@pytest.mark.asyncio
async def test_do_run_skips_rows_with_empty_or_malformed_daily(
    patch_session, db_session,
):
    d = await _seed(db_session, symbol="3231")
    d.daily_close_prices = {}
    await db_session.commit()
    assert await _do_run() == 0
