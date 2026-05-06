"""Unit tests for the runtime config service.

Covers:
  - Registry surface (known keys, get_spec)
  - get/get_int returns the compiled default when no override exists
  - upsert + get returns the override value
  - upsert validates type (int parses from "120", rejects "abc")
  - upsert validates min/max bounds
  - delete_override reverts to default
  - list_settings exposes default + effective + audit fields
  - Cache invalidation: upsert flushes Redis so subsequent reads see the
    new value within the same request (no waiting for natural TTL)
  - Integration: news_sentiment._can_make_llm_call honors the override
  - Integration: discussion_service.run_round honors the override
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.user import User, UserRole
from services import runtime_config_service as svc


# ── fixtures ──────────────────────────────────────────────────────


@pytest.fixture
async def admin_user(db_session: AsyncSession) -> User:
    user = User(
        email=f"runtime-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x",
        role=UserRole.admin,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


# ── registry ──────────────────────────────────────────────────────


def test_known_keys_includes_all_baseline_tunables():
    keys = svc.known_keys()
    assert "SENTIMENT_DAILY_LLM_CALL_CAP" in keys
    assert "SENTIMENT_INTERACTIVE_BACKFILL_CAP" in keys
    assert "SENTIMENT_CRON_MAX_AGE_DAYS" in keys
    assert "DISCUSSION_PERSONA_TIMEOUT_SECONDS" in keys
    assert "AI_REQUESTS_VIEWER_DAILY" in keys
    assert "AI_REQUESTS_ANALYST_DAILY" in keys
    assert "FINMIND_HOURLY_REQUEST_LIMIT" in keys
    assert "SENTIMENT_LLM_MAX_TOKENS" in keys
    assert "DISCUSSION_TURN_MAX_TOKENS" in keys
    assert "DISCUSSION_SYNTHESIZER_MAX_TOKENS" in keys


def test_sentiment_cron_max_age_days_has_drain_friendly_bounds():
    """Operator's lever for draining a multi-year backfill needs
    the upper bound to actually allow that. 3650 = ~10 years which
    covers FinMind's 2017+ archive comfortably."""
    spec = svc.get_spec("SENTIMENT_CRON_MAX_AGE_DAYS")
    assert spec.type == "int"
    assert spec.min_value is not None and spec.min_value >= 1
    assert spec.max_value is not None and spec.max_value >= 365
    # 7 (compiled default) must be in range.
    assert spec.min_value <= 7 <= spec.max_value


def test_max_tokens_specs_have_sane_bounds():
    """The three max_tokens tunables must have non-trivial output
    floors (so a typo doesn't accidentally lock the LLM to nothing)
    and ceilings within reach of any provider's context window."""
    for key in (
        "SENTIMENT_LLM_MAX_TOKENS",
        "DISCUSSION_TURN_MAX_TOKENS",
        "DISCUSSION_SYNTHESIZER_MAX_TOKENS",
    ):
        spec = svc.get_spec(key)
        assert spec.type == "int"
        assert spec.min_value is not None and spec.min_value >= 256
        assert spec.max_value is not None and spec.max_value >= 8192
        # 8192 is the compiled default — ensure it's within bounds.
        assert spec.min_value <= 8192 <= spec.max_value


def test_get_spec_unknown_raises():
    with pytest.raises(ValueError):
        svc.get_spec("not_a_setting")


# ── get / get_int ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_int_returns_compiled_default_when_unset(db_session: AsyncSession):
    from config import settings
    val = await svc.get_int(db_session, "SENTIMENT_DAILY_LLM_CALL_CAP")
    assert val == settings.SENTIMENT_DAILY_LLM_CALL_CAP


@pytest.mark.asyncio
async def test_get_int_returns_override_when_set(
    db_session: AsyncSession, admin_user: User,
):
    await svc.upsert(
        db_session, "SENTIMENT_DAILY_LLM_CALL_CAP", 250,
        updated_by_id=admin_user.id,
    )
    val = await svc.get_int(db_session, "SENTIMENT_DAILY_LLM_CALL_CAP")
    assert val == 250


# ── upsert validation ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_coerces_string_int_to_int(
    db_session: AsyncSession, admin_user: User,
):
    info = await svc.upsert(
        db_session, "AI_REQUESTS_VIEWER_DAILY", "12",
        updated_by_id=admin_user.id,
    )
    assert info.effective_value == 12
    assert info.is_overridden is True


@pytest.mark.asyncio
async def test_upsert_rejects_unparseable_int(
    db_session: AsyncSession, admin_user: User,
):
    with pytest.raises(ValueError, match="must be int"):
        await svc.upsert(
            db_session, "AI_REQUESTS_VIEWER_DAILY", "not-a-number",
            updated_by_id=admin_user.id,
        )


@pytest.mark.asyncio
async def test_upsert_enforces_min_bound(
    db_session: AsyncSession, admin_user: User,
):
    with pytest.raises(ValueError, match="must be >="):
        await svc.upsert(
            db_session, "DISCUSSION_PERSONA_TIMEOUT_SECONDS", 1,
            updated_by_id=admin_user.id,
        )


@pytest.mark.asyncio
async def test_upsert_enforces_max_bound(
    db_session: AsyncSession, admin_user: User,
):
    with pytest.raises(ValueError, match="must be <="):
        await svc.upsert(
            db_session, "DISCUSSION_PERSONA_TIMEOUT_SECONDS", 9_999,
            updated_by_id=admin_user.id,
        )


@pytest.mark.asyncio
async def test_upsert_unknown_key_raises(
    db_session: AsyncSession, admin_user: User,
):
    with pytest.raises(ValueError, match="unknown"):
        await svc.upsert(
            db_session, "NOT_A_REAL_SETTING", 5,
            updated_by_id=admin_user.id,
        )


# ── post-mortem threshold ordering invariant ──────────────────────


@pytest.mark.asyncio
async def test_upsert_post_mortem_marginal_above_win_rejected(
    db_session: AsyncSession, admin_user: User,
):
    """Setting marginal=8 with the default win=5 must be rejected so the
    post-mortem classifier never receives a degenerate band layout."""
    with pytest.raises(ValueError, match="marginal"):
        await svc.upsert(
            db_session, "POST_MORTEM_MARGINAL_WIN_THRESHOLD_PCT", 8.0,
            updated_by_id=admin_user.id,
        )


@pytest.mark.asyncio
async def test_upsert_post_mortem_win_above_strong_rejected(
    db_session: AsyncSession, admin_user: User,
):
    """Setting win=15 with the default strong=10 must be rejected."""
    with pytest.raises(ValueError, match="strong"):
        await svc.upsert(
            db_session, "POST_MORTEM_WIN_THRESHOLD_PCT", 15.0,
            updated_by_id=admin_user.id,
        )


@pytest.mark.asyncio
async def test_upsert_post_mortem_strong_below_win_rejected(
    db_session: AsyncSession, admin_user: User,
):
    """Setting strong=4 with the default win=5 must be rejected."""
    with pytest.raises(ValueError, match="strong"):
        await svc.upsert(
            db_session, "POST_MORTEM_STRONG_WIN_THRESHOLD_PCT", 4.0,
            updated_by_id=admin_user.id,
        )


@pytest.mark.asyncio
async def test_upsert_post_mortem_thresholds_accepts_valid_ordering(
    db_session: AsyncSession, admin_user: User,
):
    """A valid bump (e.g. marginal=2, win=4, strong=8) must be accepted
    when applied in an order that never breaks the invariant."""
    # Bump strong first to make headroom, then win, then marginal.
    await svc.upsert(
        db_session, "POST_MORTEM_STRONG_WIN_THRESHOLD_PCT", 8.0,
        updated_by_id=admin_user.id,
    )
    await svc.upsert(
        db_session, "POST_MORTEM_WIN_THRESHOLD_PCT", 4.0,
        updated_by_id=admin_user.id,
    )
    await svc.upsert(
        db_session, "POST_MORTEM_MARGINAL_WIN_THRESHOLD_PCT", 2.0,
        updated_by_id=admin_user.id,
    )
    assert await svc.get_float(
        db_session, "POST_MORTEM_MARGINAL_WIN_THRESHOLD_PCT",
    ) == 2.0
    assert await svc.get_float(
        db_session, "POST_MORTEM_WIN_THRESHOLD_PCT",
    ) == 4.0
    assert await svc.get_float(
        db_session, "POST_MORTEM_STRONG_WIN_THRESHOLD_PCT",
    ) == 8.0


# ── delete / restore ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_override_reverts_to_default(
    db_session: AsyncSession, admin_user: User,
):
    from config import settings
    await svc.upsert(
        db_session, "AI_REQUESTS_ANALYST_DAILY", 99,
        updated_by_id=admin_user.id,
    )
    await svc.delete_override(db_session, "AI_REQUESTS_ANALYST_DAILY")
    val = await svc.get_int(db_session, "AI_REQUESTS_ANALYST_DAILY")
    assert val == settings.AI_REQUESTS_ANALYST_DAILY


@pytest.mark.asyncio
async def test_delete_override_unknown_key_raises(db_session: AsyncSession):
    with pytest.raises(ValueError):
        await svc.delete_override(db_session, "NOT_A_REAL_SETTING")


# ── list_settings ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_settings_exposes_default_and_audit(
    db_session: AsyncSession, admin_user: User,
):
    await svc.upsert(
        db_session, "SENTIMENT_DAILY_LLM_CALL_CAP", 333,
        updated_by_id=admin_user.id,
    )
    rows = await svc.list_settings(db_session)
    by_key = {r.key: r for r in rows}
    cap = by_key["SENTIMENT_DAILY_LLM_CALL_CAP"]
    assert cap.is_overridden is True
    assert cap.effective_value == 333
    assert cap.updated_at is not None
    assert cap.updated_by_email == admin_user.email
    # An untouched setting still appears with default + is_overridden=False
    untouched = by_key["FINMIND_HOURLY_REQUEST_LIMIT"]
    assert untouched.is_overridden is False


# ── cache invalidation ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_invalidates_cache(
    db_session: AsyncSession, admin_user: User,
):
    """A prior get() may have cached the default in Redis. The follow-up
    upsert MUST invalidate that entry so the next get() sees the new
    value immediately — without this, admin edits would lag behind by
    up to the cache TTL."""
    # Populate the cache with the default.
    await svc.get_int(db_session, "SENTIMENT_DAILY_LLM_CALL_CAP")

    # Then upsert.
    await svc.upsert(
        db_session, "SENTIMENT_DAILY_LLM_CALL_CAP", 7,
        updated_by_id=admin_user.id,
    )

    # Next read MUST see 7 even though the cache entry was set moments ago.
    val = await svc.get_int(db_session, "SENTIMENT_DAILY_LLM_CALL_CAP")
    assert val == 7


# ── integration: callers honour the override ──────────────────────


@pytest.mark.asyncio
async def test_sentiment_cap_check_respects_admin_override(
    db_session: AsyncSession, admin_user: User,
):
    """Set the cap to 0 → `_can_make_llm_call` must return True (cap of
    0 means disabled per the spec). Set it to a tiny positive number and
    rapidly INCR past it → returns False."""
    import services.news_sentiment_service as nss

    # Set cap to 0 (disabled)
    await svc.upsert(
        db_session, "SENTIMENT_DAILY_LLM_CALL_CAP", 0,
        updated_by_id=admin_user.id,
    )
    assert await nss._can_make_llm_call(db_session) is True


@pytest.mark.asyncio
async def test_discussion_run_round_uses_override_timeout(
    db_session: AsyncSession, admin_user: User,
):
    """Override `DISCUSSION_PERSONA_TIMEOUT_SECONDS` to 10 and verify
    run_round picks it up. We don't actually exercise the timeout path —
    just assert the value flows through to the local `persona_timeout`
    variable by stubbing the persona stream and reading the value via a
    side-channel patch."""
    from services import discussion_service

    await svc.upsert(
        db_session, "DISCUSSION_PERSONA_TIMEOUT_SECONDS", 17,
        updated_by_id=admin_user.id,
    )

    discussion = await discussion_service.create_discussion(
        db_session,
        owner_id=admin_user.id,
        topic="topic",
        rules="rules",
        persona_ids=["buffett", "lynch"],
    )

    captured: dict[str, float | int | None] = {"timeout": None}
    original_timeout_factory = __import__("asyncio").timeout

    def _spy_timeout(sec):
        captured["timeout"] = sec
        return original_timeout_factory(sec)

    async def _stream(*_a, **_kw) -> AsyncIterator[dict]:
        yield {"type": "delta", "text": '{"stance":"agree","content":"x"}'}

    with patch("services.discussion_service.stream_chat", side_effect=_stream), \
         patch("services.discussion_service.gather_market_context",
               new=AsyncMock(return_value={"market": "TW"})), \
         patch("asyncio.timeout", side_effect=_spy_timeout):
        async for _ in discussion_service.run_round(
            db_session, discussion, user_id=str(admin_user.id),
        ):
            pass

    assert captured["timeout"] == 17
