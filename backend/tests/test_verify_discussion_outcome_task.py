"""Tests for tasks.verify_discussion_outcome — daily self-grader."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from models.discussion import Discussion, DiscussionTurn
from models.user import User, UserRole


@pytest_asyncio.fixture(autouse=True)
async def _isolate_db(db_session: AsyncSession):
    await db_session.execute(delete(DiscussionTurn))
    await db_session.execute(delete(Discussion))
    await db_session.execute(delete(User))
    await db_session.commit()
    yield


@pytest.fixture
def patch_session(db_session: AsyncSession):
    class _CM:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *exc):
            return False

    with patch("tasks.verify_discussion_outcome.AsyncSessionLocal", return_value=_CM()):
        yield


@pytest_asyncio.fixture
async def owner(db_session: AsyncSession) -> User:
    u = User(
        id=uuid.uuid4(),
        email="owner@example.com",
        hashed_password="x",
        role=UserRole.admin,
    )
    db_session.add(u)
    await db_session.commit()
    return u


def _stub_lock_helpers():
    return [
        patch("tasks.verify_discussion_outcome.acquire_lock", AsyncMock(return_value=True)),
        patch("tasks.verify_discussion_outcome.release_lock", AsyncMock()),
        patch(
            "tasks.verify_discussion_outcome.backoff_remaining_seconds", AsyncMock(return_value=0)
        ),
        patch("tasks.verify_discussion_outcome.record_health", AsyncMock()),
        patch("tasks.verify_discussion_outcome.record_failure", AsyncMock(return_value=1)),
        patch("tasks.verify_discussion_outcome.clear_failures", AsyncMock()),
        patch("tasks.verify_discussion_outcome.get_failure_count", AsyncMock(return_value=0)),
    ]


def _enter_all(patches):
    [p.__enter__() for p in patches]


def _exit_all(patches):
    for p in patches:
        p.__exit__(None, None, None)


async def _make_pending(
    db: AsyncSession,
    owner_id: uuid.UUID,
    *,
    symbols: list[str],
    created_offset_days: int = -8,
    verify_after_offset: int = -1,
    day1_open_prices: dict[str, float] | None = None,
) -> Discussion:
    """Build an auto-run discussion ready for the verifier.

    `created_at` is anchored to 04:00 UTC (= noon TW) so the UTC
    date and the Asia/Taipei date always agree. Without this,
    a CI run after 16:00 UTC ends up with `created_at.date()`
    one day BEFORE `to_tw_date(created_at)`, the verifier's
    anchor sits one day past the seeded bars' start, and the
    window comes up 1 bar short → `deferred_no_data` instead of
    a graded verdict (test failure with no actual code defect).
    """
    today = datetime.now(UTC).date()
    created_date = today + timedelta(days=created_offset_days)
    # 04:00 UTC = 12:00 TW; both UTC and TW dates resolve to
    # `created_date` regardless of when CI happens to fire.
    created_at = datetime.combine(
        created_date,
        datetime.min.time().replace(hour=4),
        tzinfo=UTC,
    )
    d = Discussion(
        id=uuid.uuid4(),
        owner_id=owner_id,
        topic="x",
        rules="y",
        persona_ids=["buffett", "lynch"],
        status="done",
        current_round=5,
        conclusion={
            "recommended_symbols": symbols,
            "reasoning": "z",
            "risks": [],
            "time_horizon": "short_term",
            "consensus_score": 0.8,
        },
        auto_run=True,
        verify_after_date=today + timedelta(days=verify_after_offset),
        day1_open_prices=day1_open_prices,
        created_at=created_at,
    )
    db.add(d)
    await db.commit()
    return d


def _bars(
    start: date,
    n: int,
    *,
    open_: float,
    closes: list[float],
) -> list[dict]:
    """Synthesize OHLCV bars starting from `start`. `closes[i]` drives
    what the 4-band classifier sees per day. `high` and `low` are
    derived around close ± 1% for completeness — the new verifier
    only reads close, so they're decorative."""
    return [
        {
            "time": (start + timedelta(days=i)).isoformat(),
            "open": open_,
            "high": closes[i] * 1.005,
            "low": closes[i] * 0.99,
            "close": closes[i],
            "volume": 10_000_000,
        }
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_win_when_peak_close_above_5pct(
    patch_session,
    db_session: AsyncSession,
    owner: User,
):
    """Any close ≥ +5% (and no day ≤ -5%) → win band."""
    d = await _make_pending(db_session, owner.id, symbols=["2330"])
    created_date = d.created_at.date()

    # open=100; D3 close=106 (+6%) — clears the 5% bar but D5 only +2%
    # so it's win, not big_win.
    bars = _bars(created_date, 5, open_=100.0, closes=[100.5, 101.0, 106.0, 103.0, 102.0])
    patches = _stub_lock_helpers() + [
        patch("services.tw_market_service.get_history", AsyncMock(return_value=bars)),
    ]
    _enter_all(patches)
    try:
        from tasks import verify_discussion_outcome

        await verify_discussion_outcome.run()
    finally:
        _exit_all(patches)

    refreshed = await db_session.get(Discussion, d.id)
    await db_session.refresh(refreshed)
    assert refreshed.verdict == "win"
    assert "2330" in (refreshed.verdict_reason or "")
    assert "peak close" in (refreshed.verdict_reason or "")
    assert refreshed.day1_open_prices == {"2330": 100.0}
    # day-5 close is the LAST bar's close — used by the frontend to
    # render `2330:100/102 (+2.0%)` in the sidebar.
    assert refreshed.day5_close_prices == {"2330": 102.0}


@pytest.mark.asyncio
async def test_big_win_when_d5_close_above_20pct(
    patch_session,
    db_session: AsyncSession,
    owner: User,
):
    """D5 close ≥ +20% (and no day ≤ -5%) → big_win band."""
    d = await _make_pending(db_session, owner.id, symbols=["2330"])
    created_date = d.created_at.date()
    bars = _bars(created_date, 5, open_=100.0, closes=[105, 110, 115, 118, 122])  # D5 +22%
    patches = _stub_lock_helpers() + [
        patch("services.tw_market_service.get_history", AsyncMock(return_value=bars)),
    ]
    _enter_all(patches)
    try:
        from tasks import verify_discussion_outcome

        await verify_discussion_outcome.run()
    finally:
        _exit_all(patches)

    refreshed = await db_session.get(Discussion, d.id)
    await db_session.refresh(refreshed)
    assert refreshed.verdict == "big_win"
    assert "D5 close" in (refreshed.verdict_reason or "")


@pytest.mark.asyncio
async def test_big_loss_overrides_big_win_priority(
    patch_session,
    db_session: AsyncSession,
    owner: User,
):
    """大敗優先: any close ≤ -5% triggers big_loss even when D5 closes
    above the big_win threshold. This is the headline behavior change
    of the 4-band rule — we want to be confident the verifier honors
    it."""
    d = await _make_pending(db_session, owner.id, symbols=["2330"])
    created_date = d.created_at.date()
    # D1 crashes -8% (triggers big_loss), then D5 rebounds to +25%
    # (would be big_win under naive precedence).
    bars = _bars(created_date, 5, open_=100.0, closes=[92, 95, 100, 110, 125])
    patches = _stub_lock_helpers() + [
        patch("services.tw_market_service.get_history", AsyncMock(return_value=bars)),
    ]
    _enter_all(patches)
    try:
        from tasks import verify_discussion_outcome

        await verify_discussion_outcome.run()
    finally:
        _exit_all(patches)

    refreshed = await db_session.get(Discussion, d.id)
    await db_session.refresh(refreshed)
    assert refreshed.verdict == "big_loss"
    assert "trough close" in (refreshed.verdict_reason or "")


@pytest.mark.asyncio
async def test_loss_when_no_threshold_crossed(
    patch_session,
    db_session: AsyncSession,
    owner: User,
):
    """No close crosses ±5% → loss band."""
    d = await _make_pending(db_session, owner.id, symbols=["2454"])
    created_date = d.created_at.date()
    bars = _bars(created_date, 5, open_=200.0, closes=[201, 202, 203, 202.5, 201.5])
    patches = _stub_lock_helpers() + [
        patch("services.tw_market_service.get_history", AsyncMock(return_value=bars)),
    ]
    _enter_all(patches)
    try:
        from tasks import verify_discussion_outcome

        await verify_discussion_outcome.run()
    finally:
        _exit_all(patches)

    refreshed = await db_session.get(Discussion, d.id)
    await db_session.refresh(refreshed)
    assert refreshed.verdict == "loss"
    assert "no band crossed" in (refreshed.verdict_reason or "")


@pytest.mark.asyncio
async def test_any_symbol_can_trigger_win(
    patch_session,
    db_session: AsyncSession,
    owner: User,
):
    """Three symbols, only the second crosses +5% peak close. Verdict='win',
    reason names the winning symbol."""
    d = await _make_pending(db_session, owner.id, symbols=["1101", "2330", "2454"])
    created_date = d.created_at.date()

    by_sym = {
        "1101": _bars(created_date, 5, open_=50.0, closes=[50.2, 50.5, 50.3, 50.1, 50.0]),
        "2330": _bars(
            created_date, 5, open_=600.0, closes=[610, 620, 635, 625, 620]
        ),  # peak +5.83%
        "2454": _bars(created_date, 5, open_=900.0, closes=[902, 903, 901, 900, 899]),
    }

    async def _fake_history(symbol, **_):
        return by_sym.get(symbol, [])

    patches = _stub_lock_helpers() + [
        patch("services.tw_market_service.get_history", AsyncMock(side_effect=_fake_history)),
    ]
    _enter_all(patches)
    try:
        from tasks import verify_discussion_outcome

        await verify_discussion_outcome.run()
    finally:
        _exit_all(patches)

    refreshed = await db_session.get(Discussion, d.id)
    await db_session.refresh(refreshed)
    assert refreshed.verdict == "win"
    assert "2330" in (refreshed.verdict_reason or "")


@pytest.mark.asyncio
async def test_unverifiable_when_no_symbols(
    patch_session,
    db_session: AsyncSession,
    owner: User,
):
    """Synthesizer returned empty `recommended_symbols` — verdict=
    unverifiable, set immediately, no history fetch attempted."""
    d = await _make_pending(db_session, owner.id, symbols=[])
    history = AsyncMock()

    patches = _stub_lock_helpers() + [
        patch("services.tw_market_service.get_history", history),
    ]
    _enter_all(patches)
    try:
        from tasks import verify_discussion_outcome

        await verify_discussion_outcome.run()
    finally:
        _exit_all(patches)

    refreshed = await db_session.get(Discussion, d.id)
    await db_session.refresh(refreshed)
    assert refreshed.verdict == "unverifiable"
    history.assert_not_called()


@pytest.mark.asyncio
async def test_defers_when_no_bars_yet(
    patch_session,
    db_session: AsyncSession,
    owner: User,
):
    """Symbol returns no bars (delisting / data not yet ingested) →
    verdict stays NULL so the next cycle retries."""
    d = await _make_pending(db_session, owner.id, symbols=["9999"])
    patches = _stub_lock_helpers() + [
        patch("services.tw_market_service.get_history", AsyncMock(return_value=[])),
    ]
    _enter_all(patches)
    try:
        from tasks import verify_discussion_outcome

        await verify_discussion_outcome.run()
    finally:
        _exit_all(patches)

    refreshed = await db_session.get(Discussion, d.id)
    await db_session.refresh(refreshed)
    assert refreshed.verdict is None


@pytest.mark.asyncio
async def test_unverifiable_after_stale_grace_with_no_bars(
    patch_session,
    db_session: AsyncSession,
    owner: User,
):
    """PR #223 stale-grace: a row whose `verify_after_date` is more
    than `_STALE_GRACE_DAYS` past (no bars ever resolved) gets
    verdict='unverifiable' instead of deferring forever. Stops the
    cron from burning OHLCV-fetch quota on permanently-stuck rows
    (delisted symbols, malformed codes, persistent connector
    outages)."""
    from tasks import verify_discussion_outcome as vdo

    today = datetime.now(UTC).date()

    # `verify_after_offset` measured back from today. Set it to
    # (-grace - 1) so today - verify_after_date = grace + 1 (just
    # past the cap).
    d = await _make_pending(
        db_session,
        owner.id,
        symbols=["9999"],
        verify_after_offset=-(vdo._STALE_GRACE_DAYS + 1),
    )
    patches = _stub_lock_helpers() + [
        patch("services.tw_market_service.get_history", AsyncMock(return_value=[])),
    ]
    _enter_all(patches)
    try:
        await vdo.run()
    finally:
        _exit_all(patches)

    refreshed = await db_session.get(Discussion, d.id)
    await db_session.refresh(refreshed)
    assert refreshed.verdict == "unverifiable"
    assert "no OHLCV" in (refreshed.verdict_reason or "")
    # Sanity that the stale-grace was the trigger (not the no-symbols
    # path which would also write 'unverifiable' but with a different
    # reason).
    assert "delisted" in (refreshed.verdict_reason or "")
    # Day fields stay None — there were never any bars to capture.
    _ = today  # keeps the import live for clarity; not asserted.


@pytest.mark.asyncio
async def test_defers_when_window_under_5_bars(
    patch_session,
    db_session: AsyncSession,
    owner: User,
):
    """Holiday in the middle of the 5-day window → only 4 bars exist
    when the verifier runs. Must defer (verdict=NULL) so the 5th
    trading-day's bar can land before grading. Pinning this prevents
    a regression where the verifier graded a partial window and locked
    in a wrong answer."""
    d = await _make_pending(db_session, owner.id, symbols=["2330"])
    bars = _bars(d.created_at.date(), 4, open_=100.0, closes=[100.5, 101.5, 102.5, 103.5])

    patches = _stub_lock_helpers() + [
        patch("services.tw_market_service.get_history", AsyncMock(return_value=bars)),
    ]
    _enter_all(patches)
    try:
        from tasks import verify_discussion_outcome

        await verify_discussion_outcome.run()
    finally:
        _exit_all(patches)

    refreshed = await db_session.get(Discussion, d.id)
    await db_session.refresh(refreshed)
    assert refreshed.verdict is None
    assert refreshed.day1_open_prices == {"2330": 100.0}
    assert refreshed.daily_close_prices == {"2330": [100.5, 101.5, 102.5, 103.5, None]}
    assert refreshed.day5_close_prices is None


@pytest.mark.asyncio
async def test_only_processes_pending_rows(
    patch_session,
    db_session: AsyncSession,
    owner: User,
):
    """Verifier should skip:
       - already-verdict'd rows (verdict is final)
       - rows whose verify_after_date is in the future

    Should grade BOTH auto_run AND manual rows (PR #218 dropped the
    `auto_run=True` filter so manual discussions also feed the
    `prior_discussions.verdict` cross-session memory field).
    """
    today = datetime.now(UTC).date()

    # Eligible (auto-run): due, no verdict
    eligible_auto = await _make_pending(db_session, owner.id, symbols=["2330"])

    # Eligible (manual) — auto_run=False but otherwise valid; PR #218
    # dropped the auto_run filter so this row ALSO gets graded. Pin
    # `created_at` to the same window as the auto-run row so the
    # mocked bars (keyed off that date) line up for both.
    eligible_manual = Discussion(
        id=uuid.uuid4(),
        owner_id=owner.id,
        topic="x",
        rules="y",
        persona_ids=["buffett", "lynch"],
        status="done",
        current_round=5,
        conclusion={"recommended_symbols": ["2330"]},
        auto_run=False,
        verify_after_date=today - timedelta(days=1),
        created_at=eligible_auto.created_at,
    )
    # Already graded
    already_graded = Discussion(
        id=uuid.uuid4(),
        owner_id=owner.id,
        topic="x",
        rules="y",
        persona_ids=["buffett", "lynch"],
        status="done",
        current_round=5,
        conclusion={"recommended_symbols": ["2330"]},
        auto_run=True,
        verdict="win",
        verdict_reason="prior",
        verify_after_date=today - timedelta(days=2),
    )
    # Future verify_after_date
    future = Discussion(
        id=uuid.uuid4(),
        owner_id=owner.id,
        topic="x",
        rules="y",
        persona_ids=["buffett", "lynch"],
        status="done",
        current_round=5,
        conclusion={"recommended_symbols": ["2330"]},
        auto_run=True,
        verify_after_date=today + timedelta(days=5),
    )
    db_session.add_all([eligible_manual, already_graded, future])
    await db_session.commit()

    bars = _bars(
        eligible_auto.created_at.date(), 5, open_=100.0, closes=[101, 102, 106, 103, 102]
    )  # peak close +6%
    patches = _stub_lock_helpers() + [
        patch("services.tw_market_service.get_history", AsyncMock(return_value=bars)),
    ]
    _enter_all(patches)
    try:
        from tasks import verify_discussion_outcome

        await verify_discussion_outcome.run()
    finally:
        _exit_all(patches)

    # Both eligible rows (auto-run + manual) got graded.
    for d in (eligible_auto, eligible_manual):
        r = await db_session.get(Discussion, d.id)
        await db_session.refresh(r)
        assert r.verdict == "win", f"{d.id} should have been graded"

    # already_graded keeps its prior verdict; future stays NULL.
    r = await db_session.get(Discussion, already_graded.id)
    await db_session.refresh(r)
    assert r.verdict == "win"
    assert r.verdict_reason == "prior"

    r = await db_session.get(Discussion, future.id)
    await db_session.refresh(r)
    assert r.verdict is None


@pytest.mark.asyncio
async def test_lazy_day1_open_snapshot(
    patch_session,
    db_session: AsyncSession,
    owner: User,
):
    """Verifier captures day1_open from history bars when the column is
    NULL (auto-run task doesn't populate it eagerly)."""
    d = await _make_pending(
        db_session,
        owner.id,
        symbols=["2330"],
        day1_open_prices=None,
    )
    bars = _bars(
        d.created_at.date(), 5, open_=120.0, closes=[121, 122, 127, 124, 124]
    )  # peak close +5.8%
    patches = _stub_lock_helpers() + [
        patch("services.tw_market_service.get_history", AsyncMock(return_value=bars)),
    ]
    _enter_all(patches)
    try:
        from tasks import verify_discussion_outcome

        await verify_discussion_outcome.run()
    finally:
        _exit_all(patches)

    refreshed = await db_session.get(Discussion, d.id)
    await db_session.refresh(refreshed)
    assert refreshed.verdict == "win"
    assert refreshed.day1_open_prices == {"2330": 120.0}


@pytest.mark.asyncio
async def test_filters_non_numeric_symbols(
    patch_session,
    db_session: AsyncSession,
    owner: User,
):
    """A malformed symbol like 'AAPL' is filtered out; remaining valid
    symbol drives the verdict."""
    d = await _make_pending(
        db_session,
        owner.id,
        symbols=["AAPL", "2330", "abc"],
    )
    history_calls: list[str] = []

    async def _fake_history(symbol, **_):
        history_calls.append(symbol)
        return _bars(
            d.created_at.date(), 5, open_=100.0, closes=[101, 102, 106, 103, 102]
        )  # peak close +6%

    patches = _stub_lock_helpers() + [
        patch("services.tw_market_service.get_history", AsyncMock(side_effect=_fake_history)),
    ]
    _enter_all(patches)
    try:
        from tasks import verify_discussion_outcome

        await verify_discussion_outcome.run()
    finally:
        _exit_all(patches)

    refreshed = await db_session.get(Discussion, d.id)
    await db_session.refresh(refreshed)
    assert refreshed.verdict == "win"
    # Verifier only fetched history for the valid TW symbol
    assert history_calls == ["2330"]


@pytest.mark.asyncio
async def test_pool_performance_computed_on_verdict(
    patch_session,
    db_session: AsyncSession,
    owner: User,
):
    """A verified seq-1 row with a stored candidate pool gets a
    pool_performance snapshot computed from the local OHLCV archive:
    +10% and -10% legs average to 0%."""
    d = await _make_pending(db_session, owner.id, symbols=["2330"])
    d.candidate_snapshot = {
        "strategy": "general", "sequence": 1, "candidates": [],
        "pool": [
            {"symbol": "1101", "strategy_score": 2.0},
            {"symbol": "1102", "strategy_score": 1.0},
            {"symbol": "not-a-symbol", "strategy_score": 0.5},
        ],
    }
    await db_session.commit()
    created_date = d.created_at.date()

    pick_bars = _bars(created_date, 5, open_=100.0, closes=[106.0, 106.0, 106.0, 103.0, 102.0])
    pool_bars = {
        "1101": _bars(created_date, 5, open_=100.0, closes=[101, 102, 104, 108, 110.0]),
        "1102": _bars(created_date, 5, open_=100.0, closes=[99, 97, 95, 92, 90.0]),
    }

    async def _fake_archive(_market, sym, _start, _end):
        return pool_bars.get(sym, [])

    patches = _stub_lock_helpers() + [
        patch("services.tw_market_service.get_history",
              AsyncMock(return_value=pick_bars)),
        patch("services.ingest.repository.read_ohlcv_range_autosession",
              AsyncMock(side_effect=_fake_archive)),
    ]
    _enter_all(patches)
    try:
        from tasks import verify_discussion_outcome

        await verify_discussion_outcome.run()
    finally:
        _exit_all(patches)

    refreshed = await db_session.get(Discussion, d.id)
    await db_session.refresh(refreshed)
    assert refreshed.verdict == "win"
    assert refreshed.pool_performance == {
        "avg_return_pct": 0.0, "resolved": 2, "pool_size": 2,
    }


@pytest.mark.asyncio
async def test_future_dated_rows_stay_untouched(
    patch_session,
    db_session: AsyncSession,
    owner: User,
):
    """Rows whose verify_after_date is still in the future are bounded
    out in SQL — no bar fetches, no partial progress writes."""
    d = await _make_pending(
        db_session, owner.id, symbols=["2330"], verify_after_offset=3,
    )
    history = AsyncMock(return_value=[])
    patches = _stub_lock_helpers() + [
        patch("services.tw_market_service.get_history", history),
    ]
    _enter_all(patches)
    try:
        from tasks import verify_discussion_outcome

        await verify_discussion_outcome.run()
    finally:
        _exit_all(patches)

    history.assert_not_awaited()
    refreshed = await db_session.get(Discussion, d.id)
    await db_session.refresh(refreshed)
    assert refreshed.verdict is None


@pytest.mark.asyncio
async def test_pool_performance_absent_without_pool(
    patch_session,
    db_session: AsyncSession,
    owner: User,
):
    """No stored pool → pool_performance stays NULL (legacy rows)."""
    d = await _make_pending(db_session, owner.id, symbols=["2330"])
    created_date = d.created_at.date()
    bars = _bars(created_date, 5, open_=100.0, closes=[106.0, 106.0, 106.0, 103.0, 102.0])
    patches = _stub_lock_helpers() + [
        patch("services.tw_market_service.get_history", AsyncMock(return_value=bars)),
    ]
    _enter_all(patches)
    try:
        from tasks import verify_discussion_outcome

        await verify_discussion_outcome.run()
    finally:
        _exit_all(patches)

    refreshed = await db_session.get(Discussion, d.id)
    await db_session.refresh(refreshed)
    assert refreshed.verdict == "win"
    assert refreshed.pool_performance is None
