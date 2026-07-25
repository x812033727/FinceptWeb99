"""Tests for tasks.auto_run_discussion — daily 5-round system task.

The auto-run task drives off `discussion_auto_run_configs` rows: every
user with `enabled=True` gets a discussion on every tick, owned by
themselves (no same-day skip — a fresh row is created even when one
already exists for the day). Tests cover the multi-user iteration,
same-day re-run, weekend skip, no-enabled-user no-op, and the
system-task LLM override forwarding.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import date
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
    and inflate the per-test auto-run row counts. Wipe the relevant
    tables between cases."""
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
    send_email: bool = False,
) -> DiscussionAutoRunConfig:
    cfg = DiscussionAutoRunConfig(
        user_id=user.id,
        enabled=enabled,
        send_email=send_email,
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
    # Live mode (no backtest anchor): the daily auto-run is a
    # forward-looking call, so `as_of_date` stays NULL — it must not be
    # flagged / rendered as a 「回測」 row. Pre-market settled-close
    # determinism is provided by `tw_market_service.get_quote`'s
    # closed-market `ohlcv_daily` self-heal, not by an as_of anchor.
    assert d.as_of_date is None
    # Empty universe: the general fallback slot still records an (empty)
    # ranked pool so "no pool that day" is distinguishable from legacy rows.
    assert d.candidate_snapshot["pool"] == []
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
async def test_runs_again_same_day(
    patch_session, db_session: AsyncSession,
):
    """No same-day skip: a tick must create a fresh discussion even when
    the user already has an auto-run row for the current Taipei day
    (e.g. a half-finished draft left by an earlier failed run)."""
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

    async def _fake_run_round(*_a, **_kw):
        return
        yield  # pragma: no cover

    synth = AsyncMock(return_value={
        "recommended_symbols": ["2330"],
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

    rows = (await db_session.scalars(
        select(Discussion).where(Discussion.auto_run.is_(True))
    )).all()
    assert len(rows) == 2
    synth.assert_awaited()


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


# ── prev_trading_day_estimate helper ──────────────────────────────


def test_prev_trading_day_estimate_walks_back_over_weekend():
    """Mon → prior Fri (skip Sun/Sat); Wed → Tue; Sun → Fri."""
    from datetime import date as _date

    from services.tw_trading_calendar import prev_trading_day_estimate

    # 2026-05-25 is a Monday → previous trading day is Fri 2026-05-22.
    assert prev_trading_day_estimate(_date(2026, 5, 25)) == _date(2026, 5, 22)
    # 2026-05-27 is a Wednesday → Tue 2026-05-26.
    assert prev_trading_day_estimate(_date(2026, 5, 27)) == _date(2026, 5, 26)
    # 2026-05-24 is a Sunday → Fri 2026-05-22.
    assert prev_trading_day_estimate(_date(2026, 5, 24)) == _date(2026, 5, 22)


# ── Email report (send_email opt-in) ──────────────────────────────


@pytest.mark.asyncio
async def test_sends_email_when_opted_in(
    patch_session, db_session: AsyncSession,
):
    """`cfg.send_email=True` + SMTP configured → cron calls
    `email_service.send_email` once with the user's address as the
    recipient and a non-empty Markdown body containing the topic."""
    from tasks import auto_run_discussion

    user = await _make_user(db_session, "report@example.com")
    await _enable_for(db_session, user, topic="my topic", rules="r", send_email=True)

    async def _fake_run_round(*_a, **_kw):
        return
        yield  # pragma: no cover

    synth = AsyncMock(return_value={
        "recommended_symbols": ["2330"], "reasoning": "x",
        "risks": [], "time_horizon": "short_term", "consensus_score": 0.5,
    })
    send_email = AsyncMock()

    patches = _stub_lock_helpers() + [
        patch("tasks.auto_run_discussion.is_today_likely_trading_day",
              AsyncMock(return_value=True)),
        patch.object(discussion_service, "run_round", _fake_run_round),
        patch.object(discussion_service, "synthesize_conclusion", synth),
        patch("services.email_service.is_configured", return_value=True),
        patch("services.email_service.send_email", send_email),
    ]
    _enter_all(patches)
    try:
        await auto_run_discussion.run()
    finally:
        _exit_all(patches)

    send_email.assert_awaited_once()
    kwargs = send_email.await_args.kwargs
    assert kwargs["to"] == "report@example.com"
    assert "my topic" in kwargs["body_markdown"]
    assert kwargs["attachment_filename"].endswith(".md")


@pytest.mark.asyncio
async def test_skips_email_when_not_opted_in(
    patch_session, db_session: AsyncSession,
):
    """`cfg.send_email=False` (default) → no SMTP attempt even with
    a fully configured deployment. Guards the opt-in contract."""
    from tasks import auto_run_discussion

    user = await _make_user(db_session)
    await _enable_for(db_session, user, send_email=False)

    async def _fake_run_round(*_a, **_kw):
        return
        yield  # pragma: no cover

    synth = AsyncMock(return_value={
        "recommended_symbols": [], "reasoning": "x",
        "risks": [], "time_horizon": "short_term", "consensus_score": 0.0,
    })
    send_email = AsyncMock()

    patches = _stub_lock_helpers() + [
        patch("tasks.auto_run_discussion.is_today_likely_trading_day",
              AsyncMock(return_value=True)),
        patch.object(discussion_service, "run_round", _fake_run_round),
        patch.object(discussion_service, "synthesize_conclusion", synth),
        patch("services.email_service.is_configured", return_value=True),
        patch("services.email_service.send_email", send_email),
    ]
    _enter_all(patches)
    try:
        await auto_run_discussion.run()
    finally:
        _exit_all(patches)

    send_email.assert_not_called()


@pytest.mark.asyncio
async def test_email_skipped_when_smtp_not_configured(
    patch_session, db_session: AsyncSession,
):
    """User opted in but SMTP isn't configured on this deployment →
    `is_configured()` returns False, send_email is never called, and
    the cron still reports success (the report is not a hard
    deliverable — the discussion itself is)."""
    from tasks import auto_run_discussion

    user = await _make_user(db_session)
    await _enable_for(db_session, user, send_email=True)

    async def _fake_run_round(*_a, **_kw):
        return
        yield  # pragma: no cover

    synth = AsyncMock(return_value={
        "recommended_symbols": [], "reasoning": "x",
        "risks": [], "time_horizon": "short_term", "consensus_score": 0.0,
    })
    send_email = AsyncMock()

    patches = _stub_lock_helpers() + [
        patch("tasks.auto_run_discussion.is_today_likely_trading_day",
              AsyncMock(return_value=True)),
        patch.object(discussion_service, "run_round", _fake_run_round),
        patch.object(discussion_service, "synthesize_conclusion", synth),
        patch("services.email_service.is_configured", return_value=False),
        patch("services.email_service.send_email", send_email),
    ]
    _enter_all(patches)
    try:
        await auto_run_discussion.run()
    finally:
        _exit_all(patches)

    send_email.assert_not_called()

    auto_rows = (await db_session.scalars(
        select(Discussion).where(Discussion.auto_run.is_(True))
    )).all()
    assert len(auto_rows) == 1


@pytest.mark.asyncio
async def test_email_send_failure_doesnt_break_run(
    patch_session, db_session: AsyncSession,
):
    """SMTP transport error → cron logs + continues. The discussion
    row is still committed, the run reports success, and no
    exception propagates out to trip auto-backoff."""
    from tasks import auto_run_discussion

    user = await _make_user(db_session)
    await _enable_for(db_session, user, send_email=True)

    async def _fake_run_round(*_a, **_kw):
        return
        yield  # pragma: no cover

    synth = AsyncMock(return_value={
        "recommended_symbols": [], "reasoning": "x",
        "risks": [], "time_horizon": "short_term", "consensus_score": 0.0,
    })
    send_email = AsyncMock(side_effect=RuntimeError("SMTP 535 auth failed"))

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
        patch.object(discussion_service, "synthesize_conclusion", synth),
        patch("services.email_service.is_configured", return_value=True),
        patch("services.email_service.send_email", send_email),
    ]
    _enter_all(patches)
    try:
        await auto_run_discussion.run()
    finally:
        _exit_all(patches)

    send_email.assert_awaited_once()
    health.assert_awaited()
    last = health.await_args_list[-1]
    assert last.kwargs["ok"] is True
    assert last.kwargs["row_count"] == 1


@pytest.mark.asyncio
async def test_candidate_pool_stored_slim_on_sequence_one_only(
    patch_session, db_session: AsyncSession,
):
    """The full ranked pool is a per-day per-strategy fact: sequence 1
    carries a slim {symbol, strategy_score} snapshot, later sequences
    don't repeat it."""
    from tasks import auto_run_discussion

    user = await _make_user(db_session)
    cfg = await _enable_for(db_session, user)
    cfg.strategy_run_counts = {"general": 0, "chip_quality": 2, "price_signal": 0}
    await db_session.commit()

    rows = [
        {
            "symbol": str(2000 + i), "close": 100.0, "history_days": 60,
            "avg_volume_20d": 5_000_000, "foreign_buy_days_5d": 4,
            "foreign_net_buy_5d": 2000 + i, "return_5d": 0.03,
            "revenue_yoy": 15.0, "roe": 20.0, "operating_cash_flow": 5e8,
            "pe": 18.0,
        }
        for i in range(12)
    ]

    async def _fake_run_round(*_a, **_kw):
        return
        yield  # pragma: no cover

    synth = AsyncMock(return_value={
        "recommended_symbols": ["2000"], "reasoning": "x", "risks": [],
        "time_horizon": "short_term", "consensus_score": 0.7,
    })
    patches = _stub_lock_helpers() + [
        patch("tasks.auto_run_discussion.is_today_likely_trading_day",
              AsyncMock(return_value=True)),
        patch("tasks.auto_run_discussion.load_candidate_rows",
              AsyncMock(return_value=rows)),
        patch.object(discussion_service, "run_round", _fake_run_round),
        patch.object(discussion_service, "synthesize_conclusion", synth),
    ]
    _enter_all(patches)
    try:
        await auto_run_discussion.run()
    finally:
        _exit_all(patches)

    discussions = (await db_session.scalars(select(Discussion))).all()
    snapshots = {d.auto_run_sequence: d.candidate_snapshot for d in discussions}
    assert set(snapshots) == {1, 2}
    pool = snapshots[1]["pool"]
    assert len(pool) == 12
    assert set(pool[0]) == {"symbol", "strategy_score"}  # slim, no raw fields
    assert "pool" not in snapshots[2]


@pytest.mark.asyncio
async def test_chip_quality_empty_intersection_is_a_clean_noop(
    patch_session, db_session: AsyncSession,
):
    """chip_quality requires the chip AND quality gates; on a day where
    no stock passes both, the slot must simply not run — no discussion
    row, no error (only general gets the [[]] fallback)."""
    from tasks import auto_run_discussion

    user = await _make_user(db_session)
    cfg = await _enable_for(db_session, user)
    cfg.strategy_run_counts = {"general": 0, "chip_quality": 1, "price_signal": 0}
    await db_session.commit()

    chip_only_row = {
        "symbol": "2330", "close": 100.0, "history_days": 60,
        "avg_volume_20d": 5_000_000, "foreign_buy_days_5d": 4,
        "foreign_net_buy_5d": 2000, "return_5d": 0.03,
    }
    patches = _stub_lock_helpers() + [
        patch("tasks.auto_run_discussion.is_today_likely_trading_day",
              AsyncMock(return_value=True)),
        patch("tasks.auto_run_discussion.load_candidate_rows",
              AsyncMock(return_value=[chip_only_row])),
    ]
    _enter_all(patches)
    try:
        await auto_run_discussion.run()
    finally:
        _exit_all(patches)

    rows = (await db_session.scalars(select(Discussion))).all()
    assert rows == []


@pytest.mark.asyncio
async def test_notifies_user_when_daily_run_completes(
    patch_session, db_session: AsyncSession,
):
    """After the slots run, one daily_picks_ready notification fans out
    to the user via notify_user; the fallback general slot counts."""
    from tasks import auto_run_discussion

    user = await _make_user(db_session)
    await _enable_for(db_session, user)

    async def _fake_run_round(*_a, **_kw):
        return
        yield  # pragma: no cover

    synth = AsyncMock(return_value={
        "recommended_symbols": ["2330"], "reasoning": "x", "risks": [],
        "time_horizon": "short_term", "consensus_score": 0.7,
    })
    notify = AsyncMock()

    patches = _stub_lock_helpers() + [
        patch("tasks.auto_run_discussion.is_today_likely_trading_day",
              AsyncMock(return_value=True)),
        patch.object(discussion_service, "run_round", _fake_run_round),
        patch.object(discussion_service, "synthesize_conclusion", synth),
        patch("tasks.auto_run_discussion.notify_user", notify),
    ]
    _enter_all(patches)
    try:
        await auto_run_discussion.run()
    finally:
        _exit_all(patches)

    notify.assert_awaited_once()
    user_id, payload = notify.await_args.args
    assert user_id == str(user.id)
    assert payload["kind"] == "daily_picks_ready"
    assert payload["strategies"] == ["general"]
    assert payload["discussions"] == 1

    # Second tick the same day: the idempotency check skips every slot,
    # so no second notification goes out.
    notify.reset_mock()
    _enter_all(patches)
    try:
        await auto_run_discussion.run()
    finally:
        _exit_all(patches)
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_notify_failure_does_not_fail_the_run(
    patch_session, db_session: AsyncSession,
):
    from tasks import auto_run_discussion

    user = await _make_user(db_session)
    await _enable_for(db_session, user)

    async def _fake_run_round(*_a, **_kw):
        return
        yield  # pragma: no cover

    synth = AsyncMock(return_value={
        "recommended_symbols": ["2330"], "reasoning": "x", "risks": [],
        "time_horizon": "short_term", "consensus_score": 0.7,
    })

    patches = _stub_lock_helpers() + [
        patch("tasks.auto_run_discussion.is_today_likely_trading_day",
              AsyncMock(return_value=True)),
        patch.object(discussion_service, "run_round", _fake_run_round),
        patch.object(discussion_service, "synthesize_conclusion", synth),
        patch("tasks.auto_run_discussion.notify_user",
              AsyncMock(side_effect=RuntimeError("push down"))),
    ]
    _enter_all(patches)
    try:
        await auto_run_discussion.run()
    finally:
        _exit_all(patches)

    rows = (await db_session.scalars(select(Discussion))).all()
    assert len([r for r in rows if r.auto_run]) == 1  # run still succeeded


@pytest.mark.asyncio
async def test_run_round_sees_auto_run_strategy_on_the_live_object(
    patch_session, db_session: AsyncSession,
):
    """Regression for the large-trader feed plan's Task 3 gate: the
    `auto_run_strategy` column is stamped via a raw `UPDATE` with
    `synchronize_session=False` (a bulk update, not an ORM attribute
    set), and `AsyncSessionLocal` runs with `expire_on_commit=False`
    (db/session.py) — so without an explicit in-memory mirror
    alongside `discussion.auto_run = True`, every `run_round` call in
    the `_AUTO_ROUNDS` loop would see `discussion.auto_run_strategy
    is None` regardless of what was actually persisted. That's the
    exact object `round_runner/loop.py` reads
    `strategy=discussion.auto_run_strategy` off of to gate the
    `large_trader_positioning` block — a missing mirror here silently
    defeats the price_signal gate for every real auto-run / replay
    discussion, even though the DB column is correct.

    Calls `_run_strategy_slot` directly (rather than the full `run()`
    entry point) — going through `run()` requires a populated
    candidate-rows universe for `price_signal` to produce a non-empty
    batch (there's no market data in the test DB), which is
    orthogonal to what this regression is pinning."""
    from tasks import auto_run_discussion

    user = await _make_user(db_session)
    cfg = await _enable_for(db_session, user)

    seen_strategies: list[str | None] = []
    seen_sequences: list[int | None] = []

    async def _fake_run_round(_db, discussion, *_a, **_kw):
        seen_strategies.append(discussion.auto_run_strategy)
        seen_sequences.append(discussion.auto_run_sequence)
        return
        yield  # pragma: no cover

    synth = AsyncMock(return_value={
        "recommended_symbols": ["2330"], "reasoning": "x", "risks": [],
        "time_horizon": "short_term", "consensus_score": 0.7,
    })

    with patch.object(discussion_service, "run_round", _fake_run_round), \
         patch.object(discussion_service, "synthesize_conclusion", synth):
        await auto_run_discussion._run_strategy_slot(
            db_session, cfg, "price_signal", 1,
            date(2026, 7, 24), [{"symbol": "2330"}],
        )

    # `_AUTO_ROUNDS` calls happened; every single one must have seen
    # the real strategy (and sequence) on the live object, not the
    # pre-UPDATE `None`.
    assert seen_strategies, "run_round was never called"
    assert all(s == "price_signal" for s in seen_strategies), seen_strategies
    assert all(s == 1 for s in seen_sequences), seen_sequences

    # Belt-and-braces: the persisted row must agree with what
    # run_round saw — this regression is about the *mirror*, not the
    # UPDATE itself, so both must be "price_signal".
    row = (
        await db_session.scalars(
            select(Discussion).where(Discussion.owner_id == user.id)
        )
    ).one()
    assert row.auto_run_strategy == "price_signal"


@pytest.mark.asyncio
async def test_daily_picks_event_kind_mapping():
    from services.channel_notification_service import (
        DEFAULT_EVENT_KINDS,
        event_kind,
    )

    assert "daily_picks_ready" in DEFAULT_EVENT_KINDS
    assert event_kind({"kind": "daily_picks_ready"}) == "daily_picks_ready"
    assert event_kind({"type": "alert"}) == "price_alert"
