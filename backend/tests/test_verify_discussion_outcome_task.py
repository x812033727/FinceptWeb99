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
    abstained: bool = False,
    abstain_reason: str = "",
    market: str = "TW",
    as_of_date: date | None = None,
    candidate_snapshot: dict | None = None,
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
        market=market,
        conclusion={
            "recommended_symbols": symbols,
            "reasoning": "z",
            "risks": [],
            "time_horizon": "short_term",
            "consensus_score": 0.8,
            "abstained": abstained,
            "abstain_reason": abstain_reason,
        },
        auto_run=True,
        verify_after_date=today + timedelta(days=verify_after_offset),
        day1_open_prices=day1_open_prices,
        created_at=created_at,
        as_of_date=as_of_date,
        candidate_snapshot=candidate_snapshot,
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
    """D5 close ≥ +5% (and no day ≤ -5%) → win band."""
    d = await _make_pending(db_session, owner.id, symbols=["2330"])
    created_date = d.created_at.date()

    # open=100; the gain holds to D5 (+6%) — that is what a reader
    # following the 5-day call actually realizes.
    bars = _bars(created_date, 5, open_=100.0, closes=[100.5, 101.0, 106.0, 105.0, 106.0])
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
    assert "D5 close" in (refreshed.verdict_reason or "")
    assert refreshed.day1_open_prices == {"2330": 100.0}
    # day-5 close is the LAST bar's close — used by the frontend to
    # render `2330:100/106 (+6.0%)` in the sidebar.
    assert refreshed.day5_close_prices == {"2330": 106.0}


@pytest.mark.asyncio
async def test_spike_that_fades_by_d5_is_a_loss(
    patch_session,
    db_session: AsyncSession,
    owner: User,
):
    """Peak +6% at D3 but D5 only +2% → loss, not win.

    The old peak-touch rule scored this as a win; it is the single
    biggest reason the public scoreboard read more optimistic than the
    picks it was summarizing.
    """
    d = await _make_pending(db_session, owner.id, symbols=["2330"])
    created_date = d.created_at.date()
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
    assert refreshed.verdict == "loss"
    # The peak is still reported for context, it just doesn't decide.
    assert "期間最高" in (refreshed.verdict_reason or "")


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
    """Three symbols, only the second holds ≥ +5% at D5. Verdict='win',
    reason names the winning symbol."""
    d = await _make_pending(db_session, owner.id, symbols=["1101", "2330", "2454"])
    created_date = d.created_at.date()

    by_sym = {
        "1101": _bars(created_date, 5, open_=50.0, closes=[50.2, 50.5, 50.3, 50.1, 50.0]),
        "2330": _bars(
            created_date, 5, open_=600.0, closes=[610, 620, 635, 632, 636]
        ),  # D5 +6.0%
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
async def test_abstain_when_panel_declined(
    patch_session,
    db_session: AsyncSession,
    owner: User,
):
    """Empty symbols WITH the structured `abstained` flag is a decision,
    not a failure — verdict='abstain' so the scoreboard can keep it out
    of the win-rate denominator instead of burying it in unverifiable."""
    d = await _make_pending(
        db_session, owner.id, symbols=[],
        abstained=True, abstain_reason="候選股皆未過量能確認門檻",
    )
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
    assert refreshed.verdict == "abstain"
    assert "候選股皆未過量能確認門檻" in (refreshed.verdict_reason or "")
    history.assert_not_called()


@pytest.mark.asyncio
async def test_abstain_flag_ignored_when_symbols_present(
    patch_session,
    db_session: AsyncSession,
    owner: User,
):
    """A row that recommends symbols is never an abstention, whatever
    the flag says — the picks are what gets graded."""
    d = await _make_pending(
        db_session, owner.id, symbols=["2330"], abstained=True,
    )
    created_date = d.created_at.date()
    bars = _bars(created_date, 5, open_=100.0, closes=[101, 102, 106, 105, 106])
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
        eligible_auto.created_at.date(), 5, open_=100.0, closes=[101, 102, 106, 105, 106]
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
        d.created_at.date(), 5, open_=120.0, closes=[121, 122, 127, 126, 127]
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
            d.created_at.date(), 5, open_=100.0, closes=[101, 102, 106, 105, 106]
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

    pick_bars = _bars(created_date, 5, open_=100.0, closes=[106.0, 106.0, 106.0, 105.0, 106.0])
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
    bars = _bars(created_date, 5, open_=100.0, closes=[106.0, 106.0, 106.0, 105.0, 106.0])
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


# ── WS1 finding A: US / GLOBAL discussions were permanently mislabeled
# `unverifiable` because the verifier filtered every recommended symbol
# through a TW-only `^\d{4,6}$` regex. "AAPL" never matched, so
# `symbols` came out empty and the row was graded as if the panel had
# produced nothing usable. The tests below pin the market-aware fix:
# US symbols survive the filter, route through `us_market_service`
# (not `tw_market_service`), and grade normally; junk still gets
# rejected; TW rows (every test above, run with `market` defaulting to
# "TW") are untouched.


@pytest.mark.asyncio
async def test_us_discussion_symbols_not_stripped(
    patch_session,
    db_session: AsyncSession,
    owner: User,
):
    """A US discussion recommending ["AAPL"] must reach the history
    fetch with "AAPL" intact — the old TW-only regex stripped it to
    `[]` before any fetch was attempted, so the row above would have
    been misclassified as if the synthesizer had produced nothing.
    No bars yet (empty history) => verdict stays NULL (deferred), NOT
    'unverifiable' — proof the row was scheduled for grading rather
    than short-circuited by the no-symbols path."""
    d = await _make_pending(db_session, owner.id, symbols=["AAPL"], market="US")
    history_calls: list[str] = []

    async def _fake_history(symbol, **_):
        history_calls.append(symbol)
        return []

    patches = _stub_lock_helpers() + [
        patch("services.us_market_service.get_history", AsyncMock(side_effect=_fake_history)),
        patch("services.tw_market_service.get_history", AsyncMock()),
    ]
    _enter_all(patches)
    try:
        from tasks import verify_discussion_outcome

        await verify_discussion_outcome.run()
    finally:
        _exit_all(patches)

    assert history_calls == ["AAPL"]
    refreshed = await db_session.get(Discussion, d.id)
    await db_session.refresh(refreshed)
    assert refreshed.verdict is None


@pytest.mark.asyncio
async def test_us_discussion_grades_win_via_us_market_service(
    patch_session,
    db_session: AsyncSession,
    owner: User,
):
    """Grading a US discussion with mocked `us_market_service` history
    (+6% D5 close, same shape as `test_win_when_peak_close_above_5pct`)
    yields a decided 'win' verdict — not 'unverifiable'. TW's
    `tw_market_service.get_history` must never be called for a US row.
    `pool_performance` stays None: the candidate pool / benchmark
    tables are TW-only screener artifacts, so a US row can't fabricate
    a comparison against them."""
    d = await _make_pending(db_session, owner.id, symbols=["AAPL"], market="US")
    created_date = d.created_at.date()
    bars = _bars(created_date, 5, open_=100.0, closes=[100.5, 101.0, 106.0, 105.0, 106.0])

    tw_history = AsyncMock()
    patches = _stub_lock_helpers() + [
        patch("services.us_market_service.get_history", AsyncMock(return_value=bars)),
        patch("services.tw_market_service.get_history", tw_history),
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
    assert "AAPL" in (refreshed.verdict_reason or "")
    assert refreshed.day1_open_prices == {"AAPL": 100.0}
    assert refreshed.day5_close_prices == {"AAPL": 106.0}
    assert refreshed.pool_performance is None
    tw_history.assert_not_called()


@pytest.mark.asyncio
async def test_us_junk_symbols_still_filtered(
    patch_session,
    db_session: AsyncSession,
    owner: User,
):
    """The US ticker shape is deliberately conservative: a SQL-injection-
    shaped string and lowercase noise must still be filtered out, same
    as `test_filters_non_numeric_symbols` does for TW junk. No valid
    symbols survive => 'unverifiable', no history fetch attempted."""
    d = await _make_pending(
        db_session, owner.id, symbols=["DROP TABLE", "aapl"], market="US",
    )
    history = AsyncMock()

    patches = _stub_lock_helpers() + [
        patch("services.us_market_service.get_history", history),
        patch("services.tw_market_service.get_history", AsyncMock()),
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
    assert refreshed.verdict_reason == "synthesizer returned no symbols"
    history.assert_not_called()


@pytest.mark.asyncio
async def test_backtest_row_enters_strictly_after_as_of(
    patch_session,
    db_session: AsyncSession,
    owner: User,
):
    """Look-ahead regression (2026-08-04): a backtest row's holding
    window must start at the first bar strictly AFTER `as_of_date`.
    `load_candidate_rows` clamps the screener to `ts <= as_of`, so the
    pool already knows as_of's close — grading entry at as_of's open
    would buy a morning price with evening knowledge. Here the as_of
    bar rallies 100→110; graded from as_of open that spike alone is a
    +10% "win". Honest entry is the NEXT session (open 110, flat to
    D5) — a loss."""
    as_of = datetime.now(UTC).date() - timedelta(days=30)
    d = await _make_pending(
        db_session, owner.id, symbols=["2330"], as_of_date=as_of,
    )

    # Bar ON as_of (the pumped session the screener saw) + 5 flat
    # bars after it. The stub ignores the requested range, so only
    # the strict `> as_of` filter can exclude the as_of bar.
    as_of_bar = _bars(as_of, 1, open_=100.0, closes=[110.0])
    after_bars = _bars(
        as_of + timedelta(days=1), 5, open_=110.0,
        closes=[110.0, 110.0, 110.0, 110.0, 110.0],
    )
    archive = AsyncMock(return_value=as_of_bar + after_bars)
    patches = _stub_lock_helpers() + [
        patch("services.ingest.repository.read_ohlcv_range_autosession", archive),
    ]
    _enter_all(patches)
    try:
        from tasks import verify_discussion_outcome

        await verify_discussion_outcome.run()
    finally:
        _exit_all(patches)

    refreshed = await db_session.get(Discussion, d.id)
    await db_session.refresh(refreshed)
    # Entry at 110 (first bar after as_of), D5 close 110 → 0% → loss.
    # Under the buggy `>= as_of` anchor this graded +10% → win.
    assert refreshed.verdict == "loss", refreshed.verdict_reason
    assert refreshed.day1_open_prices == {"2330": 110.0}


@pytest.mark.asyncio
async def test_backtest_abstain_pool_counterfactual_enters_after_as_of(
    patch_session,
    db_session: AsyncSession,
    owner: User,
):
    """The abstain-path pool counterfactual must use the same honest
    anchor as pick grading: entry strictly after `as_of_date`. The
    as_of bar jumps 100→110; five flat bars follow. Honest pool
    return is 0%, not +10%."""
    as_of = datetime.now(UTC).date() - timedelta(days=30)
    d = await _make_pending(
        db_session, owner.id, symbols=[], abstained=True,
        abstain_reason="風報比不足", as_of_date=as_of,
        candidate_snapshot={"pool": [{"symbol": "2330"}]},
    )

    as_of_bar = _bars(as_of, 1, open_=100.0, closes=[110.0])
    after_bars = _bars(
        as_of + timedelta(days=1), 5, open_=110.0,
        closes=[110.0, 110.0, 110.0, 110.0, 110.0],
    )
    archive = AsyncMock(return_value=as_of_bar + after_bars)
    patches = _stub_lock_helpers() + [
        patch("services.ingest.repository.read_ohlcv_range_autosession", archive),
    ]
    _enter_all(patches)
    try:
        from tasks import verify_discussion_outcome

        await verify_discussion_outcome.run()
    finally:
        _exit_all(patches)

    refreshed = await db_session.get(Discussion, d.id)
    await db_session.refresh(refreshed)
    assert refreshed.verdict == "abstain"
    perf = refreshed.pool_performance or {}
    assert perf.get("resolved") == 1
    assert perf.get("avg_return_pct") == 0.0
