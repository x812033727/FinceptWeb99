"""Tests for tasks.score_discussion_outcomes.

Verifies the daily-cron filtering rules: only concluded
discussions, only those with NULL daily_close_prices, only those
older than the 7-day age threshold get processed. Per-row failures
are isolated.
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.discussion import Discussion
from models.user import User, UserRole
from services.ingest.repository import OhlcvBar, upsert_ohlcv_bars


@pytest.fixture
def patch_session(db_session: AsyncSession):
    class _CM:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *exc):
            return False

    with patch(
        "tasks.score_discussion_outcomes.AsyncSessionLocal",
        return_value=_CM(),
    ):
        yield


async def _user(db: AsyncSession, email: str) -> User:
    u = User(
        id=uuid.uuid4(), email=email, hashed_password="x",
        role=UserRole.viewer,
    )
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


async def _disc(
    db: AsyncSession,
    owner_id: uuid.UUID,
    *,
    age_days: int,
    conclusion: dict | None,
    daily_close_prices: dict | None = None,
) -> Discussion:
    created = datetime.now(UTC) - timedelta(days=age_days)
    d = Discussion(
        id=uuid.uuid4(),
        owner_id=owner_id,
        topic="t", rules="r",
        persona_ids=["buffett", "lynch"],
        status="done", current_round=1,
        conclusion=conclusion,
        daily_close_prices=daily_close_prices,
        created_at=created,
        updated_at=created,
    )
    db.add(d)
    await db.commit()
    await db.refresh(d)
    return d


@pytest.mark.asyncio
async def test_lock_held_skips_work(patch_session):
    from tasks import score_discussion_outcomes

    with patch(
        "tasks.score_discussion_outcomes.acquire_lock",
        AsyncMock(return_value=False),
    ), patch(
        "tasks.score_discussion_outcomes.release_lock", AsyncMock(),
    ), patch(
        "services.discussion_scoreboard_service.persist_scoreboard",
        AsyncMock(),
    ) as persist:
        await score_discussion_outcomes.run()

    persist.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_unconcluded_and_already_scored(
    patch_session, db_session: AsyncSession,
):
    from tasks import score_discussion_outcomes

    user = await _user(db_session, "score-skip@example.com")
    # Skip: no conclusion
    await _disc(db_session, user.id, age_days=10, conclusion=None)
    # Skip: already scored
    await _disc(
        db_session, user.id, age_days=10,
        conclusion={"recommended_symbols": ["2330"]},
        daily_close_prices={"2330": [600, 601, 602, 603, 604]},
    )
    # Eligible
    eligible = await _disc(
        db_session, user.id, age_days=10,
        conclusion={"recommended_symbols": ["2330"]},
    )
    # Pre-seed OHLCV so persist returns clean data
    await upsert_ohlcv_bars(db_session, [
        OhlcvBar(market="TW", symbol="2330",
                 ts=datetime.now(UTC).date() - timedelta(days=10 - i),
                 open=600.0 + i, high=605.0 + i, low=595.0 + i,
                 close=601.0 + i, volume=1000, source="test")
        for i in range(5)
    ])

    persist = AsyncMock(return_value=True)
    with patch(
        "tasks.score_discussion_outcomes.acquire_lock",
        AsyncMock(return_value=True),
    ), patch(
        "tasks.score_discussion_outcomes.release_lock", AsyncMock(),
    ), patch(
        "tasks.score_discussion_outcomes.backoff_remaining_seconds",
        AsyncMock(return_value=0),
    ), patch(
        "tasks.score_discussion_outcomes.clear_failures", AsyncMock(),
    ), patch(
        "tasks.score_discussion_outcomes.record_health", AsyncMock(),
    ), patch(
        "services.discussion_scoreboard_service.persist_scoreboard",
        persist,
    ):
        await score_discussion_outcomes.run()

    # Only the eligible row went through persist_scoreboard.
    assert persist.await_count == 1
    args = persist.await_args.args
    # second positional arg is the Discussion row
    assert args[1].id == eligible.id


@pytest.mark.asyncio
async def test_skips_recent_rows(
    patch_session, db_session: AsyncSession,
):
    """Rows newer than the 7-day age threshold get skipped — wait
    one more day rather than write a partial scoreboard the cron
    has to overwrite."""
    from tasks import score_discussion_outcomes

    user = await _user(db_session, "score-recent@example.com")
    await _disc(
        db_session, user.id, age_days=2,
        conclusion={"recommended_symbols": ["2330"]},
    )

    persist = AsyncMock(return_value=True)
    with patch(
        "tasks.score_discussion_outcomes.acquire_lock",
        AsyncMock(return_value=True),
    ), patch(
        "tasks.score_discussion_outcomes.release_lock", AsyncMock(),
    ), patch(
        "tasks.score_discussion_outcomes.backoff_remaining_seconds",
        AsyncMock(return_value=0),
    ), patch(
        "tasks.score_discussion_outcomes.clear_failures", AsyncMock(),
    ), patch(
        "tasks.score_discussion_outcomes.record_health", AsyncMock(),
    ), patch(
        "services.discussion_scoreboard_service.persist_scoreboard",
        persist,
    ):
        await score_discussion_outcomes.run()

    persist.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_row_failure_doesnt_abort_batch(
    patch_session, db_session: AsyncSession,
):
    from tasks import score_discussion_outcomes

    user = await _user(db_session, "score-flaky@example.com")
    bad = await _disc(
        db_session, user.id, age_days=10,
        conclusion={"recommended_symbols": ["BAD"]},
    )
    good = await _disc(
        db_session, user.id, age_days=10,
        conclusion={"recommended_symbols": ["2330"]},
    )

    async def _flaky(_db, discussion):
        if discussion.id == bad.id:
            raise RuntimeError("ohlcv read failed")
        return True

    health = AsyncMock()
    with patch(
        "tasks.score_discussion_outcomes.acquire_lock",
        AsyncMock(return_value=True),
    ), patch(
        "tasks.score_discussion_outcomes.release_lock", AsyncMock(),
    ), patch(
        "tasks.score_discussion_outcomes.backoff_remaining_seconds",
        AsyncMock(return_value=0),
    ), patch(
        "tasks.score_discussion_outcomes.clear_failures", AsyncMock(),
    ), patch(
        "tasks.score_discussion_outcomes.record_health", health,
    ), patch(
        "services.discussion_scoreboard_service.persist_scoreboard",
        _flaky,
    ):
        await score_discussion_outcomes.run()

    health.assert_awaited()
    last = health.await_args.kwargs
    # Partial success: ok=True row_count=1 + per-row error in `error`.
    assert last["ok"] is True
    assert last["row_count"] == 1
    assert "ohlcv read failed" in (last.get("error") or "")
