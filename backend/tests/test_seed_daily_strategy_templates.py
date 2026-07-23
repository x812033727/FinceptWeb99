"""Unit tests for scripts.seed_daily_strategy_templates.

The seeding itself is trivial; what matters is that it is safe to run
after every deploy (idempotent) and that a deployment with no enabled
auto-run config reports a problem rather than a successful no-op —
"reported fine, wrote nothing" is exactly how the health metrics
managed to stay empty for months.
"""
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import scripts.seed_daily_strategy_templates as seeder
from models.discussion_auto_run_config import DiscussionAutoRunConfig
from models.discussion_strategy_template import DiscussionStrategyTemplate
from models.user import User, UserRole


class _PassthroughCM:
    def __init__(self, db_session):
        self._db = db_session

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    user = User(
        email=f"seed-{uuid4().hex[:8]}@test.com",
        hashed_password="x", role=UserRole.analyst,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _enabled_config(db: AsyncSession, owner: User) -> None:
    db.add(DiscussionAutoRunConfig(
        user_id=owner.id,
        enabled=True,
        persona_ids=["market_analyst"],
        topic="每日選股",
        rules="規則",
        market="TW",
        strategy_run_counts={
            "general": 1, "chip_quality": 1, "price_signal": 1,
        },
    ))
    await db.commit()


async def _seed(db_session, **kw) -> int:
    with patch.object(
        seeder, "AsyncSessionLocal",
        return_value=_PassthroughCM(db_session),
    ):
        return await seeder.seed(**kw)


@pytest.mark.asyncio
async def test_seeds_one_template_per_daily_strategy(
    db_session: AsyncSession, owner: User,
):
    await _enabled_config(db_session, owner)

    assert await _seed(db_session) == 3

    rows = list((await db_session.scalars(
        select(DiscussionStrategyTemplate).where(
            DiscussionStrategyTemplate.owner_id == owner.id,
        )
    )).all())
    assert {r.auto_run_strategy for r in rows} == {
        "general", "chip_quality", "price_signal",
    }
    # The template inherits the live config so health/maturity readers
    # describe the strategy that actually ran.
    assert all(r.market == "TW" for r in rows)
    assert all(r.topic == "每日選股" for r in rows)


@pytest.mark.asyncio
async def test_reseeding_updates_instead_of_duplicating(
    db_session: AsyncSession, owner: User,
):
    """Safe to run after every deploy. A duplicate row would split one
    strategy's samples across two `strategy_id`s and quietly halve
    every metric."""
    await _enabled_config(db_session, owner)
    await _seed(db_session)

    cfg = await db_session.scalar(select(DiscussionAutoRunConfig))
    cfg.topic = "改過的題目"
    await db_session.commit()

    assert await _seed(db_session) == 3

    rows = list((await db_session.scalars(
        select(DiscussionStrategyTemplate).where(
            DiscussionStrategyTemplate.owner_id == owner.id,
        )
    )).all())
    assert len(rows) == 3
    assert all(r.topic == "改過的題目" for r in rows)


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(
    db_session: AsyncSession, owner: User,
):
    await _enabled_config(db_session, owner)

    assert await _seed(db_session, dry_run=True) == 3

    rows = list((await db_session.scalars(
        select(DiscussionStrategyTemplate)
    )).all())
    assert rows == []


@pytest.mark.asyncio
async def test_no_enabled_config_seeds_nothing(
    db_session: AsyncSession, owner: User,
):
    """Auto-run switched off is a legitimate state, but it must not be
    reported as a successful seed."""
    assert await _seed(db_session) == 0


def test_main_exits_nonzero_when_nothing_was_seeded():
    """Sync: `main` owns the event loop via `asyncio.run`."""
    with patch.object(
        seeder, "seed", new=AsyncMock(return_value=0),
    ), patch("sys.argv", ["seed_daily_strategy_templates"]):
        assert seeder.main() == 1


def test_main_exits_zero_after_seeding():
    with patch.object(
        seeder, "seed", new=AsyncMock(return_value=3),
    ), patch("sys.argv", ["seed_daily_strategy_templates"]):
        assert seeder.main() == 0
