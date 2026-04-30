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
