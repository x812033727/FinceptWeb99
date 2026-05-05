"""Tests for `services.strategy_template_service` (PR-A).

Covers:
  - Input validation (name / market / persona_ids / bounds)
  - CRUD lifecycle including soft-delete + include_deleted flag
  - Owner scoping — one user's template is invisible to another
  - update_template re-validates merged shape
  - set_persona_weights stamps timestamp + replaces map
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.user import User, UserRole
from services import strategy_template_service as svc


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    user = User(
        email=f"strat-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x",
        role=UserRole.analyst,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def other_owner(db_session: AsyncSession) -> User:
    user = User(
        email=f"strat2-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x",
        role=UserRole.analyst,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def _kw(**over):
    base = dict(
        name="High-vol breakout",
        description="watch breakouts on TW small-caps",
        topic="今日是否進場高量突破標的",
        rules="只看 5%+ 漲幅且成交量 >= 5 倍均量",
        market="TW",
        persona_ids=["bull", "bear"],
        default_rounds=2,
        default_concurrency=1,
        default_auto_post_mortem=True,
    )
    base.update(over)
    return base


# ── create / validation ────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_persists_template(
    db_session: AsyncSession, owner: User,
):
    tmpl = await svc.create_template(
        db_session, owner_id=owner.id, **_kw()
    )
    assert tmpl.id is not None
    assert tmpl.deleted_at is None
    assert tmpl.persona_weights == {}
    assert tmpl.weights_updated_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    {"name": ""},
    {"name": " "},
    {"topic": ""},
    {"rules": ""},
    {"market": "JP"},
    {"persona_ids": []},
    {"default_rounds": 0},
    {"default_rounds": 99},
    {"default_concurrency": 0},
    {"default_concurrency": 99},
])
async def test_create_rejects_bad_input(
    db_session: AsyncSession, owner: User, bad: dict,
):
    with pytest.raises(ValueError):
        await svc.create_template(
            db_session, owner_id=owner.id, **_kw(**bad)
        )


# ── list / get / owner scoping ─────────────────────────────────


@pytest.mark.asyncio
async def test_list_excludes_other_owners(
    db_session: AsyncSession, owner: User, other_owner: User,
):
    await svc.create_template(db_session, owner_id=owner.id, **_kw())
    await svc.create_template(
        db_session, owner_id=other_owner.id, **_kw(name="them")
    )
    rows = await svc.list_templates(db_session, owner_id=owner.id)
    assert len(rows) == 1
    assert rows[0].owner_id == owner.id


@pytest.mark.asyncio
async def test_get_returns_none_for_other_owner(
    db_session: AsyncSession, owner: User, other_owner: User,
):
    tmpl = await svc.create_template(
        db_session, owner_id=other_owner.id, **_kw()
    )
    fetched = await svc.get_template(
        db_session, template_id=tmpl.id, owner_id=owner.id,
    )
    assert fetched is None


# ── update / re-validation ──────────────────────────────────────


@pytest.mark.asyncio
async def test_update_applies_partial_patch(
    db_session: AsyncSession, owner: User,
):
    tmpl = await svc.create_template(
        db_session, owner_id=owner.id, **_kw()
    )
    updated = await svc.update_template(
        db_session, tmpl,
        patch={"name": "v2", "default_rounds": 3,
               "description": "fresh"},
    )
    assert updated.name == "v2"
    assert updated.default_rounds == 3
    assert updated.description == "fresh"
    # untouched fields preserved
    assert updated.market == "TW"


@pytest.mark.asyncio
async def test_update_revalidates_merged_shape(
    db_session: AsyncSession, owner: User,
):
    tmpl = await svc.create_template(
        db_session, owner_id=owner.id, **_kw()
    )
    with pytest.raises(ValueError):
        await svc.update_template(
            db_session, tmpl, patch={"persona_ids": []}
        )


# ── soft-delete ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_soft_delete_hides_from_default_list(
    db_session: AsyncSession, owner: User,
):
    tmpl = await svc.create_template(
        db_session, owner_id=owner.id, **_kw()
    )
    await svc.soft_delete_template(db_session, tmpl)

    visible = await svc.list_templates(db_session, owner_id=owner.id)
    assert visible == []

    with_deleted = await svc.list_templates(
        db_session, owner_id=owner.id, include_deleted=True,
    )
    assert len(with_deleted) == 1

    # PR-B aggregator path: include_deleted=True still finds it.
    found = await svc.get_template(
        db_session, template_id=tmpl.id,
        owner_id=owner.id, include_deleted=True,
    )
    assert found is not None
    assert found.deleted_at is not None


# ── set_persona_weights (PR-C entry point) ──────────────────────


@pytest.mark.asyncio
async def test_set_persona_weights_stamps_timestamp(
    db_session: AsyncSession, owner: User,
):
    tmpl = await svc.create_template(
        db_session, owner_id=owner.id, **_kw()
    )
    weights = {"bull": 1.4, "bear": 0.7}
    updated = await svc.set_persona_weights(
        db_session, tmpl, weights=weights,
    )
    assert updated.persona_weights == weights
    assert updated.weights_updated_at is not None

    # Calling again replaces the map (not merges).
    updated2 = await svc.set_persona_weights(
        db_session, tmpl, weights={"newbie": 1.0},
    )
    assert updated2.persona_weights == {"newbie": 1.0}
