"""Tests for `scripts.seed_trading_strategies`.

Covers:
  - Every preset's `persona_ids` resolve to a registered agent
    (typo guard — would otherwise blow up mid-discussion)
  - Every preset passes `strategy_template_service.create_template`
    validation (market in {TW,US,GLOBAL}, rounds 1-5, concurrency 1-3,
    persona_ids non-empty)
  - `seed_for_owner` creates all 6 templates on first run
  - `seed_for_owner` is idempotent — second run creates 0, skips 6
  - Soft-deleting a seeded template lets the next run recreate it
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ai.agents import all_persona_ids
from models.user import User, UserRole
from scripts.seed_trading_strategies import (
    TRADING_STRATEGIES,
    _validate_personas,
    seed_for_owner,
)
from services import strategy_template_service as svc


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    user = User(
        email=f"seed-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x",
        role=UserRole.admin,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def test_all_preset_personas_are_registered():
    """Every persona referenced by a preset must exist in the
    AGENTS registry — otherwise the discussion would fail at runtime
    with a confusing 'Unknown agent' error."""
    _validate_personas()


def test_presets_cover_expected_count_and_markets():
    assert len(TRADING_STRATEGIES) == 6
    markets = {s.market for s in TRADING_STRATEGIES}
    assert markets == {"TW", "US", "GLOBAL"}


def test_preset_field_bounds_match_service_validation():
    for s in TRADING_STRATEGIES:
        assert s.market in svc.ALLOWED_MARKETS
        assert 1 <= s.default_rounds <= svc.MAX_ROUNDS
        assert 1 <= s.default_concurrency <= svc.MAX_CONCURRENCY
        assert 1 <= len(s.name) <= svc.MAX_NAME_LEN
        assert s.topic.strip()
        assert s.rules.strip()
        assert len(s.persona_ids) >= 1


def test_persona_ids_are_unique_within_each_preset():
    for s in TRADING_STRATEGIES:
        assert len(s.persona_ids) == len(set(s.persona_ids)), (
            f"duplicate persona in {s.name}: {s.persona_ids}"
        )


@pytest.mark.asyncio
async def test_seed_creates_all_presets_for_fresh_owner(
    db_session: AsyncSession, owner: User,
):
    created, skipped = await seed_for_owner(db_session, owner.id)
    assert created == len(TRADING_STRATEGIES)
    assert skipped == 0

    rows = await svc.list_templates(db_session, owner_id=owner.id)
    assert {t.name for t in rows} == {s.name for s in TRADING_STRATEGIES}
    # markets + persona lists round-tripped intact
    by_name = {t.name: t for t in rows}
    for preset in TRADING_STRATEGIES:
        t = by_name[preset.name]
        assert t.market == preset.market
        assert t.persona_ids == list(preset.persona_ids)
        assert t.default_rounds == preset.default_rounds
        assert t.default_concurrency == preset.default_concurrency


@pytest.mark.asyncio
async def test_seed_is_idempotent(db_session: AsyncSession, owner: User):
    await seed_for_owner(db_session, owner.id)
    created, skipped = await seed_for_owner(db_session, owner.id)
    assert created == 0
    assert skipped == len(TRADING_STRATEGIES)

    rows = await svc.list_templates(db_session, owner_id=owner.id)
    assert len(rows) == len(TRADING_STRATEGIES)


@pytest.mark.asyncio
async def test_seed_recreates_after_soft_delete(
    db_session: AsyncSession, owner: User,
):
    """A user who deletes a seeded template should be able to re-run
    the seeder to get it back. `list_templates` defaults to active-only
    so the soft-deleted row doesn't block the re-create."""
    await seed_for_owner(db_session, owner.id)
    rows = await svc.list_templates(db_session, owner_id=owner.id)
    victim = rows[0]
    await svc.soft_delete_template(db_session, victim)

    created, skipped = await seed_for_owner(db_session, owner.id)
    assert created == 1
    assert skipped == len(TRADING_STRATEGIES) - 1

    active = await svc.list_templates(db_session, owner_id=owner.id)
    assert len(active) == len(TRADING_STRATEGIES)
    # the recreated row is a NEW id, not a revival of the soft-deleted one
    assert victim.id not in {t.id for t in active}


@pytest.mark.asyncio
async def test_seed_owner_scoping_does_not_leak(
    db_session: AsyncSession, owner: User,
):
    """Seeding for owner A must not surface in owner B's list."""
    other = User(
        email=f"seed-other-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x",
        role=UserRole.viewer,
    )
    db_session.add(other)
    await db_session.commit()
    await db_session.refresh(other)

    await seed_for_owner(db_session, owner.id)

    other_rows = await svc.list_templates(db_session, owner_id=other.id)
    assert other_rows == []


def test_known_short_term_personas_present_in_at_least_one_preset():
    """Sanity: this seeder is positioned as a 'trader strategies' set,
    so the short-term trading personas should actually appear somewhere
    — guards against future edits that accidentally drop them all."""
    used = {p for s in TRADING_STRATEGIES for p in s.persona_ids}
    short_term = {"livermore", "ptj", "minervini", "raschke"}
    registered = set(all_persona_ids())
    assert short_term <= registered, (
        "short-term personas missing from registry — update this test"
    )
    assert used & short_term, (
        "no short-term trader personas referenced — this seeder lost its "
        "'trader strategies' identity"
    )
