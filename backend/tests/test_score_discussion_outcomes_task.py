"""Tests for tasks.score_discussion_outcomes.

Verifies the daily-cron filtering rules: only concluded
discussions get touched; never-scored rows process at any age;
recent rows (≤14 days) re-process even when already scored so
partial windows fill in as more bars land; old already-scored
rows stay skipped. Per-row failures are isolated.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
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
    # Skip: already scored AND past the 14-day refresh window —
    # stable historical row, no point re-processing.
    await _disc(
        db_session, user.id, age_days=30,
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
async def test_processes_recent_rows_even_when_already_scored(
    patch_session, db_session: AsyncSession,
):
    """Recently created rows (within the 14-day refresh window)
    re-process even when daily_close_prices is already populated.
    Reason: each new tick may have additional bars available, so
    the saved scoreboard could be stale (e.g. only D1-D2 written
    yesterday, D3 today)."""
    from tasks import score_discussion_outcomes

    user = await _user(db_session, "score-recent@example.com")
    recent = await _disc(
        db_session, user.id, age_days=2,
        conclusion={"recommended_symbols": ["2330"]},
        daily_close_prices={"2330": [600, 601, None, None, None]},
    )

    persist = AsyncMock(return_value=False)
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

    persist.assert_awaited_once()
    args = persist.await_args.args
    assert args[1].id == recent.id


@pytest.mark.asyncio
async def test_processes_brand_new_unscored_recent_row(
    patch_session, db_session: AsyncSession,
):
    """The case the user actually hit: fresh discussion (~2 days
    old), no scoreboard yet. Cron used to skip these via a 7-day
    age filter; now it processes them so manual `Retry now` from
    the AdminPage actually populates daily_close_prices for new
    rows."""
    from tasks import score_discussion_outcomes

    user = await _user(db_session, "score-fresh@example.com")
    fresh = await _disc(
        db_session, user.id, age_days=2,
        conclusion={"recommended_symbols": ["2330"]},
    )

    persist = AsyncMock(return_value=False)
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

    persist.assert_awaited_once()
    args = persist.await_args.args
    assert args[1].id == fresh.id


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
    # Second eligible row — its returned ID isn't referenced (only
    # `bad.id` is matched in the flaky stub) but seeding it ensures
    # the cron has at least two rows to iterate over and the
    # post-iteration error count check is meaningful.
    await _disc(
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
