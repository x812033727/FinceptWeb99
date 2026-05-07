"""PR-4c: tests for the lesson demotion + archive cron path.

Covers `update_recent_hit_rates`, `demote_stale_lessons`, and
`archive_stale_lessons` end-to-end against the in-memory test DB.
The integration with sweep Phase 3 is left to the existing
backtest_sweep tests — these tests focus on the per-rule logic.
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.discussion_lesson import DiscussionLesson
from models.user import User, UserRole
from services import lesson_tier_service as svc


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    user = User(
        email=f"ld-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x", role=UserRole.analyst,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def _lesson(
    *,
    owner_id: uuid.UUID,
    market: str = "TW",
    tier: str = "episodic",
    usage: int = 0,
    hits: int = 0,
    last_used_offset_days: int | None = None,
    archived: bool = False,
    text: str = "外資轉多 + 半導體領漲 + 殖利率倒掛緩解",
) -> DiscussionLesson:
    last_used = (
        datetime.now(UTC) - timedelta(days=last_used_offset_days)
        if last_used_offset_days is not None else None
    )
    return DiscussionLesson(
        discussion_id=uuid4(),
        owner_user_id=owner_id,
        market=market,
        as_of_date=date.today(),
        category="other",
        lesson_text=text,
        lesson_text_hash=uuid.uuid4().hex,
        related_symbols=[],
        missed_winners=[],
        tier=tier,
        usage_count=usage,
        hit_count=hits,
        last_used_at=last_used,
        archived_at=datetime.now(UTC) if archived else None,
    )


# ── update_recent_hit_rates ──────────────────────────────────────


@pytest.mark.asyncio
async def test_update_recent_hit_rates_skips_unused_lessons(
    db_session: AsyncSession, owner: User,
):
    """A brand-new lesson with usage=0 → recent_hit_rate stays NULL."""
    db_session.add(_lesson(owner_id=owner.id, usage=0, hits=0))
    await db_session.commit()
    n = await svc.update_recent_hit_rates(db_session, market="TW")
    # No update needed since unused stays NULL
    assert n == 0


@pytest.mark.asyncio
async def test_update_recent_hit_rates_caps_at_window(
    db_session: AsyncSession, owner: User,
):
    """A lesson with usage=20, hits=15 → window-capped rate is
    min(15,10)/min(20,10) = 1.0."""
    db_session.add(_lesson(owner_id=owner.id, usage=20, hits=15))
    await db_session.commit()
    await svc.update_recent_hit_rates(db_session, market="TW")
    # Re-fetch via ORM to read recent_hit_rate_10
    from sqlalchemy import select as _sel
    lesson = (await db_session.scalars(_sel(DiscussionLesson))).first()
    assert lesson is not None
    assert lesson.recent_hit_rate_10 == 1.0


@pytest.mark.asyncio
async def test_update_recent_hit_rates_market_scoped(
    db_session: AsyncSession, owner: User,
):
    """A US lesson should NOT be touched by a TW market update."""
    db_session.add(_lesson(owner_id=owner.id, market="US", usage=10, hits=5))
    db_session.add(_lesson(owner_id=owner.id, market="TW", usage=10, hits=5))
    await db_session.commit()
    await svc.update_recent_hit_rates(db_session, market="TW")
    from sqlalchemy import select as _sel
    rows = list((await db_session.scalars(_sel(DiscussionLesson))).all())
    by_market = {r.market: r.recent_hit_rate_10 for r in rows}
    assert by_market["TW"] == 0.5
    assert by_market["US"] is None  # still untouched


# ── demote_stale_lessons ────────────────────────────────────────


@pytest.mark.asyncio
async def test_demote_semantic_lesson_with_low_recent_hit_rate(
    db_session: AsyncSession, owner: User,
):
    lesson = _lesson(owner_id=owner.id, tier="semantic", usage=10, hits=2)
    db_session.add(lesson)
    await db_session.commit()
    # Manually set recent_hit_rate_10 to simulate the cron's compute
    await svc.update_recent_hit_rates(db_session, market="TW")
    # 2 hits / 10 usages = 0.20 < 0.40 threshold
    demoted = await svc.demote_stale_lessons(db_session, market="TW")
    assert len(demoted) == 1
    assert demoted[0]["previous_tier"] == "semantic"
    await db_session.refresh(lesson)
    assert lesson.tier == "episodic"
    assert lesson.demoted_at is not None


@pytest.mark.asyncio
async def test_demote_skips_when_recent_rate_above_threshold(
    db_session: AsyncSession, owner: User,
):
    lesson = _lesson(owner_id=owner.id, tier="semantic", usage=10, hits=8)
    db_session.add(lesson)
    await db_session.commit()
    await svc.update_recent_hit_rates(db_session, market="TW")
    # 8/10 = 0.80 ≥ 0.40 → no demotion
    demoted = await svc.demote_stale_lessons(db_session, market="TW")
    assert demoted == []
    await db_session.refresh(lesson)
    assert lesson.tier == "semantic"


@pytest.mark.asyncio
async def test_demote_requires_min_usages(
    db_session: AsyncSession, owner: User,
):
    """A semantic lesson with too few usages stays put — can't
    trust the rolling rate computed over 1-2 samples."""
    lesson = _lesson(owner_id=owner.id, tier="semantic", usage=2, hits=0)
    db_session.add(lesson)
    await db_session.commit()
    await svc.update_recent_hit_rates(db_session, market="TW")
    demoted = await svc.demote_stale_lessons(db_session, market="TW")
    assert demoted == []
    await db_session.refresh(lesson)
    assert lesson.tier == "semantic"


@pytest.mark.asyncio
async def test_demote_skips_episodic_tier(
    db_session: AsyncSession, owner: User,
):
    """Episodic is already the bottom auto-tier; demote_stale only
    targets semantic."""
    lesson = _lesson(owner_id=owner.id, tier="episodic", usage=10, hits=2)
    db_session.add(lesson)
    await db_session.commit()
    await svc.update_recent_hit_rates(db_session, market="TW")
    demoted = await svc.demote_stale_lessons(db_session, market="TW")
    assert demoted == []


# ── archive_stale_lessons ───────────────────────────────────────


@pytest.mark.asyncio
async def test_archive_unused_with_low_hit_rate(
    db_session: AsyncSession, owner: User,
):
    """Both conditions hit: unused 90 days + recent rate 0.20."""
    lesson = _lesson(
        owner_id=owner.id, tier="episodic",
        usage=10, hits=2, last_used_offset_days=90,
    )
    db_session.add(lesson)
    await db_session.commit()
    await svc.update_recent_hit_rates(db_session, market="TW")
    archived = await svc.archive_stale_lessons(db_session, market="TW")
    assert len(archived) == 1
    await db_session.refresh(lesson)
    assert lesson.archived_at is not None


@pytest.mark.asyncio
async def test_archive_zero_hits_branch(
    db_session: AsyncSession, owner: User,
):
    """OR branch: usage>0 AND hits==0 → archive even with few
    usages (the lesson was tried + always missed)."""
    lesson = _lesson(
        owner_id=owner.id, tier="episodic",
        usage=2, hits=0, last_used_offset_days=90,
    )
    db_session.add(lesson)
    await db_session.commit()
    await svc.update_recent_hit_rates(db_session, market="TW")
    archived = await svc.archive_stale_lessons(db_session, market="TW")
    assert len(archived) == 1


@pytest.mark.asyncio
async def test_archive_skips_recently_used(
    db_session: AsyncSession, owner: User,
):
    """Used 5 days ago → still fresh, don't archive even if
    hit rate is bad."""
    lesson = _lesson(
        owner_id=owner.id, tier="episodic",
        usage=10, hits=2, last_used_offset_days=5,
    )
    db_session.add(lesson)
    await db_session.commit()
    await svc.update_recent_hit_rates(db_session, market="TW")
    archived = await svc.archive_stale_lessons(db_session, market="TW")
    assert archived == []


@pytest.mark.asyncio
async def test_archive_skips_structural_tier(
    db_session: AsyncSession, owner: User,
):
    """Structural lessons are admin-curated; never auto-archive."""
    lesson = _lesson(
        owner_id=owner.id, tier="structural",
        usage=10, hits=0, last_used_offset_days=200,
    )
    db_session.add(lesson)
    await db_session.commit()
    await svc.update_recent_hit_rates(db_session, market="TW")
    archived = await svc.archive_stale_lessons(db_session, market="TW")
    assert archived == []


@pytest.mark.asyncio
async def test_archive_idempotent_on_already_archived(
    db_session: AsyncSession, owner: User,
):
    """Running twice doesn't double-stamp `archived_at` or churn
    rows — the WHERE clause already filters archived_at IS NULL."""
    lesson = _lesson(
        owner_id=owner.id, tier="episodic",
        usage=2, hits=0, last_used_offset_days=90,
    )
    db_session.add(lesson)
    await db_session.commit()
    await svc.update_recent_hit_rates(db_session, market="TW")
    first = await svc.archive_stale_lessons(db_session, market="TW")
    assert len(first) == 1
    second = await svc.archive_stale_lessons(db_session, market="TW")
    assert second == []


@pytest.mark.asyncio
async def test_archived_lessons_excluded_from_fetch(
    db_session: AsyncSession, owner: User,
):
    """Archived lesson should NOT show up in
    `fetch_relevant_lessons` — that's the production read path."""
    from services.discussion_lesson_service import fetch_relevant_lessons
    db_session.add(_lesson(
        owner_id=owner.id, tier="episodic", text="this one stays" + "_" * 10,
        usage=1, hits=1,
    ))
    db_session.add(_lesson(
        owner_id=owner.id, tier="episodic",
        text="this one was archived" + "_" * 10, archived=True,
        usage=1, hits=0,
    ))
    await db_session.commit()
    out = await fetch_relevant_lessons(
        db_session, owner_user_id=owner.id, market="TW", limit=10,
    )
    texts = [s.lesson_text for s in out]
    assert any("this one stays" in t for t in texts)
    assert not any("archived" in t for t in texts)
