"""Tests for `services.lesson_tier_service` (PR-B2).

In-memory SQLite fixtures + direct ORM writes — no LLM, no
context builder. Each test seeds the rows it needs, calls the
service entry point, and asserts the persisted state.

Coverage:
  - record_lesson_usage bumps usage_count + last_used_at and
    handles empty / missing-id input gracefully
  - record_lesson_outcome walks round contexts and bumps
    hit_count only on verdict=win
  - promote_eligible_lessons promotes the right rows + leaves
    structural alone
  - promote_to_structural is idempotent + returns None for
    unknown ids
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.discussion import Discussion
from models.discussion_lesson import DiscussionLesson
from models.discussion_round_context import DiscussionRoundContext
from models.user import User, UserRole
from services import lesson_tier_service as svc


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    u = User(
        email=f"tier-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x", role=UserRole.analyst,
    )
    db_session.add(u)
    await db_session.commit()
    await db_session.refresh(u)
    return u


async def _seed_lesson(
    db: AsyncSession,
    *,
    owner_id: UUID,
    text: str = "教訓內容範本",
    tier: str = "episodic",
    usage: int = 0,
    hits: int = 0,
) -> DiscussionLesson:
    fake_disc_id = uuid.uuid4()
    db.add(Discussion(
        id=fake_disc_id, owner_id=owner_id,
        topic="t", rules="r",
        persona_ids=["a"], market="TW",
        status="done", current_round=1,
    ))
    await db.commit()
    row = DiscussionLesson(
        discussion_id=fake_disc_id, owner_user_id=owner_id,
        market="TW", as_of_date=date(2026, 5, 5),
        category="other",
        lesson_text=text + "_" + uuid.uuid4().hex[:6],   # avoid hash dedup
        lesson_text_hash=uuid.uuid4().hex,
        related_symbols=[], missed_winners=[],
        tier=tier, usage_count=usage, hit_count=hits,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


# ── record_lesson_usage ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_usage_bumps_count_and_timestamp(
    db_session: AsyncSession, owner: User,
):
    a = await _seed_lesson(db_session, owner_id=owner.id)
    assert a.usage_count == 0
    assert a.last_used_at is None

    n = await svc.record_lesson_usage(db_session, lesson_ids=[a.id])
    assert n == 1
    await db_session.refresh(a)
    refreshed = a
    assert refreshed.usage_count == 1
    assert refreshed.last_used_at is not None


@pytest.mark.asyncio
async def test_record_usage_handles_empty_input(
    db_session: AsyncSession, owner: User,
):
    n = await svc.record_lesson_usage(db_session, lesson_ids=[])
    assert n == 0


@pytest.mark.asyncio
async def test_record_usage_filters_none_ids(
    db_session: AsyncSession, owner: User,
):
    a = await _seed_lesson(db_session, owner_id=owner.id)
    n = await svc.record_lesson_usage(
        db_session, lesson_ids=[None, a.id, None],
    )
    assert n == 1


@pytest.mark.asyncio
async def test_record_usage_bumps_each_call(
    db_session: AsyncSession, owner: User,
):
    a = await _seed_lesson(db_session, owner_id=owner.id)
    await svc.record_lesson_usage(db_session, lesson_ids=[a.id])
    await svc.record_lesson_usage(db_session, lesson_ids=[a.id])
    await svc.record_lesson_usage(db_session, lesson_ids=[a.id])
    await db_session.refresh(a)
    refreshed = a
    assert refreshed.usage_count == 3


# ── record_lesson_outcome ────────────────────────────────────────


async def _seed_discussion_with_lessons(
    db: AsyncSession,
    *,
    owner_id: UUID,
    verdict: str | None,
    market_lesson_ids: list[int],
) -> Discussion:
    """Build a discussion + a round context whose recent_lessons.market
    block references the supplied lesson IDs (mirrors the shape
    summary_to_dict produces)."""
    d = Discussion(
        id=uuid.uuid4(), owner_id=owner_id,
        topic="t", rules="r",
        persona_ids=["a"], market="TW",
        status="done", current_round=1,
        verdict=verdict,
        as_of_date=date(2026, 5, 5),
    )
    db.add(d)
    await db.commit()
    await db.refresh(d)

    db.add(DiscussionRoundContext(
        discussion_id=d.id,
        round=1,
        context={
            "recent_lessons": {
                "market": [
                    {"id": lid, "as_of_date": "2026-04-01",
                     "category": "other", "lesson_text": "x",
                     "related_symbols": [], "missed_winners": []}
                    for lid in market_lesson_ids
                ],
                "per_symbol": {},
            },
        },
        captured_at=datetime.now(UTC),
    ))
    await db.commit()
    return d


@pytest.mark.asyncio
async def test_record_outcome_bumps_hits_only_on_win(
    db_session: AsyncSession, owner: User,
):
    lesson = await _seed_lesson(db_session, owner_id=owner.id)
    d = await _seed_discussion_with_lessons(
        db_session, owner_id=owner.id,
        verdict="win", market_lesson_ids=[lesson.id],
    )
    out = await svc.record_lesson_outcome(
        db_session, discussion_id=d.id,
    )
    assert out["verdict"] == "win"
    assert out["lesson_ids"] == [lesson.id]
    assert out["updated"] == 1
    await db_session.refresh(lesson)
    assert lesson.hit_count == 1


@pytest.mark.asyncio
async def test_record_outcome_no_op_on_loss(
    db_session: AsyncSession, owner: User,
):
    lesson = await _seed_lesson(db_session, owner_id=owner.id)
    d = await _seed_discussion_with_lessons(
        db_session, owner_id=owner.id,
        verdict="loss", market_lesson_ids=[lesson.id],
    )
    out = await svc.record_lesson_outcome(
        db_session, discussion_id=d.id,
    )
    assert out["verdict"] == "loss"
    assert out["updated"] == 0
    await db_session.refresh(lesson)
    assert lesson.hit_count == 0


@pytest.mark.asyncio
async def test_record_outcome_dedupes_lesson_ids_across_rounds(
    db_session: AsyncSession, owner: User,
):
    """Same lesson appearing in multiple round contexts should bump
    hit_count exactly once per outcome resolution."""
    lesson = await _seed_lesson(db_session, owner_id=owner.id)
    d = Discussion(
        id=uuid.uuid4(), owner_id=owner.id,
        topic="t", rules="r",
        persona_ids=["a"], market="TW",
        status="done", current_round=2,
        verdict="win",
        as_of_date=date(2026, 5, 5),
    )
    db_session.add(d)
    await db_session.commit()
    await db_session.refresh(d)

    snap = {
        "recent_lessons": {
            "market": [{
                "id": lesson.id, "as_of_date": "2026-04-01",
                "category": "other", "lesson_text": "x",
                "related_symbols": [], "missed_winners": [],
            }],
            "per_symbol": {},
        },
    }
    db_session.add_all([
        DiscussionRoundContext(
            discussion_id=d.id, round=1,
            context=snap, captured_at=datetime.now(UTC),
        ),
        DiscussionRoundContext(
            discussion_id=d.id, round=2,
            context=snap, captured_at=datetime.now(UTC),
        ),
    ])
    await db_session.commit()

    out = await svc.record_lesson_outcome(
        db_session, discussion_id=d.id,
    )
    assert out["lesson_ids"] == [lesson.id]
    await db_session.refresh(lesson)
    assert lesson.hit_count == 1   # NOT 2


@pytest.mark.asyncio
async def test_record_outcome_walks_per_symbol_block(
    db_session: AsyncSession, owner: User,
):
    """Lessons in the per_symbol block should also bump hit_count."""
    a = await _seed_lesson(db_session, owner_id=owner.id)
    b = await _seed_lesson(db_session, owner_id=owner.id)
    d = Discussion(
        id=uuid.uuid4(), owner_id=owner.id,
        topic="t", rules="r",
        persona_ids=["a"], market="TW",
        status="done", current_round=1, verdict="win",
        as_of_date=date(2026, 5, 5),
    )
    db_session.add(d)
    await db_session.commit()
    await db_session.refresh(d)

    db_session.add(DiscussionRoundContext(
        discussion_id=d.id, round=1,
        context={
            "recent_lessons": {
                "market": [{
                    "id": a.id, "as_of_date": "2026-04-01",
                    "category": "other", "lesson_text": "x",
                    "related_symbols": [], "missed_winners": [],
                }],
                "per_symbol": {
                    "2330": [{
                        "id": b.id, "as_of_date": "2026-04-01",
                        "category": "other", "lesson_text": "y",
                        "related_symbols": ["2330"],
                        "missed_winners": [],
                    }],
                },
            },
        },
        captured_at=datetime.now(UTC),
    ))
    await db_session.commit()

    out = await svc.record_lesson_outcome(
        db_session, discussion_id=d.id,
    )
    assert sorted(out["lesson_ids"]) == sorted([a.id, b.id])
    await db_session.refresh(a)
    await db_session.refresh(b)
    assert a.hit_count == 1
    assert b.hit_count == 1


@pytest.mark.asyncio
async def test_record_outcome_skips_unknown_discussion(
    db_session: AsyncSession,
):
    out = await svc.record_lesson_outcome(
        db_session, discussion_id=uuid.uuid4(),
    )
    assert out["verdict"] is None
    assert out["updated"] == 0


# ── promote_eligible_lessons ─────────────────────────────────────


@pytest.mark.asyncio
async def test_promote_episodic_with_high_hit_rate(
    db_session: AsyncSession, owner: User,
):
    eligible = await _seed_lesson(
        db_session, owner_id=owner.id,
        usage=5, hits=4,   # 0.8 hit rate, above 0.6
    )
    promoted = await svc.promote_eligible_lessons(
        db_session, market="TW",
    )
    assert len(promoted) == 1
    assert promoted[0]["lesson_id"] == eligible.id
    await db_session.refresh(eligible)
    assert eligible.tier == "semantic"
    assert eligible.promoted_at is not None


@pytest.mark.asyncio
async def test_promote_skips_low_hit_rate(
    db_session: AsyncSession, owner: User,
):
    a = await _seed_lesson(
        db_session, owner_id=owner.id,
        usage=10, hits=4,   # 0.4 hit rate, below threshold
    )
    promoted = await svc.promote_eligible_lessons(
        db_session, market="TW",
    )
    assert promoted == []
    await db_session.refresh(a)
    refreshed = a
    assert refreshed.tier == "episodic"


@pytest.mark.asyncio
async def test_promote_skips_low_usage(
    db_session: AsyncSession, owner: User,
):
    a = await _seed_lesson(
        db_session, owner_id=owner.id,
        usage=3, hits=3,   # high rate but not enough samples
    )
    promoted = await svc.promote_eligible_lessons(
        db_session, market="TW",
    )
    assert promoted == []
    await db_session.refresh(a)
    refreshed = a
    assert refreshed.tier == "episodic"


@pytest.mark.asyncio
async def test_promote_does_not_touch_semantic_or_structural(
    db_session: AsyncSession, owner: User,
):
    s = await _seed_lesson(
        db_session, owner_id=owner.id,
        tier="semantic", usage=20, hits=20,
    )
    st = await _seed_lesson(
        db_session, owner_id=owner.id,
        tier="structural", usage=20, hits=20,
    )
    promoted = await svc.promote_eligible_lessons(
        db_session, market="TW",
    )
    assert promoted == []
    await db_session.refresh(s)
    await db_session.refresh(st)
    assert s.tier == "semantic"
    assert st.tier == "structural"


@pytest.mark.asyncio
async def test_promote_market_isolation(
    db_session: AsyncSession, owner: User,
):
    """Eligible TW lesson promotes; eligible US lesson stays."""
    tw_eligible = await _seed_lesson(
        db_session, owner_id=owner.id,
        usage=5, hits=4,
    )
    # Manually set market on a fresh lesson for US.
    fake_disc_id = uuid.uuid4()
    db_session.add(Discussion(
        id=fake_disc_id, owner_id=owner.id,
        topic="t", rules="r", persona_ids=["a"],
        market="US", status="done", current_round=1,
    ))
    await db_session.commit()
    us_lesson = DiscussionLesson(
        discussion_id=fake_disc_id, owner_user_id=owner.id,
        market="US", as_of_date=date(2026, 5, 5),
        category="other",
        lesson_text="us_" + uuid.uuid4().hex[:6],
        lesson_text_hash=uuid.uuid4().hex,
        related_symbols=[], missed_winners=[],
        tier="episodic", usage_count=5, hit_count=4,
    )
    db_session.add(us_lesson)
    await db_session.commit()
    await db_session.refresh(us_lesson)

    promoted = await svc.promote_eligible_lessons(
        db_session, market="TW",
    )
    assert {p["lesson_id"] for p in promoted} == {tw_eligible.id}

    await db_session.refresh(tw_eligible)
    await db_session.refresh(us_lesson)
    assert tw_eligible.tier == "semantic"
    assert us_lesson.tier == "episodic"


# ── promote_to_structural ────────────────────────────────────────


@pytest.mark.asyncio
async def test_promote_to_structural_flips_tier(
    db_session: AsyncSession, owner: User,
):
    a = await _seed_lesson(db_session, owner_id=owner.id)
    refreshed = await svc.promote_to_structural(
        db_session, lesson_id=a.id,
    )
    assert refreshed is not None
    assert refreshed.tier == "structural"
    assert refreshed.promoted_at is not None


@pytest.mark.asyncio
async def test_promote_to_structural_idempotent(
    db_session: AsyncSession, owner: User,
):
    a = await _seed_lesson(
        db_session, owner_id=owner.id, tier="structural",
    )
    refreshed = await svc.promote_to_structural(
        db_session, lesson_id=a.id,
    )
    assert refreshed is not None
    assert refreshed.tier == "structural"


@pytest.mark.asyncio
async def test_promote_to_structural_returns_none_for_unknown_id(
    db_session: AsyncSession,
):
    out = await svc.promote_to_structural(db_session, lesson_id=99999)
    assert out is None


# ── constants sanity ─────────────────────────────────────────────


def test_promotion_thresholds_match_plan():
    assert svc.PROMOTE_MIN_USAGE == 5
    assert svc.PROMOTE_MIN_HIT_RATE == 0.6
    assert set(svc.VALID_TIERS) == {"episodic", "semantic", "structural"}
