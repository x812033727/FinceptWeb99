"""PR-4a: tests for the strategy version history layer.

Covers `record_version`, `list_versions`, and `rollback_to_version`
end-to-end against the in-memory SQLite test DB. Verifies the
core invariant: exactly one `active` row per (strategy, artifact_kind)
at any time, and rollback is itself an auditable version row.
"""
from __future__ import annotations

import uuid
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.discussion_strategy_template import DiscussionStrategyTemplate
from models.user import User, UserRole
from services import strategy_version_service as svc


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    user = User(
        email=f"vsvc-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x", role=UserRole.analyst,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _make_strategy(
    db: AsyncSession, owner_id: uuid.UUID,
    *,
    initial_weights: dict | None = None,
    initial_curve: list | None = None,
) -> DiscussionStrategyTemplate:
    tmpl = DiscussionStrategyTemplate(
        id=uuid4(), owner_id=owner_id,
        name="t", topic="x", rules="", market="TW",
        persona_ids=["market_analyst"],
        persona_weights=initial_weights,
        calibration_curve=initial_curve,
    )
    db.add(tmpl)
    await db.commit()
    await db.refresh(tmpl)
    return tmpl


# ── record_version ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_record_is_v1_active(db_session: AsyncSession, owner: User):
    tmpl = await _make_strategy(db_session, owner.id)
    row = await svc.record_version(
        db_session, strategy_id=tmpl.id,
        artifact_kind="weights", payload={"buffett": 1.5},
        sample_count=42,
    )
    assert row.version_number == 1
    assert row.status == "active"
    assert row.payload == {"buffett": 1.5}


@pytest.mark.asyncio
async def test_second_record_supersedes_first(
    db_session: AsyncSession, owner: User,
):
    """Invariant: exactly one active row per (strategy, kind)."""
    tmpl = await _make_strategy(db_session, owner.id)
    v1 = await svc.record_version(
        db_session, strategy_id=tmpl.id,
        artifact_kind="weights", payload={"buffett": 1.5},
    )
    v2 = await svc.record_version(
        db_session, strategy_id=tmpl.id,
        artifact_kind="weights", payload={"buffett": 1.2},
    )
    await db_session.refresh(v1)
    await db_session.refresh(v2)
    assert v1.status == "superseded"
    assert v2.status == "active"
    assert v2.version_number == 2


@pytest.mark.asyncio
async def test_different_artifact_kinds_have_independent_numbering(
    db_session: AsyncSession, owner: User,
):
    """A weights v1 and a calibration_curve v1 can coexist —
    each kind has its own auto-increment counter."""
    tmpl = await _make_strategy(db_session, owner.id)
    w1 = await svc.record_version(
        db_session, strategy_id=tmpl.id,
        artifact_kind="weights", payload={"buffett": 1.0},
    )
    c1 = await svc.record_version(
        db_session, strategy_id=tmpl.id,
        artifact_kind="calibration_curve",
        payload=[{"raw": 0.5, "calibrated": 0.4}],
    )
    assert w1.version_number == 1
    assert c1.version_number == 1
    assert w1.status == "active"
    assert c1.status == "active"


@pytest.mark.asyncio
async def test_unknown_artifact_kind_raises(
    db_session: AsyncSession, owner: User,
):
    tmpl = await _make_strategy(db_session, owner.id)
    with pytest.raises(ValueError):
        await svc.record_version(
            db_session, strategy_id=tmpl.id,
            artifact_kind="bogus", payload={},
        )


# ── list_versions ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_versions_newest_first(
    db_session: AsyncSession, owner: User,
):
    tmpl = await _make_strategy(db_session, owner.id)
    for w in ({"a": 1.0}, {"a": 1.1}, {"a": 1.2}):
        await svc.record_version(
            db_session, strategy_id=tmpl.id,
            artifact_kind="weights", payload=w,
        )
    rows = await svc.list_versions(
        db_session, strategy_id=tmpl.id, artifact_kind="weights",
    )
    assert len(rows) == 3
    # Newest first
    assert rows[0].payload == {"a": 1.2}
    assert rows[2].payload == {"a": 1.0}


@pytest.mark.asyncio
async def test_list_versions_filter_by_kind(
    db_session: AsyncSession, owner: User,
):
    tmpl = await _make_strategy(db_session, owner.id)
    await svc.record_version(
        db_session, strategy_id=tmpl.id,
        artifact_kind="weights", payload={"a": 1.0},
    )
    await svc.record_version(
        db_session, strategy_id=tmpl.id,
        artifact_kind="calibration_curve", payload=[{"raw": 0.5, "calibrated": 0.4}],
    )
    weights_only = await svc.list_versions(
        db_session, strategy_id=tmpl.id, artifact_kind="weights",
    )
    curves_only = await svc.list_versions(
        db_session, strategy_id=tmpl.id, artifact_kind="calibration_curve",
    )
    assert len(weights_only) == 1
    assert len(curves_only) == 1
    assert weights_only[0].artifact_kind == "weights"
    assert curves_only[0].artifact_kind == "calibration_curve"


# ── rollback_to_version ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_rollback_promotes_older_version_and_audits(
    db_session: AsyncSession, owner: User,
):
    """Three versions land in order. Rollback to v1 should:
      - mark v3 (current active) as `rolled_back`
      - write v1's payload to the live column
      - INSERT a NEW row (v4) with v1's payload + audit notes
    """
    tmpl = await _make_strategy(db_session, owner.id)
    v1 = await svc.record_version(
        db_session, strategy_id=tmpl.id,
        artifact_kind="weights", payload={"a": 1.0},
    )
    await svc.record_version(
        db_session, strategy_id=tmpl.id,
        artifact_kind="weights", payload={"a": 1.1},
    )
    v3 = await svc.record_version(
        db_session, strategy_id=tmpl.id,
        artifact_kind="weights", payload={"a": 1.2},
    )
    # Live column reflects v3 only because the LEARNER would have
    # written it; record_version itself doesn't touch live. Set it
    # manually to mirror the production flow.
    tmpl.persona_weights = {"a": 1.2}
    await db_session.commit()

    result = await svc.rollback_to_version(
        db_session,
        owner_id=owner.id,
        strategy_id=tmpl.id,
        version_id=v1.id,
    )
    assert result["rolled_back_to_version"] == 1
    assert result["new_active_version"] == 4
    assert result["no_op"] is False

    await db_session.refresh(v3)
    assert v3.status == "rolled_back"

    await db_session.refresh(tmpl)
    assert tmpl.persona_weights == {"a": 1.0}

    # v4 was inserted as the new active
    rows = await svc.list_versions(
        db_session, strategy_id=tmpl.id, artifact_kind="weights",
    )
    assert rows[0].version_number == 4
    assert rows[0].status == "active"
    assert rows[0].payload == {"a": 1.0}
    assert "rolled back from v1" in (rows[0].notes or "")


@pytest.mark.asyncio
async def test_rollback_to_active_is_noop(
    db_session: AsyncSession, owner: User,
):
    tmpl = await _make_strategy(db_session, owner.id)
    v1 = await svc.record_version(
        db_session, strategy_id=tmpl.id,
        artifact_kind="weights", payload={"a": 1.0},
    )
    result = await svc.rollback_to_version(
        db_session,
        owner_id=owner.id,
        strategy_id=tmpl.id,
        version_id=v1.id,
    )
    assert result["no_op"] is True
    assert result["new_active_version"] == 1


@pytest.mark.asyncio
async def test_rollback_unknown_version_raises(
    db_session: AsyncSession, owner: User,
):
    tmpl = await _make_strategy(db_session, owner.id)
    with pytest.raises(ValueError):
        await svc.rollback_to_version(
            db_session,
            owner_id=owner.id,
            strategy_id=tmpl.id,
            version_id=uuid4(),
        )


@pytest.mark.asyncio
async def test_rollback_calibration_curve_syncs_template_metadata(
    db_session: AsyncSession, owner: User,
):
    """Rolling back a calibration_curve version should also reset
    the legacy `calibration_updated_at` and `calibration_sample_count`
    surface on the template — readers of those columns shouldn't
    drift from the live column."""
    tmpl = await _make_strategy(
        db_session, owner.id,
        initial_curve=[{"raw": 0.5, "calibrated": 0.4}],
    )
    v1 = await svc.record_version(
        db_session, strategy_id=tmpl.id,
        artifact_kind="calibration_curve",
        payload=[{"raw": 0.5, "calibrated": 0.4}],
        sample_count=30,
    )
    await svc.record_version(
        db_session, strategy_id=tmpl.id,
        artifact_kind="calibration_curve",
        payload=[{"raw": 0.5, "calibrated": 0.7}],
        sample_count=60,
    )
    tmpl.calibration_curve = [{"raw": 0.5, "calibrated": 0.7}]
    tmpl.calibration_sample_count = 60
    await db_session.commit()

    await svc.rollback_to_version(
        db_session,
        owner_id=owner.id,
        strategy_id=tmpl.id,
        version_id=v1.id,
    )
    await db_session.refresh(tmpl)
    assert tmpl.calibration_curve == [{"raw": 0.5, "calibrated": 0.4}]
    assert tmpl.calibration_sample_count == 30
    assert tmpl.calibration_updated_at is not None
