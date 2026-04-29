"""Tests for tasks.auto_run_discussion — daily 5-round system task."""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.discussion import Discussion, DiscussionTurn
from models.user import User, UserRole
from services import discussion_service


@pytest_asyncio.fixture(autouse=True)
async def _isolate_db(db_session: AsyncSession):
    """The shared StaticPool keeps the in-memory SQLite DB alive across
    tests, so leftover Discussion / User rows from a previous case can
    leak into the next (e.g. an earlier test's auto-run row trips the
    idempotency short-circuit). Wipe the relevant tables between cases.
    """
    await db_session.execute(delete(DiscussionTurn))
    await db_session.execute(delete(Discussion))
    await db_session.execute(delete(User))
    await db_session.commit()
    # Also drop the in-process admin-email cache so fresh ADMIN_EMAIL
    # patches take effect immediately.
    from services import admin_user_service
    admin_user_service._invalidate_cache()
    yield


@pytest.fixture
def patch_session(db_session: AsyncSession):
    class _CM:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *exc):
            return False

    with patch("tasks.auto_run_discussion.AsyncSessionLocal", return_value=_CM()):
        yield


@pytest.fixture
async def admin_user(db_session: AsyncSession) -> User:
    u = User(
        id=uuid.uuid4(),
        email="admin@example.com",
        hashed_password="x",
        role=UserRole.admin,
    )
    db_session.add(u)
    await db_session.commit()
    await db_session.refresh(u)
    return u


def _stream_events_sequence(replies: list[str]):
    counter = {"i": 0}

    async def _gen(*_a, **_kw) -> AsyncIterator[dict]:
        idx = min(counter["i"], len(replies) - 1)
        counter["i"] += 1
        yield {"type": "delta", "text": replies[idx]}

    return _gen


def _stub_lock_helpers():
    """Wrap the lock + backoff + health helpers so the task body runs
    instead of being short-circuited."""
    return [
        patch("tasks.auto_run_discussion.acquire_lock",
              AsyncMock(return_value=True)),
        patch("tasks.auto_run_discussion.release_lock", AsyncMock()),
        patch("tasks.auto_run_discussion.backoff_remaining_seconds",
              AsyncMock(return_value=0)),
        patch("tasks.auto_run_discussion.record_health", AsyncMock()),
        patch("tasks.auto_run_discussion.record_failure",
              AsyncMock(return_value=1)),
        patch("tasks.auto_run_discussion.clear_failures", AsyncMock()),
        patch("tasks.auto_run_discussion.get_failure_count",
              AsyncMock(return_value=0)),
    ]


def _enter_all(patches):
    return [p.__enter__() for p in patches]


def _exit_all(patches):
    for p in patches:
        p.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_creates_discussion_with_auto_run_flag(
    patch_session, db_session: AsyncSession, admin_user: User,
):
    """Happy path: trading day, admin user exists, 5 rounds + synthesizer
    produce a row tagged auto_run=true with verify_after_date set."""
    from tasks import auto_run_discussion

    replies = ['{"stance":"supplement","content":"x"}'] * 100

    patches = _stub_lock_helpers() + [
        patch("tasks.auto_run_discussion.is_today_likely_trading_day",
              AsyncMock(return_value=True)),
        patch("services.discussion_service.gather_market_context",
              new=AsyncMock(return_value={"market": "TW", "top_gainers": []})),
        patch("services.discussion_service.stream_chat",
              side_effect=_stream_events_sequence(replies)),
        patch("services.discussion_service.synthesize_conclusion",
              new=AsyncMock(return_value={
                  "recommended_symbols": ["2330", "2454"],
                  "reasoning": "x",
                  "risks": [],
                  "time_horizon": "short_term",
                  "consensus_score": 0.7,
              })),
        patch("config.settings.ADMIN_EMAIL", "admin@example.com"),
    ]
    _enter_all(patches)
    try:
        await auto_run_discussion.run()
    finally:
        _exit_all(patches)

    rows = (await db_session.scalars(select(Discussion))).all()
    auto_rows = [r for r in rows if r.auto_run]
    assert len(auto_rows) == 1
    d = auto_rows[0]
    assert d.owner_id == admin_user.id
    assert d.persona_ids == auto_run_discussion._AUTO_PERSONAS
    assert d.topic == auto_run_discussion._AUTO_TOPIC
    assert d.verify_after_date is not None
    # 5 weekdays out — at minimum 5 calendar days (Mon→Mon), at most 7
    # (Wed→next Wed crossing weekend).
    today = datetime.now(UTC).date()
    delta = (d.verify_after_date - today).days
    assert 5 <= delta <= 9


@pytest.mark.asyncio
async def test_idempotent_same_day(
    patch_session, db_session: AsyncSession, admin_user: User,
):
    """A second tick on the same UTC date must NOT create another
    discussion. The existing row's create_at is today, so the
    idempotency check short-circuits."""
    from tasks import auto_run_discussion

    # Pre-seed an auto-run row created today
    pre = Discussion(
        id=uuid.uuid4(),
        owner_id=admin_user.id,
        topic="x", rules="y", persona_ids=["buffett", "lynch"],
        status="draft", current_round=0,
        auto_run=True,
    )
    db_session.add(pre)
    await db_session.commit()

    stream_chat = AsyncMock()
    patches = _stub_lock_helpers() + [
        patch("tasks.auto_run_discussion.is_today_likely_trading_day",
              AsyncMock(return_value=True)),
        patch("services.discussion_service.stream_chat", new=stream_chat),
        patch("config.settings.ADMIN_EMAIL", "admin@example.com"),
    ]
    _enter_all(patches)
    try:
        await auto_run_discussion.run()
    finally:
        _exit_all(patches)

    rows = (await db_session.scalars(
        select(Discussion).where(Discussion.auto_run.is_(True))
    )).all()
    assert len(rows) == 1
    # No LLM call fired on the second attempt.
    stream_chat.assert_not_called()


@pytest.mark.asyncio
async def test_skipped_when_not_trading_day(
    patch_session, db_session: AsyncSession, admin_user: User,
):
    """Weekend: trading-day gate returns False, no discussion created,
    health recorded ok with row_count=0."""
    from tasks import auto_run_discussion

    health = AsyncMock()
    patches = [
        patch("tasks.auto_run_discussion.acquire_lock",
              AsyncMock(return_value=True)),
        patch("tasks.auto_run_discussion.release_lock", AsyncMock()),
        patch("tasks.auto_run_discussion.backoff_remaining_seconds",
              AsyncMock(return_value=0)),
        patch("tasks.auto_run_discussion.record_health", health),
        patch("tasks.auto_run_discussion.record_failure",
              AsyncMock(return_value=1)),
        patch("tasks.auto_run_discussion.clear_failures", AsyncMock()),
        patch("tasks.auto_run_discussion.get_failure_count",
              AsyncMock(return_value=0)),
        patch("tasks.auto_run_discussion.is_today_likely_trading_day",
              AsyncMock(return_value=False)),
        patch("config.settings.ADMIN_EMAIL", "admin@example.com"),
    ]
    _enter_all(patches)
    try:
        await auto_run_discussion.run()
    finally:
        _exit_all(patches)

    rows = (await db_session.scalars(
        select(Discussion).where(Discussion.auto_run.is_(True))
    )).all()
    assert rows == []
    # Weekend skip is NOT a failure — record health ok with row_count=0
    health.assert_awaited()
    last_call = health.await_args_list[-1]
    assert last_call.kwargs["ok"] is True
    assert last_call.kwargs["row_count"] == 0


@pytest.mark.asyncio
async def test_records_failure_when_admin_email_missing(
    patch_session, db_session: AsyncSession,
):
    """No admin_user fixture — get_admin_owner_id returns None and the
    task records a health failure with a clear message."""
    from tasks import auto_run_discussion

    health = AsyncMock()
    patches = [
        patch("tasks.auto_run_discussion.acquire_lock",
              AsyncMock(return_value=True)),
        patch("tasks.auto_run_discussion.release_lock", AsyncMock()),
        patch("tasks.auto_run_discussion.backoff_remaining_seconds",
              AsyncMock(return_value=0)),
        patch("tasks.auto_run_discussion.record_health", health),
        patch("tasks.auto_run_discussion.record_failure",
              AsyncMock(return_value=1)),
        patch("tasks.auto_run_discussion.clear_failures", AsyncMock()),
        patch("tasks.auto_run_discussion.get_failure_count",
              AsyncMock(return_value=0)),
        patch("tasks.auto_run_discussion.is_today_likely_trading_day",
              AsyncMock(return_value=True)),
        patch("config.settings.ADMIN_EMAIL", ""),
    ]
    _enter_all(patches)
    try:
        await auto_run_discussion.run()
    finally:
        _exit_all(patches)

    health.assert_awaited()
    last_call = health.await_args_list[-1]
    assert last_call.kwargs["ok"] is False
    assert "ADMIN_EMAIL" in last_call.kwargs["error"]


@pytest.mark.asyncio
async def test_runs_5_rounds_then_synthesize(
    patch_session, db_session: AsyncSession, admin_user: User,
):
    """Counts the round and synthesizer invocations to pin the canonical
    5-rounds-then-conclude flow."""
    from tasks import auto_run_discussion

    round_calls = {"n": 0}

    async def _fake_run_round(*_a, **_kw):
        round_calls["n"] += 1
        # Yield no events; persistence is mocked away — we just count
        # invocations.
        return
        yield  # pragma: no cover — never reached

    synth = AsyncMock(return_value={
        "recommended_symbols": ["2330"],
        "reasoning": "x",
        "risks": [], "time_horizon": "short_term", "consensus_score": 0.5,
    })

    patches = _stub_lock_helpers() + [
        patch("tasks.auto_run_discussion.is_today_likely_trading_day",
              AsyncMock(return_value=True)),
        patch.object(discussion_service, "run_round", _fake_run_round),
        patch.object(discussion_service, "synthesize_conclusion", synth),
        patch("config.settings.ADMIN_EMAIL", "admin@example.com"),
    ]
    _enter_all(patches)
    try:
        await auto_run_discussion.run()
    finally:
        _exit_all(patches)

    assert round_calls["n"] == 5
    synth.assert_awaited_once()


@pytest.mark.asyncio
async def test_passes_system_task_llm_override_to_run_round(
    patch_session, db_session: AsyncSession, admin_user: User,
):
    """Auto-run resolves the `auto_run_discussion_persona` system-task
    config and forwards the provider/model as a per-call override to
    run_round so all 8 personas use the same LLM. Pinning this prevents
    a regression where the cost knob silently slipped back to per-
    persona default routing."""
    from tasks import auto_run_discussion

    captured_calls: list[dict] = []

    async def _fake_run_round(*_a, **kwargs):
        captured_calls.append(kwargs)
        return
        yield  # pragma: no cover

    synth = AsyncMock(return_value={
        "recommended_symbols": ["2330"],
        "reasoning": "x",
        "risks": [], "time_horizon": "short_term", "consensus_score": 0.5,
    })

    patches = _stub_lock_helpers() + [
        patch("tasks.auto_run_discussion.is_today_likely_trading_day",
              AsyncMock(return_value=True)),
        patch.object(discussion_service, "run_round", _fake_run_round),
        patch.object(discussion_service, "synthesize_conclusion", synth),
        patch(
            "services.system_task_config_service.resolve",
            new=AsyncMock(return_value=("groq", "llama-3.3-70b-versatile")),
        ),
        patch("config.settings.ADMIN_EMAIL", "admin@example.com"),
    ]
    _enter_all(patches)
    try:
        await auto_run_discussion.run()
    finally:
        _exit_all(patches)

    # All 5 round invocations must have received the override pulled
    # from SystemTaskConfig.
    assert len(captured_calls) == 5
    for call_kwargs in captured_calls:
        assert call_kwargs["provider_override"] == "groq"
        assert call_kwargs["model_override"] == "llama-3.3-70b-versatile"


@pytest.mark.asyncio
async def test_filters_non_tw_symbols_from_logs(
    patch_session, db_session: AsyncSession, admin_user: User,
):
    """Synthesizer might emit a non-numeric or out-of-range symbol; the
    auto-run filter strips them before the verify_after_date update so
    the verifier doesn't waste a TW history lookup on `"AAPL"`. We
    assert the discussion is still created and the filter doesn't
    reject the row outright."""
    from tasks import auto_run_discussion

    synth = AsyncMock(return_value={
        "recommended_symbols": ["2330", "AAPL", "12", "2454"],  # 4 + 4 + 2 + 4 chars
        "reasoning": "x",
        "risks": [], "time_horizon": "short_term", "consensus_score": 0.5,
    })

    async def _fake_run_round(*_a, **_kw):
        return
        yield  # pragma: no cover

    patches = _stub_lock_helpers() + [
        patch("tasks.auto_run_discussion.is_today_likely_trading_day",
              AsyncMock(return_value=True)),
        patch.object(discussion_service, "run_round", _fake_run_round),
        patch.object(discussion_service, "synthesize_conclusion", synth),
        patch("config.settings.ADMIN_EMAIL", "admin@example.com"),
    ]
    _enter_all(patches)
    try:
        await auto_run_discussion.run()
    finally:
        _exit_all(patches)

    rows = (await db_session.scalars(
        select(Discussion).where(Discussion.auto_run.is_(True))
    )).all()
    assert len(rows) == 1
    # Conclusion is whatever the synthesizer returned; verifier does
    # the filtering at grade time. Auto-run task only filters for the
    # log message.
    assert rows[0].verify_after_date is not None
