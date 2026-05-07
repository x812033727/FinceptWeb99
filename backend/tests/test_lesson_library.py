"""PR-5b: tests for `list_lessons_with_metrics` — the filterable
lesson browser that powers the LessonLibraryPage.
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.discussion_lesson import DiscussionLesson
from models.user import User, UserRole
from services import discussion_lesson_service as svc


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    user = User(
        email=f"lib-{uuid.uuid4().hex[:8]}@test.com",
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
    text: str = "外資轉多 + 半導體領漲 + 殖利率倒掛緩解",
    category: str = "correct_signal_combo",
    usage: int = 0,
    hits: int = 0,
    recent_rate: float | None = None,
    archived: bool = False,
    last_used_offset_days: int | None = None,
    created_offset_days: int = 0,
) -> DiscussionLesson:
    last_used = (
        datetime.now(UTC) - timedelta(days=last_used_offset_days)
        if last_used_offset_days is not None else None
    )
    created = datetime.now(UTC) - timedelta(days=created_offset_days)
    return DiscussionLesson(
        discussion_id=uuid4(),
        owner_user_id=owner_id,
        market=market,
        as_of_date=date.today(),
        category=category,
        lesson_text=text,
        lesson_text_hash=uuid.uuid4().hex,
        related_symbols=[],
        missed_winners=[],
        tier=tier,
        usage_count=usage,
        hit_count=hits,
        last_used_at=last_used,
        recent_hit_rate_10=recent_rate,
        archived_at=datetime.now(UTC) if archived else None,
        created_at=created,
    )


# ── basic listing + total ────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_list(db_session: AsyncSession, owner: User):
    out = await svc.list_lessons_with_metrics(
        db_session, owner_user_id=owner.id,
    )
    assert out["total"] == 0
    assert out["items"] == []


@pytest.mark.asyncio
async def test_list_with_total_count(
    db_session: AsyncSession, owner: User,
):
    for _ in range(7):
        db_session.add(_lesson(owner_id=owner.id))
    await db_session.commit()
    out = await svc.list_lessons_with_metrics(
        db_session, owner_user_id=owner.id, limit=3,
    )
    assert out["total"] == 7
    assert len(out["items"]) == 3
    assert out["limit"] == 3
    assert out["offset"] == 0


# ── filters ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_archive_filter_excludes_active_when_archived_true(
    db_session: AsyncSession, owner: User,
):
    db_session.add(_lesson(owner_id=owner.id))
    db_session.add(_lesson(owner_id=owner.id, archived=True))
    await db_session.commit()
    active_only = await svc.list_lessons_with_metrics(
        db_session, owner_user_id=owner.id, archived=False,
    )
    archived_only = await svc.list_lessons_with_metrics(
        db_session, owner_user_id=owner.id, archived=True,
    )
    assert active_only["total"] == 1
    assert archived_only["total"] == 1


@pytest.mark.asyncio
async def test_market_filter(db_session: AsyncSession, owner: User):
    db_session.add(_lesson(owner_id=owner.id, market="TW"))
    db_session.add(_lesson(owner_id=owner.id, market="US"))
    db_session.add(_lesson(owner_id=owner.id, market="GLOBAL"))
    await db_session.commit()
    out = await svc.list_lessons_with_metrics(
        db_session, owner_user_id=owner.id, market="US",
    )
    assert out["total"] == 1
    assert out["items"][0]["market"] == "US"


@pytest.mark.asyncio
async def test_tier_filter(db_session: AsyncSession, owner: User):
    db_session.add(_lesson(owner_id=owner.id, tier="episodic"))
    db_session.add(_lesson(owner_id=owner.id, tier="semantic"))
    db_session.add(_lesson(owner_id=owner.id, tier="structural"))
    await db_session.commit()
    out = await svc.list_lessons_with_metrics(
        db_session, owner_user_id=owner.id, tier="semantic",
    )
    assert out["total"] == 1
    assert out["items"][0]["tier"] == "semantic"


@pytest.mark.asyncio
async def test_search_substring_in_text(
    db_session: AsyncSession, owner: User,
):
    db_session.add(_lesson(
        owner_id=owner.id,
        text="外資台指期由空轉多 + 半導體領漲 + 殖利率倒掛緩解",
    ))
    db_session.add(_lesson(
        owner_id=owner.id,
        text="散戶日當沖比過熱導致回檔風險升高",
    ))
    await db_session.commit()
    out = await svc.list_lessons_with_metrics(
        db_session, owner_user_id=owner.id, search="台指期",
    )
    assert out["total"] == 1
    assert "台指期" in out["items"][0]["lesson_text"]


@pytest.mark.asyncio
async def test_search_substring_in_category(
    db_session: AsyncSession, owner: User,
):
    db_session.add(_lesson(
        owner_id=owner.id, category="correct_signal_combo",
    ))
    db_session.add(_lesson(
        owner_id=owner.id, category="missing_signal_category",
    ))
    await db_session.commit()
    out = await svc.list_lessons_with_metrics(
        db_session, owner_user_id=owner.id, search="missing",
    )
    assert out["total"] == 1
    assert out["items"][0]["category"] == "missing_signal_category"


# ── sort ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sort_by_hit_rate_nulls_last(
    db_session: AsyncSession, owner: User,
):
    """A lesson with NULL recent_hit_rate_10 should sort AFTER
    those with rates set, even when the rates are low."""
    db_session.add(_lesson(
        owner_id=owner.id, recent_rate=None, text="no rate yet" + "_" * 20,
    ))
    db_session.add(_lesson(
        owner_id=owner.id, recent_rate=0.20, text="low rate" + "_" * 20,
    ))
    db_session.add(_lesson(
        owner_id=owner.id, recent_rate=0.80, text="high rate" + "_" * 20,
    ))
    await db_session.commit()
    out = await svc.list_lessons_with_metrics(
        db_session, owner_user_id=owner.id, sort="hit_rate",
    )
    items = out["items"]
    # Highest first, then 0.20, then NULL
    assert "high rate" in items[0]["lesson_text"]
    assert "low rate" in items[1]["lesson_text"]
    assert "no rate yet" in items[2]["lesson_text"]


@pytest.mark.asyncio
async def test_sort_by_usage(db_session: AsyncSession, owner: User):
    db_session.add(_lesson(
        owner_id=owner.id, usage=2, text="low-usage" + "_" * 20,
    ))
    db_session.add(_lesson(
        owner_id=owner.id, usage=20, text="high-usage" + "_" * 20,
    ))
    await db_session.commit()
    out = await svc.list_lessons_with_metrics(
        db_session, owner_user_id=owner.id, sort="usage",
    )
    assert "high-usage" in out["items"][0]["lesson_text"]


# ── owner scoping ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_owner_scoped(db_session: AsyncSession, owner: User):
    """A lesson owned by a different user must NOT leak through."""
    other = User(
        email=f"other-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x", role=UserRole.analyst,
    )
    db_session.add(other)
    await db_session.commit()
    await db_session.refresh(other)

    db_session.add(_lesson(owner_id=other.id))
    db_session.add(_lesson(owner_id=owner.id))
    await db_session.commit()
    out = await svc.list_lessons_with_metrics(
        db_session, owner_user_id=owner.id,
    )
    assert out["total"] == 1
