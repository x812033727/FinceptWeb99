"""Tests for tasks.auto_run_discussion — daily 5-round system task.

The auto-run task drives off `discussion_auto_run_configs` rows: every
user with `enabled=True` gets one discussion per UTC day, owned by
themselves. Tests cover the multi-user iteration, per-user
idempotency, weekend skip, no-enabled-user no-op, and the system-task
LLM override forwarding.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.discussion import Discussion, DiscussionTurn
from models.discussion_auto_run_config import DiscussionAutoRunConfig
from models.user import User, UserRole
from services import discussion_service


@pytest_asyncio.fixture(autouse=True)
async def _isolate_db(db_session: AsyncSession):
    """The shared StaticPool keeps the in-memory SQLite DB alive across
    tests, so leftover rows from a previous case can leak into the next
    (e.g. an earlier test's auto-run row trips the idempotency short-
    circuit). Wipe the relevant tables between cases."""
    await db_session.execute(delete(DiscussionTurn))
    await db_session.execute(delete(Discussion))
    await db_session.execute(delete(DiscussionAutoRunConfig))
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

    with patch("tasks.auto_run_discussion.AsyncSessionLocal", return_value=_CM()):
        yield


async def _make_user(
    db_session: AsyncSession, email: str = "user@example.com",
) -> User:
    u = User(
        id=uuid.uuid4(),
        email=email,
        hashed_password="x",
        role=UserRole.viewer,
    )
    db_session.add(u)
    await db_session.commit()
    await db_session.refresh(u)
    return u


async def _enable_for(
    db_session: AsyncSession,
    user: User,
    *,
    persona_ids: list[str] | None = None,
    topic: str = "topic",
    rules: str = "rules",
    enabled: bool = True,
) -> DiscussionAutoRunConfig:
    cfg = DiscussionAutoRunConfig(
        user_id=user.id,
        enabled=enabled,
        persona_ids=persona_ids or ["buffett", "lynch"],
        topic=topic,
        rules=rules,
    )
    db_session.add(cfg)
    await db_session.commit()
    await db_session.refresh(cfg)
    return cfg


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
    patch_session, db_session: AsyncSession,
):
    """Happy path: trading day, one enabled user → one discussion row
    owned by that user, tagged auto_run=true with verify_after_date set."""
    from tasks import auto_run_discussion

    user = await _make_user(db_session)
    await _enable_for(
        db_session, user,
        persona_ids=["buffett", "lynch", "soros"],
        topic="my topic",
        rules="my rules",
    )

    async def _fake_run_round(*_a, **_kw):
        return
        yield  # pragma: no cover

    synth = AsyncMock(return_value={
        "recommended_symbols": ["2330", "2454"],
        "reasoning": "x",
        "risks": [],
        "time_horizon": "short_term",
        "consensus_score": 0.7,
    })

    patches = _stub_lock_helpers() + [
        patch("tasks.auto_run_discussion.is_today_likely_trading_day",
              AsyncMock(return_value=True)),
        patch.object(discussion_service, "run_round", _fake_run_round),
        patch.object(discussion_service, "synthesize_conclusion", synth),
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
    assert d.owner_id == user.id
    # Service-layer normalize may dedupe but otherwise preserves order
    assert d.persona_ids == ["buffett", "lynch", "soros"]
    assert d.topic == "my topic"
    assert d.rules == "my rules"
    # `verify_after_date` is now seeded inside `synthesize_conclusion`
    # (PR #218) — the auto-run task no longer sets it explicitly
    # (PR #222). This test mocks `synthesize_conclusion` so the real
    # seed never fires; the contract is covered by
    # `test_synthesize_conclusion_seeds_verify_after_date`.


@pytest.mark.asyncio
async def test_iterates_multiple_enabled_users(
    patch_session, db_session: AsyncSession,
):
    """Two enabled users → two discussions, one owned by each. Disabled
    users are skipped."""
    from tasks import auto_run_discussion

    a = await _make_user(db_session, "a@example.com")
    b = await _make_user(db_session, "b@example.com")
    c = await _make_user(db_session, "c@example.com")
    await _enable_for(db_session, a, topic="topic A", rules="r")
    await _enable_for(db_session, b, topic="topic B", rules="r")
    await _enable_for(db_session, c, topic="topic C", rules="r", enabled=False)

    async def _fake_run_round(*_a, **_kw):
        return
        yield  # pragma: no cover

    synth = AsyncMock(return_value={
        "recommended_symbols": [],
        "reasoning": "x",
        "risks": [], "time_horizon": "short_term", "consensus_score": 0.0,
    })

    patches = _stub_lock_helpers() + [
        patch("tasks.auto_run_discussion.is_today_likely_trading_day",
              AsyncMock(return_value=True)),
        patch.object(discussion_service, "run_round", _fake_run_round),
        patch.object(discussion_service, "synthesize_conclusion", synth),
    ]
    _enter_all(patches)
    try:
        await auto_run_discussion.run()
    finally:
        _exit_all(patches)

    auto_rows = (await db_session.scalars(
        select(Discussion).where(Discussion.auto_run.is_(True))
    )).all()
    owners = {r.owner_id for r in auto_rows}
    assert owners == {a.id, b.id}


@pytest.mark.asyncio
async def test_idempotent_same_day(
    patch_session, db_session: AsyncSession,
):
    """A second tick on the same UTC date must NOT create another
    discussion for a user who already has one today."""
    from tasks import auto_run_discussion

    user = await _make_user(db_session)
    await _enable_for(db_session, user)

    pre = Discussion(
        id=uuid.uuid4(),
        owner_id=user.id,
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
    stream_chat.assert_not_called()


@pytest.mark.asyncio
async def test_skipped_when_not_trading_day(
    patch_session, db_session: AsyncSession,
):
    """Weekend: trading-day gate returns False, no discussion created,
    health recorded ok with row_count=0."""
    from tasks import auto_run_discussion

    user = await _make_user(db_session)
    await _enable_for(db_session, user)

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
    health.assert_awaited()
    last_call = health.await_args_list[-1]
    assert last_call.kwargs["ok"] is True
    assert last_call.kwargs["row_count"] == 0


@pytest.mark.asyncio
async def test_no_enabled_users_is_a_clean_noop(
    patch_session, db_session: AsyncSession,
):
    """No user has opted in → task records ok with row_count=0. Not a
    failure (no auto-backoff) since this is a normal steady state for a
    fresh deployment."""
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
    ]
    _enter_all(patches)
    try:
        await auto_run_discussion.run()
    finally:
        _exit_all(patches)

    rows = (await db_session.scalars(select(Discussion))).all()
    assert rows == []
    health.assert_awaited()
    last_call = health.await_args_list[-1]
    assert last_call.kwargs["ok"] is True
    assert last_call.kwargs["row_count"] == 0


@pytest.mark.asyncio
async def test_runs_5_rounds_then_synthesize(
    patch_session, db_session: AsyncSession,
):
    """Counts the round + synthesizer invocations to pin the canonical
    5-rounds-then-conclude flow per enabled user."""
    from tasks import auto_run_discussion

    user = await _make_user(db_session)
    await _enable_for(db_session, user)

    round_calls = {"n": 0}

    async def _fake_run_round(*_a, **_kw):
        round_calls["n"] += 1
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
    patch_session, db_session: AsyncSession,
):
    """Auto-run resolves the `auto_run_discussion_persona` system-task
    config and forwards the provider/model as a per-call override to
    run_round so all personas use the same LLM. Pinning this prevents a
    regression where the cost knob silently slipped back to per-persona
    default routing."""
    from tasks import auto_run_discussion

    user = await _make_user(db_session)
    await _enable_for(db_session, user)

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
    ]
    _enter_all(patches)
    try:
        await auto_run_discussion.run()
    finally:
        _exit_all(patches)

    assert len(captured_calls) == 5
    for call_kwargs in captured_calls:
        assert call_kwargs["provider_override"] == "groq"
        assert call_kwargs["model_override"] == "llama-3.3-70b-versatile"


@pytest.mark.asyncio
async def test_one_user_failure_doesnt_block_others(
    patch_session, db_session: AsyncSession,
):
    """User A's run raises mid-flow → user B still gets their discussion
    created. The job records ok=true with the per-user error in the
    health row (partial-success path), not a hard failure."""
    from tasks import auto_run_discussion

    a = await _make_user(db_session, "a@example.com")
    b = await _make_user(db_session, "b@example.com")
    await _enable_for(db_session, a, topic="A", rules="r")
    await _enable_for(db_session, b, topic="B", rules="r")

    async def _fake_run_round(*_a, **_kw):
        return
        yield  # pragma: no cover

    call_counter = {"n": 0}

    async def _flaky_synth(_db, discussion, **_kw):
        call_counter["n"] += 1
        if discussion.owner_id == a.id:
            raise RuntimeError("boom")
        # Simulate the real synthesize_conclusion's side effect of
        # seeding verify_after_date on success (PR #218 / #222 —
        # the auto-run task no longer sets it explicitly).
        from datetime import UTC as _UTC, datetime as _dt, timedelta as _td
        discussion.verify_after_date = (
            _dt.now(_UTC).date() + _td(days=7)
        )
        return {
            "recommended_symbols": [],
            "reasoning": "x",
            "risks": [], "time_horizon": "short_term", "consensus_score": 0.0,
        }

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
        patch.object(discussion_service, "run_round", _fake_run_round),
        patch.object(discussion_service, "synthesize_conclusion", _flaky_synth),
    ]
    _enter_all(patches)
    try:
        await auto_run_discussion.run()
    finally:
        _exit_all(patches)

    auto_rows = (await db_session.scalars(
        select(Discussion).where(Discussion.auto_run.is_(True))
    )).all()
    owners = {r.owner_id for r in auto_rows}
    # Both rows get created (synthesizer raises after create); but only
    # B has verify_after_date populated.
    assert a.id in owners and b.id in owners
    b_row = next(r for r in auto_rows if r.owner_id == b.id)
    a_row = next(r for r in auto_rows if r.owner_id == a.id)
    assert b_row.verify_after_date is not None
    assert a_row.verify_after_date is None

    health.assert_awaited()
    last = health.await_args_list[-1]
    assert last.kwargs["ok"] is True
    assert last.kwargs["row_count"] == 1
    assert "boom" in (last.kwargs.get("error") or "")
