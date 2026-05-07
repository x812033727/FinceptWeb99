"""PR-4c: tests for the persona status governance — auto-freeze
based on weight version history + manual override + roster filter.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.discussion_strategy_template import DiscussionStrategyTemplate
from models.strategy_version import StrategyVersion
from models.user import User, UserRole
from services import persona_status_service as svc


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    user = User(
        email=f"ps-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x", role=UserRole.analyst,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _make_strategy(
    db: AsyncSession, owner_id: uuid.UUID,
    *, persona_ids: list[str] | None = None,
    persona_status: dict[str, str] | None = None,
) -> DiscussionStrategyTemplate:
    tmpl = DiscussionStrategyTemplate(
        id=uuid4(), owner_id=owner_id,
        name="t", topic="x", rules="", market="TW",
        persona_ids=persona_ids or ["a", "b", "c"],
        persona_status=persona_status or {},
    )
    db.add(tmpl)
    await db.commit()
    await db.refresh(tmpl)
    return tmpl


async def _add_weight_version(
    db: AsyncSession, strategy_id: uuid.UUID,
    *, version: int, weights: dict[str, float],
    fit_at: datetime | None = None,
) -> StrategyVersion:
    row = StrategyVersion(
        id=uuid4(),
        strategy_id=strategy_id,
        version_number=version,
        artifact_kind="weights",
        payload=weights,
        sample_count=42,
        fit_at=fit_at or datetime.now(UTC),
        status="active" if version == 5 else "superseded",
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


# ── pure helpers ────────────────────────────────────────────────


def test_filter_roster_drops_frozen_keeps_shadow():
    """Frozen → out. Shadow → kept (output dropped at synth side
    instead). Active / unmapped → kept."""
    out = svc.filter_roster_by_status(
        persona_ids=["a", "b", "c", "d"],
        persona_status={"a": "frozen", "b": "shadow", "c": "active"},
    )
    assert out == ["b", "c", "d"]


def test_filter_roster_handles_none_map():
    out = svc.filter_roster_by_status(
        persona_ids=["a", "b"], persona_status=None,
    )
    assert out == ["a", "b"]


def test_is_shadow_persona_only_true_for_shadow():
    m = {"a": "shadow", "b": "active", "c": "frozen"}
    assert svc.is_shadow_persona("a", m) is True
    assert svc.is_shadow_persona("b", m) is False
    assert svc.is_shadow_persona("c", m) is False
    assert svc.is_shadow_persona("d", m) is False   # unmapped → active


# ── auto_freeze_underperformers ─────────────────────────────────


@pytest.mark.asyncio
async def test_auto_freeze_skips_with_insufficient_history(
    db_session: AsyncSession, owner: User,
):
    """Need FREEZE_LOOKBACK_SWEEPS (5) versions to trust the
    pattern. With only 3 versions, no freeze should fire."""
    tmpl = await _make_strategy(db_session, owner.id)
    for i in range(1, 4):
        await _add_weight_version(
            db_session, tmpl.id, version=i,
            weights={"a": 0.01, "b": 1.0, "c": 1.0},
        )
    out = await svc.auto_freeze_underperformers(
        db_session, strategy_id=tmpl.id,
    )
    assert out == []


@pytest.mark.asyncio
async def test_auto_freeze_persona_consistently_below_threshold(
    db_session: AsyncSession, owner: User,
):
    """Persona 'a' has weight ≤ 0.05 across all 5 most recent
    versions → frozen. Personas 'b' and 'c' have varied weights →
    stay active."""
    tmpl = await _make_strategy(db_session, owner.id)
    for i in range(1, 6):
        await _add_weight_version(
            db_session, tmpl.id, version=i,
            weights={"a": 0.02, "b": 1.0, "c": 1.0},
            fit_at=datetime.now(UTC) - timedelta(days=6 - i),
        )
    out = await svc.auto_freeze_underperformers(
        db_session, strategy_id=tmpl.id,
    )
    assert out == ["a"]
    await db_session.refresh(tmpl)
    assert tmpl.persona_status["a"] == "frozen"
    assert "b" not in tmpl.persona_status
    assert "c" not in tmpl.persona_status


@pytest.mark.asyncio
async def test_auto_freeze_skips_when_one_recent_high(
    db_session: AsyncSession, owner: User,
):
    """If 'a' was below threshold in 4 of 5 but came back to 1.0
    in the most recent fit, don't freeze — recovery in progress."""
    tmpl = await _make_strategy(db_session, owner.id)
    for i in range(1, 5):
        await _add_weight_version(
            db_session, tmpl.id, version=i,
            weights={"a": 0.02},
            fit_at=datetime.now(UTC) - timedelta(days=6 - i),
        )
    await _add_weight_version(
        db_session, tmpl.id, version=5,
        weights={"a": 1.5},   # bounce back
        fit_at=datetime.now(UTC),
    )
    out = await svc.auto_freeze_underperformers(
        db_session, strategy_id=tmpl.id,
    )
    assert out == []


@pytest.mark.asyncio
async def test_auto_freeze_skips_already_frozen(
    db_session: AsyncSession, owner: User,
):
    """Idempotent: already-frozen personas don't re-stamp the
    update timestamp (avoids notification spam from rerunning)."""
    tmpl = await _make_strategy(
        db_session, owner.id,
        persona_status={"a": "frozen"},
    )
    for i in range(1, 6):
        await _add_weight_version(
            db_session, tmpl.id, version=i,
            weights={"a": 0.02},
        )
    out = await svc.auto_freeze_underperformers(
        db_session, strategy_id=tmpl.id,
    )
    assert out == []   # already frozen, no change


@pytest.mark.asyncio
async def test_auto_freeze_skips_shadow_persona(
    db_session: AsyncSession, owner: User,
):
    """A persona deliberately put in shadow mode should NOT auto-
    freeze — operator chose shadow on purpose."""
    tmpl = await _make_strategy(
        db_session, owner.id,
        persona_status={"a": "shadow"},
    )
    for i in range(1, 6):
        await _add_weight_version(
            db_session, tmpl.id, version=i,
            weights={"a": 0.02},
        )
    out = await svc.auto_freeze_underperformers(
        db_session, strategy_id=tmpl.id,
    )
    assert out == []
    await db_session.refresh(tmpl)
    assert tmpl.persona_status["a"] == "shadow"


# ── set_persona_status (manual API) ──────────────────────────────


@pytest.mark.asyncio
async def test_set_persona_status_freeze_then_unfreeze(
    db_session: AsyncSession, owner: User,
):
    tmpl = await _make_strategy(db_session, owner.id)
    # Freeze
    await svc.set_persona_status(
        db_session,
        owner_id=owner.id, strategy_id=tmpl.id,
        persona_id="a", status="frozen",
    )
    await db_session.refresh(tmpl)
    assert tmpl.persona_status["a"] == "frozen"
    # Unfreeze
    await svc.set_persona_status(
        db_session,
        owner_id=owner.id, strategy_id=tmpl.id,
        persona_id="a", status="active",
    )
    await db_session.refresh(tmpl)
    # Active is encoded as ABSENCE from the map
    assert "a" not in tmpl.persona_status


@pytest.mark.asyncio
async def test_set_persona_status_invalid_raises(
    db_session: AsyncSession, owner: User,
):
    tmpl = await _make_strategy(db_session, owner.id)
    with pytest.raises(ValueError):
        await svc.set_persona_status(
            db_session,
            owner_id=owner.id, strategy_id=tmpl.id,
            persona_id="a", status="bogus",
        )


@pytest.mark.asyncio
async def test_set_persona_status_unknown_strategy_raises(
    db_session: AsyncSession, owner: User,
):
    with pytest.raises(ValueError):
        await svc.set_persona_status(
            db_session,
            owner_id=owner.id, strategy_id=uuid4(),
            persona_id="a", status="frozen",
        )
