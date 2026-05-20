"""Tests for services.discussion_scoreboard_service.

Builds Discussion + OhlcvDaily fixtures in the test SQLite session
and verifies the per-symbol D1-D5 close + change % computation
end-to-end. Persisted-vs-on-demand semantics live here too —
`compute_scoreboard` is read-only, `persist_scoreboard` writes the
JSON column atomically.
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.discussion import Discussion
from models.user import User, UserRole
from services import discussion_scoreboard_service
from services.ingest.repository import OhlcvBar, upsert_ohlcv_bars


async def _make_user(db: AsyncSession, email: str = "scorer@example.com") -> User:
    u = User(
        id=uuid.uuid4(),
        email=email,
        hashed_password="x",
        role=UserRole.viewer,
    )
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


async def _make_discussion(
    db: AsyncSession,
    *,
    owner_id: uuid.UUID,
    created_at: datetime,
    recommended: list[str] | None,
    day1_open_prices: dict[str, float] | None = None,
    as_of_date: date | None = None,
) -> Discussion:
    d = Discussion(
        id=uuid.uuid4(),
        owner_id=owner_id,
        topic="t", rules="r",
        persona_ids=["buffett", "lynch"],
        status="done",
        current_round=1,
        conclusion=(
            None if recommended is None
            else {
                "recommended_symbols": recommended,
                "reasoning": "x", "risks": [],
                "time_horizon": "short_term", "consensus_score": 0.5,
            }
        ),
        day1_open_prices=day1_open_prices,
        as_of_date=as_of_date,
        created_at=created_at,
        updated_at=created_at,
    )
    db.add(d)
    await db.commit()
    await db.refresh(d)
    return d


async def _seed_bars(
    db: AsyncSession, symbol: str, *, start: date, closes: list[float],
    opens: list[float] | None = None,
) -> None:
    """Seed `len(closes)` consecutive daily bars starting at `start`."""
    opens = opens if opens is not None else closes  # default open=close
    bars = [
        OhlcvBar(
            market="TW", symbol=symbol,
            ts=start + timedelta(days=i),
            open=opens[i], high=closes[i] + 1, low=closes[i] - 1,
            close=closes[i], volume=1000,
            source="test",
        )
        for i in range(len(closes))
    ]
    await upsert_ohlcv_bars(db, bars)


# ── compute_scoreboard ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compute_scoreboard_full_window(db_session: AsyncSession):
    """5 consecutive bars from creation date → all 5 days resolved,
    change %s computed against day-1 open."""
    user = await _make_user(db_session)
    created = datetime(2026, 4, 27, 6, 0, tzinfo=UTC)  # Mon TW 14:00
    d = await _make_discussion(
        db_session,
        owner_id=user.id, created_at=created,
        recommended=["2330"],
    )
    # Live mode: anchor = TW-local creation date.
    # Both `anchor_date` and `created_at_tw_date` should match.
    # day1 open=600, closes climb 600→610
    await _seed_bars(
        db_session, "2330", start=date(2026, 4, 27),
        closes=[602, 605, 607, 609, 610],
        opens=[600, 603, 605, 607, 609],
    )

    result = await discussion_scoreboard_service.compute_scoreboard(
        db_session, d,
    )
    assert result["anchor_date"] == "2026-04-27"
    assert result["created_at_tw_date"] == "2026-04-27"
    rows = result["rows"]
    assert len(rows) == 1
    r = rows[0]
    assert r["symbol"] == "2330"
    assert r["day1_open"] == 600
    assert r["daily_closes"] == [602, 605, 607, 609, 610]
    # (close - 600) / 600 rounded to 6 decimals
    assert r["change_pcts"] == [
        round((602 - 600) / 600, 6),
        round((605 - 600) / 600, 6),
        round((607 - 600) / 600, 6),
        round((609 - 600) / 600, 6),
        round((610 - 600) / 600, 6),
    ]
    assert r["days_resolved"] == 5


@pytest.mark.asyncio
async def test_compute_scoreboard_partial_window(db_session: AsyncSession):
    """Only 3 bars exist yet — last two closes are None, change_pcts
    too. days_resolved=3 lets the cron know to retry tomorrow."""
    user = await _make_user(db_session, "scorer-partial@example.com")
    created = datetime(2026, 4, 27, 6, 0, tzinfo=UTC)
    d = await _make_discussion(
        db_session,
        owner_id=user.id, created_at=created,
        recommended=["2454"],
    )
    await _seed_bars(
        db_session, "2454", start=date(2026, 4, 27),
        closes=[1000, 1010, 1005],
        opens=[995, 1002, 1008],
    )

    result = await discussion_scoreboard_service.compute_scoreboard(
        db_session, d,
    )
    r = result["rows"][0]
    assert r["day1_open"] == 995
    assert r["daily_closes"] == [1000, 1010, 1005, None, None]
    assert r["change_pcts"][3] is None
    assert r["change_pcts"][4] is None
    assert r["days_resolved"] == 3


@pytest.mark.asyncio
async def test_compute_scoreboard_uses_cached_day1_open(
    db_session: AsyncSession,
):
    """When `discussion.day1_open_prices[symbol]` is already set
    (verifier pinned it earlier), the scoreboard reuses it instead
    of re-reading from OHLCV — protects against later upstream
    corrections silently shifting the baseline."""
    user = await _make_user(db_session, "scorer-cached@example.com")
    created = datetime(2026, 4, 27, 6, 0, tzinfo=UTC)
    d = await _make_discussion(
        db_session,
        owner_id=user.id, created_at=created,
        recommended=["2330"],
        day1_open_prices={"2330": 555.5},  # cached from verifier
    )
    # OHLCV says open=600 but cache says 555.5 — cache wins.
    await _seed_bars(
        db_session, "2330", start=date(2026, 4, 27),
        closes=[602, 605, 607, 609, 610],
        opens=[600, 603, 605, 607, 609],
    )

    result = await discussion_scoreboard_service.compute_scoreboard(
        db_session, d,
    )
    r = result["rows"][0]
    assert r["day1_open"] == 555.5


@pytest.mark.asyncio
async def test_compute_scoreboard_empty_recommended_returns_no_rows(
    db_session: AsyncSession,
):
    user = await _make_user(db_session, "scorer-empty@example.com")
    d = await _make_discussion(
        db_session,
        owner_id=user.id,
        created_at=datetime(2026, 4, 27, 6, 0, tzinfo=UTC),
        recommended=[],   # synthesizer returned no symbols
    )
    result = await discussion_scoreboard_service.compute_scoreboard(
        db_session, d,
    )
    assert result["rows"] == []


@pytest.mark.asyncio
async def test_compute_scoreboard_handles_missing_ohlcv(
    db_session: AsyncSession,
):
    """No bars in archive AND live fallback returns [] → all closes
    None, day1_open None, days_resolved=0. Doesn't crash."""
    from unittest.mock import AsyncMock, patch

    user = await _make_user(db_session, "scorer-noohlcv@example.com")
    d = await _make_discussion(
        db_session,
        owner_id=user.id,
        created_at=datetime(2026, 4, 27, 6, 0, tzinfo=UTC),
        recommended=["9999"],
    )
    with patch(
        "services.tw_market_service.get_history",
        new=AsyncMock(return_value=[]),
    ):
        result = await discussion_scoreboard_service.compute_scoreboard(
            db_session, d,
        )
    r = result["rows"][0]
    assert r["day1_open"] is None
    assert r["daily_closes"] == [None] * 5
    assert r["change_pcts"] == [None] * 5
    assert r["days_resolved"] == 0


@pytest.mark.asyncio
async def test_compute_scoreboard_falls_back_to_live_when_db_empty(
    db_session: AsyncSession,
):
    """Symbol's bars never landed in `ohlcv_daily` (transient cron
    failure for one symbol while the rest of the universe ingested
    fine). The live waterfall in `tw_market_service.get_history`
    must fill the gap so the user sees actual D1-D5 data instead
    of all dashes."""
    from unittest.mock import AsyncMock, patch

    user = await _make_user(db_session, "scorer-livefallback@example.com")
    created = datetime(2026, 4, 27, 6, 0, tzinfo=UTC)
    d = await _make_discussion(
        db_session,
        owner_id=user.id, created_at=created,
        recommended=["2375"],
    )
    # Note: NO `_seed_bars` call — DB is intentionally empty.
    # Live waterfall returns 5 fresh bars instead.
    live_bars = [
        {"time": "2026-04-27", "open": 100, "high": 102, "low":  99, "close": 101, "volume": 1_000},
        {"time": "2026-04-28", "open": 101, "high": 103, "low": 100, "close": 102, "volume": 1_000},
        {"time": "2026-04-29", "open": 102, "high": 104, "low": 101, "close": 103, "volume": 1_000},
        {"time": "2026-04-30", "open": 103, "high": 105, "low": 102, "close": 104, "volume": 1_000},
        {"time": "2026-05-01", "open": 104, "high": 106, "low": 103, "close": 105, "volume": 1_000},
    ]
    with patch(
        "services.tw_market_service.get_history",
        new=AsyncMock(return_value=live_bars),
    ):
        result = await discussion_scoreboard_service.compute_scoreboard(
            db_session, d,
        )

    r = result["rows"][0]
    assert r["day1_open"] == 100
    assert r["daily_closes"] == [101, 102, 103, 104, 105]
    assert r["days_resolved"] == 5


@pytest.mark.asyncio
async def test_compute_scoreboard_populates_debug_traces(
    db_session: AsyncSession,
):
    """`debug_traces=[]` collects per-symbol diagnostics — archive
    bars count + window dates + day1_open source — without altering
    the returned payload shape."""
    user = await _make_user(db_session, "scorer-debug-trace@example.com")
    created = datetime(2026, 4, 27, 6, 0, tzinfo=UTC)
    d = await _make_discussion(
        db_session,
        owner_id=user.id, created_at=created,
        recommended=["2330"],
    )
    await _seed_bars(
        db_session, "2330", start=date(2026, 4, 27),
        closes=[602, 605, 607, 609, 610],
        opens=[600, 603, 605, 607, 609],
    )

    traces: list[dict] = []
    result = await discussion_scoreboard_service.compute_scoreboard(
        db_session, d, debug_traces=traces,
    )
    assert result["rows"][0]["days_resolved"] == 5
    assert len(traces) == 1
    t = traces[0]
    assert t["symbol"] == "2330"
    assert t["archive_bars_count"] == 5
    assert t["archive_dates"] == [
        "2026-04-27", "2026-04-28", "2026-04-29",
        "2026-04-30", "2026-05-01",
    ]
    assert t["live_fallback_tried"] is False
    assert t["window_bars_count"] == 5
    assert t["day1_open_source"] == "archive"
    assert t["days_resolved"] == 5


@pytest.mark.asyncio
async def test_compute_scoreboard_debug_trace_marks_live_fallback(
    db_session: AsyncSession,
):
    """When the archive is empty and the live waterfall fills in,
    the per-symbol trace reports `live_fallback_tried=True` and the
    day1_open_source flips to `live_fallback`."""
    from unittest.mock import AsyncMock, patch

    user = await _make_user(db_session, "scorer-debug-livefallback@example.com")
    created = datetime(2026, 4, 27, 6, 0, tzinfo=UTC)
    d = await _make_discussion(
        db_session,
        owner_id=user.id, created_at=created,
        recommended=["2375"],
    )
    live_bars = [
        {"time": "2026-04-27", "open": 100, "high": 102, "low":  99, "close": 101, "volume": 1_000},
        {"time": "2026-04-28", "open": 101, "high": 103, "low": 100, "close": 102, "volume": 1_000},
        {"time": "2026-04-29", "open": 102, "high": 104, "low": 101, "close": 103, "volume": 1_000},
        {"time": "2026-04-30", "open": 103, "high": 105, "low": 102, "close": 104, "volume": 1_000},
        {"time": "2026-05-01", "open": 104, "high": 106, "low": 103, "close": 105, "volume": 1_000},
    ]
    traces: list[dict] = []
    with patch(
        "services.tw_market_service.get_history",
        new=AsyncMock(return_value=live_bars),
    ):
        await discussion_scoreboard_service.compute_scoreboard(
            db_session, d, debug_traces=traces,
        )
    t = traces[0]
    assert t["archive_bars_count"] == 0
    assert t["live_fallback_tried"] is True
    assert t["live_fallback_bars_count"] == 5
    assert t["day1_open_source"] == "live_fallback"


@pytest.mark.asyncio
async def test_build_scoreboard_debug_payload_shape(
    db_session: AsyncSession,
):
    """`build_scoreboard_debug_payload` rolls per-symbol traces +
    cron-eligibility + trading-window resolution into one blob.

    Validates the contract used by the `/scoreboard?debug=true`
    endpoint: discussion eligibility for the daily cron, anchor
    source labelling, daily_close_prices state, and that the
    last-cron-run lookup is best-effort (None tolerated)."""
    user = await _make_user(db_session, "scorer-debug-payload@example.com")
    created = datetime(2026, 4, 27, 6, 0, tzinfo=UTC)
    d = await _make_discussion(
        db_session,
        owner_id=user.id, created_at=created,
        recommended=["2330"],
        as_of_date=date(2026, 4, 20),   # backtest mode
    )
    traces = [{"symbol": "2330", "archive_bars_count": 0}]
    payload = await discussion_scoreboard_service.build_scoreboard_debug_payload(
        d, traces,
    )

    # Discussion block reflects the row state.
    assert payload["discussion"]["recommended_symbols"] == ["2330"]
    assert payload["discussion"]["anchor_source"] == "as_of_date"
    assert payload["discussion"]["anchor_date"] == "2026-04-20"
    assert payload["discussion"]["daily_close_prices_state"] == "null"
    assert payload["discussion"]["conclusion_present"] is True

    # Cron eligibility: conclusion present + daily_close_prices NULL
    # → eligible regardless of created_at age.
    elig = payload["cron_eligibility"]
    assert elig["eligible"] is True
    assert elig["daily_close_missing"] is True
    assert elig["skip_reason"] is None

    # Trading window mirrors module constants.
    tw = payload["trading_window"]
    assert tw["anchor_tw"] == "2026-04-20"
    assert tw["window_days_target"] == 5
    assert tw["lookahead_calendar_days"] == 14

    assert payload["per_symbol"] == traces


@pytest.mark.asyncio
async def test_build_scoreboard_debug_payload_skip_already_scored(
    db_session: AsyncSession,
):
    """Old discussion that's already fully scored: `daily_close_prices`
    populated, `created_at` outside the 14-day refresh window →
    cron would skip it. Payload reports the skip reason."""
    user = await _make_user(db_session, "scorer-debug-skip@example.com")
    created = datetime.now(UTC) - timedelta(days=30)
    d = await _make_discussion(
        db_session,
        owner_id=user.id, created_at=created,
        recommended=["2330"],
    )
    # Stamp daily_close_prices so the daily_close_missing check fails.
    d.daily_close_prices = {"2330": [600.0, 601.0, 602.0, 603.0, 604.0]}
    db_session.add(d)
    await db_session.commit()
    await db_session.refresh(d)

    payload = await discussion_scoreboard_service.build_scoreboard_debug_payload(
        d, [],
    )
    elig = payload["cron_eligibility"]
    assert elig["eligible"] is False
    assert elig["daily_close_missing"] is False
    assert elig["within_refresh_window"] is False
    assert elig["skip_reason"] == "already_scored_and_outside_refresh_window"
    assert payload["discussion"]["daily_close_prices_state"] == "populated"


@pytest.mark.asyncio
async def test_compute_scoreboard_skips_live_fallback_when_db_has_bars(
    db_session: AsyncSession,
):
    """Defense: a partial DB window (e.g. 3 of 5 days ingested) must
    NOT trigger the live fallback — that would burn an extra upstream
    call when we already have actionable data, and the next cron tick
    will fill in the missing days anyway."""
    from unittest.mock import AsyncMock, patch

    user = await _make_user(db_session, "scorer-partial-no-fallback@example.com")
    created = datetime(2026, 4, 27, 6, 0, tzinfo=UTC)
    d = await _make_discussion(
        db_session,
        owner_id=user.id, created_at=created,
        recommended=["2330"],
    )
    await _seed_bars(
        db_session, "2330", start=date(2026, 4, 27),
        closes=[600, 601, 602],
        opens=[600, 601, 602],
    )

    fallback = AsyncMock(return_value=[])
    with patch("services.tw_market_service.get_history", new=fallback):
        await discussion_scoreboard_service.compute_scoreboard(
            db_session, d,
        )
    fallback.assert_not_awaited()


# ── persist_scoreboard ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_persist_scoreboard_writes_column_and_returns_true(
    db_session: AsyncSession,
):
    user = await _make_user(db_session, "scorer-persist@example.com")
    created = datetime(2026, 4, 27, 6, 0, tzinfo=UTC)
    d = await _make_discussion(
        db_session,
        owner_id=user.id, created_at=created,
        recommended=["2330", "2454"],
    )
    await _seed_bars(
        db_session, "2330", start=date(2026, 4, 27),
        closes=[602, 605, 607, 609, 610],
        opens=[600, 603, 605, 607, 609],
    )
    await _seed_bars(
        db_session, "2454", start=date(2026, 4, 27),
        closes=[1000, 1010, 1005, 1020, 1015],
        opens=[995, 1002, 1008, 1012, 1018],
    )

    fully = await discussion_scoreboard_service.persist_scoreboard(
        db_session, d,
    )
    assert fully is True

    # Column persisted
    refreshed = await db_session.get(Discussion, d.id)
    assert refreshed.daily_close_prices is not None
    assert refreshed.daily_close_prices["2330"] == [602, 605, 607, 609, 610]
    assert refreshed.daily_close_prices["2454"] == [1000, 1010, 1005, 1020, 1015]
    # day1_open back-filled when missing (manual discussion path)
    assert refreshed.day1_open_prices == {"2330": 600.0, "2454": 995.0}


@pytest.mark.asyncio
async def test_persist_scoreboard_partial_window_returns_false(
    db_session: AsyncSession,
):
    user = await _make_user(db_session, "scorer-partial-persist@example.com")
    d = await _make_discussion(
        db_session,
        owner_id=user.id,
        created_at=datetime(2026, 4, 27, 6, 0, tzinfo=UTC),
        recommended=["2330"],
    )
    await _seed_bars(
        db_session, "2330", start=date(2026, 4, 27),
        closes=[602, 605],   # only 2 bars
        opens=[600, 603],
    )

    fully = await discussion_scoreboard_service.persist_scoreboard(
        db_session, d,
    )
    assert fully is False
    refreshed = await db_session.get(Discussion, d.id)
    # Partial scoreboard still gets persisted so the UI can show what
    # we have today; the cron will overwrite it tomorrow when more
    # bars land. days_resolved < 5 just keeps the "skip" signal off.
    assert refreshed.daily_close_prices["2330"][0] == 602
    assert refreshed.daily_close_prices["2330"][3] is None


# ── backtest mode anchor ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_compute_scoreboard_backtest_anchors_on_as_of_date(
    db_session: AsyncSession,
):
    """Backtest discussion (`as_of_date` set, `created_at` typically
    months later): the D1-D5 window must start at `as_of_date`, not
    at `created_at`. Mirrors `tasks/verify_discussion_outcome` so
    both modules grade against the same window."""
    user = await _make_user(db_session, "scorer-backtest@example.com")
    # User created the backtest "today" (2026-04-27) but is asking
    # "what would the personas have said back on 2025-01-15".
    created = datetime(2026, 4, 27, 6, 0, tzinfo=UTC)
    anchor = date(2025, 1, 15)
    d = await _make_discussion(
        db_session,
        owner_id=user.id, created_at=created,
        as_of_date=anchor,
        recommended=["2330"],
    )
    # Bars seeded around the BACKTEST anchor, not creation date. If
    # the service still anchors to `created_at` it'll find no bars
    # for "2026-04-27..2026-05-11" and report all-None.
    await _seed_bars(
        db_session, "2330", start=anchor,
        closes=[602, 605, 607, 609, 610],
        opens=[600, 603, 605, 607, 609],
    )

    result = await discussion_scoreboard_service.compute_scoreboard(
        db_session, d,
    )
    # Both anchor keys reflect the backtest date.
    assert result["anchor_date"] == "2025-01-15"
    assert result["created_at_tw_date"] == "2025-01-15"
    r = result["rows"][0]
    assert r["day1_open"] == 600
    assert r["daily_closes"] == [602, 605, 607, 609, 610]
    assert r["days_resolved"] == 5


@pytest.mark.asyncio
async def test_persist_scoreboard_backtest_writes_anchor_aligned_closes(
    db_session: AsyncSession,
):
    """End-to-end persist for a backtest discussion: the persisted
    `daily_close_prices` and `day1_open_prices` reflect bars at the
    backtest anchor, not at `created_at`."""
    user = await _make_user(
        db_session, "scorer-backtest-persist@example.com",
    )
    created = datetime(2026, 4, 27, 6, 0, tzinfo=UTC)
    anchor = date(2025, 1, 15)
    d = await _make_discussion(
        db_session,
        owner_id=user.id, created_at=created,
        as_of_date=anchor,
        recommended=["2454"],
    )
    await _seed_bars(
        db_session, "2454", start=anchor,
        closes=[1000, 1010, 1005, 1020, 1015],
        opens=[995, 1002, 1008, 1012, 1018],
    )

    fully = await discussion_scoreboard_service.persist_scoreboard(
        db_session, d,
    )
    assert fully is True
    refreshed = await db_session.get(Discussion, d.id)
    assert refreshed.daily_close_prices["2454"] == [
        1000, 1010, 1005, 1020, 1015,
    ]
    assert refreshed.day1_open_prices == {"2454": 995.0}


# ── PR-C1: Brier score + reliability buckets ────────────────────────


def _disc_for_brier(
    *,
    recommendations: list[dict],
    daily: dict[str, list[float | None]],
    opens: dict[str, float],
) -> Discussion:
    """Lightweight Discussion stub for compute_brier_for_discussion
    tests — no DB session needed since the function reads off the
    in-memory ORM instance."""
    return Discussion(
        id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        topic="t", rules="r",
        persona_ids=["a"],
        status="done",
        current_round=1,
        conclusion={
            "recommended_symbols": [r["symbol"] for r in recommendations],
            "recommendations": recommendations,
            "reasoning": "x", "risks": [],
            "time_horizon": "short_term",
            "consensus_score": 0.5,
        },
        daily_close_prices=daily,
        day1_open_prices=opens,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def test_brier_basic_textbook_example():
    """Conf 0.8, outcome 1 (peak ≥ threshold) → loss = (0.8 - 1)² = 0.04."""
    d = _disc_for_brier(
        recommendations=[{"symbol": "2330", "confidence": 0.8}],
        # peak D1-D5 = (110 - 100) / 100 = 10% > threshold 5% → outcome 1
        daily={"2330": [110.0, 105.0, 102.0, 108.0, 109.0]},
        opens={"2330": 100.0},
    )
    out = discussion_scoreboard_service.compute_brier_for_discussion(d)
    assert out is not None
    assert out["brier_score"] == pytest.approx(0.04, abs=1e-6)
    assert out["samples"] == 1
    assert out["outcome_vector"][0]["outcome_binary"] == 1


def test_brier_handles_outcome_zero_when_below_threshold():
    """Conf 0.7, outcome 0 (peak < threshold) → loss = 0.49."""
    d = _disc_for_brier(
        recommendations=[{"symbol": "2330", "confidence": 0.7}],
        # peak = 2% < 5% → outcome 0
        daily={"2330": [101.0, 102.0, 100.5, 100.0, 99.5]},
        opens={"2330": 100.0},
    )
    out = discussion_scoreboard_service.compute_brier_for_discussion(d)
    assert out is not None
    assert out["brier_score"] == pytest.approx(0.49, abs=1e-6)
    assert out["outcome_vector"][0]["outcome_binary"] == 0


def test_brier_averages_across_multiple_symbols():
    d = _disc_for_brier(
        recommendations=[
            {"symbol": "A", "confidence": 0.8},   # outcome 1, loss 0.04
            {"symbol": "B", "confidence": 0.3},   # outcome 0, loss 0.09
        ],
        daily={
            "A": [110.0, 105.0, 102.0, 108.0, 109.0],
            "B": [101.0, 102.0, 100.5, 100.0, 99.5],
        },
        opens={"A": 100.0, "B": 100.0},
    )
    out = discussion_scoreboard_service.compute_brier_for_discussion(d)
    assert out is not None
    # mean of (0.04, 0.09) = 0.065
    assert out["brier_score"] == pytest.approx(0.065, abs=1e-6)
    assert out["samples"] == 2


def test_brier_returns_none_when_recommendations_missing():
    """Pre-PR-C0 conclusion that bypassed the parser fallback —
    no recommendations means nothing to score against."""
    d = Discussion(
        id=uuid.uuid4(), owner_id=uuid.uuid4(),
        topic="t", rules="r", persona_ids=["a"],
        status="done", current_round=1,
        conclusion={"recommended_symbols": ["2330"]},   # no 'recommendations'
        daily_close_prices={"2330": [110.0] * 5},
        day1_open_prices={"2330": 100.0},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    assert discussion_scoreboard_service.compute_brier_for_discussion(d) is None


def test_brier_returns_none_when_no_resolved_symbols():
    """Window completely unresolved (closes all None) → no samples
    contribute, so the function returns None rather than a Brier of 0."""
    d = _disc_for_brier(
        recommendations=[{"symbol": "2330", "confidence": 0.7}],
        daily={"2330": [None, None, None, None, None]},
        opens={"2330": 100.0},
    )
    assert discussion_scoreboard_service.compute_brier_for_discussion(d) is None


def test_brier_skips_symbol_with_missing_open():
    """One symbol resolved, one missing day1_open — only the resolved
    one contributes to the average. Brier = 0.04 (single sample)."""
    d = _disc_for_brier(
        recommendations=[
            {"symbol": "A", "confidence": 0.8},
            {"symbol": "B", "confidence": 0.4},
        ],
        daily={
            "A": [110.0, 105.0, 102.0, 108.0, 109.0],
            "B": [101.0, 102.0, 100.5, 100.0, 99.5],
        },
        opens={"A": 100.0},   # B missing
    )
    out = discussion_scoreboard_service.compute_brier_for_discussion(d)
    assert out is not None
    assert out["brier_score"] == pytest.approx(0.04, abs=1e-6)
    assert out["samples"] == 1


def test_brier_clamps_invalid_confidence():
    """LLM occasionally emits 1.5 or -0.3 even after the C0 parser
    clamp — defensive bound here so a bad row in a sweep can't
    poison the aggregate."""
    d = _disc_for_brier(
        recommendations=[
            {"symbol": "2330", "confidence": 1.5},  # clamps to 1.0
        ],
        # outcome 0 → loss = (1 - 0)² = 1.0
        daily={"2330": [101.0, 102.0, 100.5, 100.0, 99.5]},
        opens={"2330": 100.0},
    )
    out = discussion_scoreboard_service.compute_brier_for_discussion(d)
    assert out is not None
    assert out["brier_score"] == pytest.approx(1.0, abs=1e-6)


def test_reliability_buckets_basic_monotonicity():
    """Perfectly calibrated synthesizer: every bucket's hit_rate
    matches its bucket midpoint. Monotonicity check."""
    pairs: list[tuple[float, int]] = []
    # bucket 0.0-0.1 → hit_rate 0.0
    pairs.extend([(0.05, 0)] * 10)
    # bucket 0.5-0.6 → hit_rate ~ 0.5
    pairs.extend([(0.55, 1)] * 5 + [(0.55, 0)] * 5)
    # bucket 0.9-1.0 → hit_rate ~ 0.9
    pairs.extend([(0.95, 1)] * 9 + [(0.95, 0)] * 1)
    buckets = discussion_scoreboard_service.compute_reliability_buckets(pairs)
    assert len(buckets) == 10
    rate_lookup = {b["bucket_lower"]: b["hit_rate"] for b in buckets}
    assert rate_lookup[0.0] == pytest.approx(0.0)
    assert rate_lookup[0.5] == pytest.approx(0.5)
    assert rate_lookup[0.9] == pytest.approx(0.9)


def test_reliability_buckets_emit_empty_buckets_with_count_zero():
    """Empty buckets must still appear in the output so the frontend
    can render a continuous diagram instead of stitching gaps."""
    pairs = [(0.05, 0)] * 10
    buckets = discussion_scoreboard_service.compute_reliability_buckets(pairs)
    assert len(buckets) == 10
    populated = [b for b in buckets if b["count"] > 0]
    empty = [b for b in buckets if b["count"] == 0]
    assert len(populated) == 1
    assert len(empty) == 9
    for b in empty:
        assert b["mean_confidence"] is None
        assert b["hit_rate"] is None


def test_reliability_buckets_includes_confidence_at_one():
    """Edge case — confidence = 1.0 must land in the last bucket
    rather than getting dropped because the upper bound is exclusive
    everywhere except the final bucket."""
    pairs = [(1.0, 1)] * 5
    buckets = discussion_scoreboard_service.compute_reliability_buckets(pairs)
    last = buckets[-1]
    assert last["count"] == 5
    assert last["hit_rate"] == pytest.approx(1.0)


def test_reliability_buckets_rejects_too_few_buckets():
    with pytest.raises(ValueError, match="n_buckets"):
        discussion_scoreboard_service.compute_reliability_buckets(
            [(0.5, 1)], n_buckets=1,
        )


@pytest.mark.asyncio
async def test_persist_scoreboard_writes_brier_when_resolved(
    db_session: AsyncSession,
):
    """End-to-end: a backtest discussion with PR-C0 recommendations
    + a fully resolved D1-D5 window gets `brier_score` + `outcome_vector`
    written alongside `daily_close_prices`."""
    user = await _make_user(db_session, "brier@example.com")
    anchor = date(2026, 5, 5)
    d = await _make_discussion(
        db_session,
        owner_id=user.id,
        created_at=datetime(2026, 5, 5, 9, 0, tzinfo=UTC),
        recommended=["2330"],
        day1_open_prices={"2330": 100.0},
        as_of_date=anchor - timedelta(days=1),
    )
    # Patch the in-memory conclusion to add `recommendations` —
    # simulates a post-PR-C0 row.
    d.conclusion = {
        **(d.conclusion or {}),
        "recommendations": [{"symbol": "2330", "confidence": 0.8}],
    }
    await db_session.commit()
    await db_session.refresh(d)

    await _seed_bars(
        db_session, "2330", start=anchor,
        closes=[110.0, 105.0, 102.0, 108.0, 109.0],
        opens=[100.0, 100.0, 100.0, 100.0, 100.0],
    )
    fully = await discussion_scoreboard_service.persist_scoreboard(
        db_session, d,
    )
    assert fully is True
    refreshed = await db_session.get(Discussion, d.id)
    assert refreshed.brier_score == pytest.approx(0.04, abs=1e-6)
    assert refreshed.outcome_vector is not None
    assert refreshed.outcome_vector[0]["outcome_binary"] == 1
    assert refreshed.outcome_vector[0]["confidence"] == pytest.approx(0.8)


# ── PR-C2 follow-up: calibrated_brier_score ─────────────────────────


def test_brier_includes_calibrated_when_recommendation_has_it():
    """When every recommendation carries calibrated_confidence, the
    function returns BOTH brier_score (raw) and
    calibrated_brier_score (post-curve). They differ when the
    curve actually corrected the synthesizer's overconfidence."""
    d = _disc_for_brier(
        recommendations=[
            # raw 0.9, calibrated 0.4 — synthesizer was 5x over-
            # confident, curve compresses it. Outcome 0 (peak < 5%)
            # so raw loss = (0.9-0)² = 0.81, calibrated loss = 0.16.
            {
                "symbol": "A",
                "confidence": 0.9,
                "calibrated_confidence": 0.4,
            },
        ],
        daily={"A": [101.0, 102.0, 100.5, 100.0, 99.5]},
        opens={"A": 100.0},
    )
    out = discussion_scoreboard_service.compute_brier_for_discussion(d)
    assert out is not None
    assert out["brier_score"] == pytest.approx(0.81, abs=1e-6)
    assert out["calibrated_brier_score"] == pytest.approx(0.16, abs=1e-6)
    # outcome_vector carries the calibrated value too — needed for
    # the sweep aggregate's reliability split.
    assert (
        out["outcome_vector"][0]["calibrated_confidence"] == 0.4
    )


def test_brier_calibrated_null_when_no_recommendation_has_it():
    """Cold-start strategy or live discussion: no calibration was
    applied, so calibrated_brier stays NULL while raw works as
    usual."""
    d = _disc_for_brier(
        recommendations=[{"symbol": "A", "confidence": 0.8}],
        daily={"A": [110.0, 105.0, 102.0, 108.0, 109.0]},
        opens={"A": 100.0},
    )
    out = discussion_scoreboard_service.compute_brier_for_discussion(d)
    assert out is not None
    assert out["brier_score"] == pytest.approx(0.04, abs=1e-6)
    assert out["calibrated_brier_score"] is None


def test_brier_calibrated_null_on_partial_coverage():
    """Mixed coverage — one pick has calibrated_confidence, the other
    doesn't. The function refuses to mix the two so the raw/
    calibrated comparison stays meaningful. Raw brier still
    computed normally."""
    d = _disc_for_brier(
        recommendations=[
            {
                "symbol": "A",
                "confidence": 0.9,
                "calibrated_confidence": 0.4,
            },
            # B has no calibrated_confidence
            {"symbol": "B", "confidence": 0.4},
        ],
        daily={
            "A": [101.0, 102.0, 100.5, 100.0, 99.5],
            "B": [101.0, 102.0, 100.5, 100.0, 99.5],
        },
        opens={"A": 100.0, "B": 100.0},
    )
    out = discussion_scoreboard_service.compute_brier_for_discussion(d)
    assert out is not None
    # raw mean: (0.81 + 0.16) / 2 = 0.485
    assert out["brier_score"] == pytest.approx(0.485, abs=1e-6)
    # NULL — partial coverage refuses to mix
    assert out["calibrated_brier_score"] is None


def test_brier_calibrated_treats_nan_as_missing():
    """Audit follow-up #3: a NaN or ±inf calibrated_confidence
    must be treated as missing (so it trips the partial-coverage
    NULL guard) rather than getting silently clamped to a corner
    value — which would make the calibrated_brier metric look
    suspiciously good / bad without the operator knowing the
    upstream emitted garbage."""
    d = _disc_for_brier(
        recommendations=[
            {
                "symbol": "A",
                "confidence": 0.8,
                "calibrated_confidence": float("nan"),
            },
        ],
        daily={"A": [110.0, 105.0, 102.0, 108.0, 109.0]},
        opens={"A": 100.0},
    )
    out = discussion_scoreboard_service.compute_brier_for_discussion(d)
    assert out is not None
    # Raw brier still computed normally.
    assert out["brier_score"] == pytest.approx(0.04, abs=1e-6)
    # NaN treated as missing → partial coverage → calibrated NULL.
    assert out["calibrated_brier_score"] is None


def test_brier_calibrated_clamps_invalid_value():
    """LLM occasionally emits broken calibrated_confidence —
    string / out-of-bounds / null. Clamp at the brier compute
    layer so the metric stays robust."""
    d = _disc_for_brier(
        recommendations=[
            {
                "symbol": "A",
                "confidence": 0.8,
                "calibrated_confidence": 1.5,   # clamps to 1.0
            },
        ],
        # outcome 1 (peak > 5%): raw loss = 0.04, calibrated loss = 0
        daily={"A": [110.0, 105.0, 102.0, 108.0, 109.0]},
        opens={"A": 100.0},
    )
    out = discussion_scoreboard_service.compute_brier_for_discussion(d)
    assert out is not None
    assert out["calibrated_brier_score"] == pytest.approx(0.0, abs=1e-6)


@pytest.mark.asyncio
async def test_persist_scoreboard_writes_calibrated_brier(
    db_session: AsyncSession,
):
    """End-to-end: a discussion whose recommendations carry
    calibrated_confidence gets calibrated_brier_score persisted
    alongside the raw brier_score."""
    user = await _make_user(db_session, "calbrier@example.com")
    anchor = date(2026, 5, 5)
    d = await _make_discussion(
        db_session,
        owner_id=user.id,
        created_at=datetime(2026, 5, 5, 9, 0, tzinfo=UTC),
        recommended=["2330"],
        day1_open_prices={"2330": 100.0},
        as_of_date=anchor - timedelta(days=1),
    )
    d.conclusion = {
        **(d.conclusion or {}),
        "recommendations": [{
            "symbol": "2330",
            "confidence": 0.9,
            "calibrated_confidence": 0.4,
        }],
    }
    await db_session.commit()
    await db_session.refresh(d)
    await _seed_bars(
        db_session, "2330", start=anchor,
        # peak < 5% so outcome=0 — raw loss 0.81, calibrated 0.16
        closes=[101.0, 102.0, 100.5, 100.0, 99.5],
        opens=[100.0, 100.0, 100.0, 100.0, 100.0],
    )
    fully = await discussion_scoreboard_service.persist_scoreboard(
        db_session, d,
    )
    assert fully is True
    refreshed = await db_session.get(Discussion, d.id)
    assert refreshed.brier_score == pytest.approx(0.81, abs=1e-6)
    assert refreshed.calibrated_brier_score == pytest.approx(
        0.16, abs=1e-6,
    )


@pytest.mark.asyncio
async def test_persist_scoreboard_no_brier_when_partial(
    db_session: AsyncSession,
):
    """Partial window — fully_resolved is False, brier_score must
    stay NULL so the next cron tick can complete it."""
    user = await _make_user(db_session, "brier-partial@example.com")
    anchor = date(2026, 5, 5)
    d = await _make_discussion(
        db_session,
        owner_id=user.id,
        created_at=datetime(2026, 5, 5, 9, 0, tzinfo=UTC),
        recommended=["2330"],
        day1_open_prices={"2330": 100.0},
        as_of_date=anchor - timedelta(days=1),
    )
    d.conclusion = {
        **(d.conclusion or {}),
        "recommendations": [{"symbol": "2330", "confidence": 0.8}],
    }
    await db_session.commit()
    await db_session.refresh(d)

    # only 3 bars seeded — D4 and D5 still NULL
    await _seed_bars(
        db_session, "2330", start=anchor,
        closes=[110.0, 105.0, 102.0],
        opens=[100.0, 100.0, 100.0],
    )
    fully = await discussion_scoreboard_service.persist_scoreboard(
        db_session, d,
    )
    assert fully is False
    refreshed = await db_session.get(Discussion, d.id)
    assert refreshed.brier_score is None
    assert refreshed.outcome_vector is None


# ── company-name enrichment ───────────────────────────────────────


@pytest.mark.asyncio
async def test_compute_scoreboard_enriches_name_for_tw_symbols(
    db_session: AsyncSession,
):
    """TW scoreboard rows pick up `name` from the in-memory company
    map so the frontend can render `台積電 (2330)` instead of a bare
    code. Lookup failures (symbol not in map) stay None."""
    from unittest.mock import patch

    user = await _make_user(db_session, "scorer-name-tw@example.com")
    d = await _make_discussion(
        db_session,
        owner_id=user.id,
        created_at=datetime(2026, 4, 27, 6, 0, tzinfo=UTC),
        recommended=["2330", "9999"],
    )
    await _seed_bars(
        db_session, "2330", start=date(2026, 4, 27),
        closes=[600, 605, 610, 612, 615],
    )
    await _seed_bars(
        db_session, "9999", start=date(2026, 4, 27),
        closes=[50, 51, 52, 53, 54],
    )

    name_map = {"2330": "台積電"}
    with patch(
        "services.tw_market_service.get_company_name",
        side_effect=lambda sym: name_map.get(sym),
    ):
        result = await discussion_scoreboard_service.compute_scoreboard(
            db_session, d,
        )

    by_sym = {r["symbol"]: r for r in result["rows"]}
    assert by_sym["2330"]["name"] == "台積電"
    assert by_sym["9999"]["name"] is None

