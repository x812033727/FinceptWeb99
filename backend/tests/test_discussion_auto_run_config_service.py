"""Tests for services.discussion_auto_run_config_service.

Exercises CRUD shape (insert vs update vs read) and validation
delegation to the discussion_service helpers — persona-id whitelist,
2-8 count bound, non-empty topic / rules.
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from models.discussion_auto_run_config import DiscussionAutoRunConfig
from models.user import User, UserRole
from services import discussion_auto_run_config_service as svc


@pytest_asyncio.fixture(autouse=True)
async def _isolate(db_session: AsyncSession):
    await db_session.execute(delete(DiscussionAutoRunConfig))
    await db_session.execute(delete(User))
    await db_session.commit()
    yield


@pytest_asyncio.fixture
async def user(db_session: AsyncSession) -> User:
    u = User(
        id=uuid.uuid4(),
        email="user@example.com",
        hashed_password="x",
        role=UserRole.viewer,
    )
    db_session.add(u)
    await db_session.commit()
    await db_session.refresh(u)
    return u


@pytest.mark.asyncio
async def test_get_returns_none_for_missing_user(
    db_session: AsyncSession, user: User,
):
    cfg = await svc.get_config(db_session, user_id=user.id)
    assert cfg is None


@pytest.mark.asyncio
async def test_upsert_inserts_then_updates(
    db_session: AsyncSession, user: User,
):
    cfg = await svc.upsert_config(
        db_session,
        user_id=user.id,
        enabled=True,
        persona_ids=["buffett", "lynch"],
        topic="t",
        rules="r",
    )
    assert cfg.enabled is True
    assert cfg.persona_ids == ["buffett", "lynch"]
    assert cfg.topic == "t"

    cfg2 = await svc.upsert_config(
        db_session,
        user_id=user.id,
        enabled=False,
        persona_ids=["buffett", "lynch", "soros"],
        topic="t2",
        rules="r2",
    )
    # PK is user_id, so this must be an UPDATE not a second INSERT.
    assert cfg2.user_id == cfg.user_id
    assert cfg2.enabled is False
    assert cfg2.persona_ids == ["buffett", "lynch", "soros"]
    assert cfg2.topic == "t2"


@pytest.mark.asyncio
async def test_upsert_rejects_too_few_personas(
    db_session: AsyncSession, user: User,
):
    with pytest.raises(ValueError):
        await svc.upsert_config(
            db_session,
            user_id=user.id,
            enabled=True,
            persona_ids=["buffett"],
            topic="t",
            rules="r",
        )


@pytest.mark.asyncio
async def test_upsert_rejects_empty_topic(
    db_session: AsyncSession, user: User,
):
    with pytest.raises(ValueError):
        await svc.upsert_config(
            db_session,
            user_id=user.id,
            enabled=True,
            persona_ids=["buffett", "lynch"],
            topic="   ",
            rules="r",
        )


@pytest.mark.asyncio
async def test_upsert_persists_send_email_flag(
    db_session: AsyncSession, user: User,
):
    """`send_email` (opt-in daily email report) defaults to False and
    round-trips through upsert. Pins the field through service →
    DB so a future refactor that forgets to copy it surfaces here."""
    cfg = await svc.upsert_config(
        db_session,
        user_id=user.id,
        enabled=True,
        persona_ids=["buffett", "lynch"],
        topic="t",
        rules="r",
    )
    assert cfg.send_email is False

    cfg2 = await svc.upsert_config(
        db_session,
        user_id=user.id,
        enabled=True,
        persona_ids=["buffett", "lynch"],
        topic="t",
        rules="r",
        send_email=True,
    )
    assert cfg2.send_email is True

    # Flipping it back must also stick — the update branch matters.
    cfg3 = await svc.upsert_config(
        db_session,
        user_id=user.id,
        enabled=True,
        persona_ids=["buffett", "lynch"],
        topic="t",
        rules="r",
        send_email=False,
    )
    assert cfg3.send_email is False


@pytest.mark.asyncio
async def test_list_enabled_filters_disabled(
    db_session: AsyncSession,
):
    a = User(id=uuid.uuid4(), email="a@x.com", hashed_password="x", role=UserRole.viewer)
    b = User(id=uuid.uuid4(), email="b@x.com", hashed_password="x", role=UserRole.viewer)
    db_session.add_all([a, b])
    await db_session.commit()

    await svc.upsert_config(
        db_session, user_id=a.id, enabled=True,
        persona_ids=["buffett", "lynch"], topic="t", rules="r",
    )
    await svc.upsert_config(
        db_session, user_id=b.id, enabled=False,
        persona_ids=["buffett", "lynch"], topic="t", rules="r",
    )

    rows = await svc.list_enabled(db_session)
    assert {r.user_id for r in rows} == {a.id}


def test_normalize_counts_defaults_to_merged_strategy_keys():
    assert svc.normalize_strategy_run_counts(None, legacy_enabled=True) == {
        "general": 1, "chip_quality": 0, "price_signal": 0,
    }
    assert svc.normalize_strategy_run_counts(None) == {
        "general": 0, "chip_quality": 0, "price_signal": 0,
    }


@pytest.mark.parametrize(
    "old_key", ["chip_momentum", "quality_growth", "breakout", "oversold_reversal"]
)
def test_normalize_counts_rejects_retired_keys(old_key):
    with pytest.raises(ValueError, match="unknown strategy"):
        svc.normalize_strategy_run_counts({old_key: 1})


def test_normalize_counts_bounds_on_new_keys():
    assert svc.normalize_strategy_run_counts({"chip_quality": 5})["chip_quality"] == 5
    with pytest.raises(ValueError, match="between 0 and 5"):
        svc.normalize_strategy_run_counts({"price_signal": 6})
