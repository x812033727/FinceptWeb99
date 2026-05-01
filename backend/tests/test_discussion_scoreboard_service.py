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
    # day1 open=600, closes climb 600→610
    await _seed_bars(
        db_session, "2330", start=date(2026, 4, 27),
        closes=[602, 605, 607, 609, 610],
        opens=[600, 603, 605, 607, 609],
    )

    result = await discussion_scoreboard_service.compute_scoreboard(
        db_session, d,
    )
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
