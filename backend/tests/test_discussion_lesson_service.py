"""Unit tests for the discussion learning loop service.

Covers the write path (quality gates, dedup, category coercion,
per-discussion caps) and the read path (owner scoping, time decay,
symbol boost, backtest-time filter).

In-memory SQLite via the existing test stack — no FinMind / yfinance
contact, no mock LLM. The service writes commits, so we use the
shared `db_session` fixture and assert via direct SQL.
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.discussion import Discussion
from models.discussion_lesson import DiscussionLesson
from models.user import User, UserRole
from services import discussion_lesson_service as svc


def _user(suffix: str = "") -> User:
    return User(
        id=uuid.uuid4(),
        email=f"u{suffix}@x.com",
        hashed_password="x",
        role=UserRole.analyst,
        is_active=True,
    )


def _disc(owner_id: uuid.UUID, *, market: str = "TW",
          as_of: date | None = date(2026, 3, 23)) -> Discussion:
    return Discussion(
        id=uuid.uuid4(),
        owner_id=owner_id,
        topic="t", rules="r",
        persona_ids=["a", "b"],
        market=market,
        status="done",
        current_round=1,
        as_of_date=as_of,
    )


# ── extract_and_persist_lessons (write path) ────────────────────


@pytest.mark.asyncio
async def test_extract_persists_lessons_with_unknown_category_falls_back_to_other(
    db_session: AsyncSession,
):
    """Anything outside the 5-category enum becomes `other` rather
    than rejecting the whole row. Keeps the synthesizer's free-form
    output salvageable."""
    u = _user("a")
    d = _disc(u.id)
    db_session.add_all([u, d])
    await db_session.commit()

    out = await svc.extract_and_persist_lessons(
        db_session,
        discussion_id=d.id, owner_user_id=u.id,
        market="TW", as_of_date=d.as_of_date,
        lessons_payload=[{
            "category": "made_up_category",
            "lesson_text": "外資台指期 1500 口多單訊號被忽略，導致沒看到 +5% 動能。",
        }],
    )
    assert len(out) == 1
    assert out[0].category == "other"


@pytest.mark.asyncio
async def test_extract_handles_malformed_payload_without_breaking(
    db_session: AsyncSession,
):
    """Synthesizer occasionally emits None / dict / str at this key.
    Each shape returns [] without raising so the conclusion commit
    isn't disturbed."""
    u = _user("b")
    d = _disc(u.id)
    db_session.add_all([u, d])
    await db_session.commit()

    for bad in (None, {}, "lessons", 0, [None, "x", 42]):
        out = await svc.extract_and_persist_lessons(
            db_session,
            discussion_id=d.id, owner_user_id=u.id,
            market="TW", as_of_date=d.as_of_date,
            lessons_payload=bad,
        )
        assert out == []


@pytest.mark.asyncio
async def test_extract_drops_lessons_under_min_length(
    db_session: AsyncSession,
):
    u = _user("c")
    d = _disc(u.id)
    db_session.add_all([u, d])
    await db_session.commit()

    out = await svc.extract_and_persist_lessons(
        db_session,
        discussion_id=d.id, owner_user_id=u.id,
        market="TW", as_of_date=d.as_of_date,
        lessons_payload=[
            {"category": "other", "lesson_text": "太短"},
            {"category": "other", "lesson_text": "外資個股期 2330 連 5 日多單建倉被忽略"},
        ],
    )
    assert len(out) == 1
    assert "外資個股期" in out[0].lesson_text


@pytest.mark.asyncio
async def test_extract_truncates_lessons_over_max_length(
    db_session: AsyncSession,
):
    u = _user("d")
    d = _disc(u.id)
    db_session.add_all([u, d])
    await db_session.commit()

    long_text = "外資台指期動能訊號被忽略，" * 30  # > 200 chars
    out = await svc.extract_and_persist_lessons(
        db_session,
        discussion_id=d.id, owner_user_id=u.id,
        market="TW", as_of_date=d.as_of_date,
        lessons_payload=[{"category": "missing_data", "lesson_text": long_text}],
    )
    assert len(out) == 1
    assert len(out[0].lesson_text) == 200


@pytest.mark.asyncio
async def test_extract_dedups_via_hash_within_60d_window(
    db_session: AsyncSession,
):
    """Same lesson_text written twice within the dedup window — the
    second call is a no-op."""
    u = _user("e")
    d = _disc(u.id)
    d2 = _disc(u.id)
    db_session.add_all([u, d, d2])
    await db_session.commit()

    text = "外資台指期 連 5 日多單 +1500 口被忽略，導致漏掉 +5% 動能。"
    out1 = await svc.extract_and_persist_lessons(
        db_session,
        discussion_id=d.id, owner_user_id=u.id,
        market="TW", as_of_date=d.as_of_date,
        lessons_payload=[{"category": "wrong_signal_weight", "lesson_text": text}],
    )
    assert len(out1) == 1

    out2 = await svc.extract_and_persist_lessons(
        db_session,
        discussion_id=d2.id, owner_user_id=u.id,
        market="TW", as_of_date=d.as_of_date,
        lessons_payload=[{"category": "wrong_signal_weight", "lesson_text": text}],
    )
    assert out2 == []
    rows = (await db_session.scalars(
        select(DiscussionLesson).where(DiscussionLesson.owner_user_id == u.id)
    )).all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_extract_caps_lessons_per_discussion_at_5(
    db_session: AsyncSession,
):
    """Synthesizer dumps 30 wandering takeaways → cap kicks in,
    only the first 5 (in payload order) get persisted."""
    u = _user("f")
    d = _disc(u.id)
    db_session.add_all([u, d])
    await db_session.commit()

    lessons = [
        {"category": "other",
         "lesson_text": f"訊號 {i} 沒被權衡好，未來請更注意這條 ({i})。"}
        for i in range(30)
    ]
    out = await svc.extract_and_persist_lessons(
        db_session,
        discussion_id=d.id, owner_user_id=u.id,
        market="TW", as_of_date=d.as_of_date,
        lessons_payload=lessons,
    )
    assert len(out) == 5


# ── fetch_relevant_lessons (read path) ──────────────────────────


@pytest.mark.asyncio
async def test_fetch_filters_by_owner_user_id(db_session: AsyncSession):
    """Owner A's lesson must NOT appear in owner B's fetch — the
    multi-admin separation guarantee."""
    a, b = _user("a2"), _user("b2")
    da, db_ = _disc(a.id), _disc(b.id)
    db_session.add_all([a, b, da, db_])
    await db_session.commit()

    await svc.extract_and_persist_lessons(
        db_session,
        discussion_id=da.id, owner_user_id=a.id,
        market="TW", as_of_date=da.as_of_date,
        lessons_payload=[{"category": "other",
                          "lesson_text": "A's secret lesson 外資 動能 1500 口"}],
    )

    rows = await svc.fetch_relevant_lessons(
        db_session, owner_user_id=b.id, market="TW",
    )
    assert rows == []
    rows_a = await svc.fetch_relevant_lessons(
        db_session, owner_user_id=a.id, market="TW",
    )
    assert len(rows_a) == 1


@pytest.mark.asyncio
async def test_fetch_excludes_future_as_of_in_backtest_mode(
    db_session: AsyncSession,
):
    """Backtest discussion at as_of=2026-03-15 must NOT see a lesson
    whose own as_of_date is 2026-04-01 — that's information from the
    future."""
    u = _user("g")
    d_old = _disc(u.id, as_of=date(2026, 3, 1))
    d_new = _disc(u.id, as_of=date(2026, 4, 1))
    db_session.add_all([u, d_old, d_new])
    await db_session.commit()

    await svc.extract_and_persist_lessons(
        db_session,
        discussion_id=d_old.id, owner_user_id=u.id,
        market="TW", as_of_date=date(2026, 3, 1),
        lessons_payload=[{"category": "other",
                          "lesson_text": "Old lesson 外資 buy heavy enough"}],
    )
    await svc.extract_and_persist_lessons(
        db_session,
        discussion_id=d_new.id, owner_user_id=u.id,
        market="TW", as_of_date=date(2026, 4, 1),
        lessons_payload=[{"category": "other",
                          "lesson_text": "Future leak — should not surface"}],
    )

    rows = await svc.fetch_relevant_lessons(
        db_session, owner_user_id=u.id, market="TW",
        discussion_as_of=date(2026, 3, 15),
    )
    assert len(rows) == 1
    assert "Old lesson" in rows[0].lesson_text


@pytest.mark.asyncio
async def test_fetch_includes_all_when_anchor_is_none(
    db_session: AsyncSession,
):
    """Live mode (`discussion_as_of=None`) bypasses the time filter
    so personas see every lesson regardless of the lesson's own
    as_of_date — matches "live" semantics."""
    u = _user("h")
    d = _disc(u.id, as_of=date(2030, 1, 1))   # absurdly future
    db_session.add_all([u, d])
    await db_session.commit()

    await svc.extract_and_persist_lessons(
        db_session,
        discussion_id=d.id, owner_user_id=u.id,
        market="TW", as_of_date=date(2030, 1, 1),
        lessons_payload=[{"category": "other",
                          "lesson_text": "Future lesson visible in live mode"}],
    )

    rows = await svc.fetch_relevant_lessons(
        db_session, owner_user_id=u.id, market="TW",
        discussion_as_of=None,
    )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_fetch_applies_time_decay(db_session: AsyncSession):
    """Two lessons, one ancient + one fresh → fresh ranks higher
    even with no symbol boost. Verifies the recency-weighted score
    actually orders results."""
    u = _user("i")
    d_old = _disc(u.id, as_of=date(2025, 1, 1))
    d_new = _disc(u.id, as_of=date(2026, 4, 1))
    db_session.add_all([u, d_old, d_new])
    await db_session.commit()

    # Distinct text so dedup doesn't swallow the second write.
    await svc.extract_and_persist_lessons(
        db_session,
        discussion_id=d_old.id, owner_user_id=u.id,
        market="TW", as_of_date=date(2025, 1, 1),
        lessons_payload=[{"category": "other",
                          "lesson_text": "Ancient miss: 外資撤碼但未察覺"}],
    )
    await svc.extract_and_persist_lessons(
        db_session,
        discussion_id=d_new.id, owner_user_id=u.id,
        market="TW", as_of_date=date(2026, 4, 1),
        lessons_payload=[{"category": "other",
                          "lesson_text": "Recent miss: 個股期 動能訊號被忽略"}],
    )

    rows = await svc.fetch_relevant_lessons(
        db_session, owner_user_id=u.id, market="TW",
        discussion_as_of=date(2026, 5, 1),
    )
    assert len(rows) == 2
    # Recent lesson should rank above ancient one
    assert "個股期" in rows[0].lesson_text


@pytest.mark.asyncio
async def test_fetch_boosts_focus_symbol_matches(db_session: AsyncSession):
    """Symbol boost (1.5×) flips the order so a slightly older
    same-symbol lesson outranks a newer unrelated one."""
    u = _user("j")
    # Two lessons within a week of each other: ~7 days time-decay
    # difference is small (recency ratio ≈ 0.92), and the 1.5×
    # symbol boost easily overcomes that.
    d_match = _disc(u.id, as_of=date(2026, 4, 1))
    d_other = _disc(u.id, as_of=date(2026, 4, 8))
    db_session.add_all([u, d_match, d_other])
    await db_session.commit()

    await svc.extract_and_persist_lessons(
        db_session,
        discussion_id=d_match.id, owner_user_id=u.id,
        market="TW", as_of_date=date(2026, 4, 1),
        lessons_payload=[{
            "category": "wrong_signal_weight",
            "lesson_text": "2330 外資個股期 1500 口多單訊號被低估",
            "related_symbols": ["2330"],
        }],
    )
    await svc.extract_and_persist_lessons(
        db_session,
        discussion_id=d_other.id, owner_user_id=u.id,
        market="TW", as_of_date=date(2026, 4, 8),
        lessons_payload=[{
            "category": "missed_sector",
            "lesson_text": "金融類股輪動被忽略導致漏接 +4% 動能",
            "related_symbols": ["2882"],
        }],
    )

    # Without symbol filter: newer (2882) wins on pure recency
    rows_pure = await svc.fetch_relevant_lessons(
        db_session, owner_user_id=u.id, market="TW",
        discussion_as_of=date(2026, 5, 1),
    )
    assert "2882" in (rows_pure[0].related_symbols or [])

    # With focus={2330}: boost lifts 2330's lesson to top
    rows_boosted = await svc.fetch_relevant_lessons(
        db_session, owner_user_id=u.id, market="TW",
        focus_symbols={"2330"}, discussion_as_of=date(2026, 5, 1),
    )
    assert "2330" in (rows_boosted[0].related_symbols or [])


@pytest.mark.asyncio
async def test_fetch_respects_limit(db_session: AsyncSession):
    u = _user("k")
    d = _disc(u.id, as_of=date(2026, 3, 1))
    db_session.add_all([u, d])
    await db_session.commit()

    for i in range(7):
        await svc.extract_and_persist_lessons(
            db_session,
            discussion_id=d.id, owner_user_id=u.id,
            market="TW", as_of_date=date(2026, 3, 1) - timedelta(days=i),
            lessons_payload=[{"category": "other",
                              "lesson_text": f"Lesson {i}: 外資 訊號被低估 {i}"}],
        )

    rows = await svc.fetch_relevant_lessons(
        db_session, owner_user_id=u.id, market="TW", limit=3,
        discussion_as_of=date(2026, 4, 1),
    )
    assert len(rows) == 3


# ── admin CRUD ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_recent_lessons_owner_scoped(db_session: AsyncSession):
    a, b = _user("la"), _user("lb")
    da, db_ = _disc(a.id), _disc(b.id)
    db_session.add_all([a, b, da, db_])
    await db_session.commit()

    await svc.extract_and_persist_lessons(
        db_session,
        discussion_id=da.id, owner_user_id=a.id,
        market="TW", as_of_date=da.as_of_date,
        lessons_payload=[{"category": "other",
                          "lesson_text": "A 的教訓 — 外資台指期轉買訊號被忽略"}],
    )
    await svc.extract_and_persist_lessons(
        db_session,
        discussion_id=db_.id, owner_user_id=b.id,
        market="TW", as_of_date=db_.as_of_date,
        lessons_payload=[{"category": "other",
                          "lesson_text": "B 的教訓 — 散戶融資餘額激增 警示"}],
    )

    a_rows = await svc.list_recent_lessons(db_session, owner_user_id=a.id)
    assert len(a_rows) == 1
    assert a_rows[0].owner_user_id == a.id


@pytest.mark.asyncio
async def test_delete_lesson_rejects_other_owners(db_session: AsyncSession):
    a, b = _user("da"), _user("db")
    da = _disc(a.id)
    db_session.add_all([a, b, da])
    await db_session.commit()
    written = await svc.extract_and_persist_lessons(
        db_session,
        discussion_id=da.id, owner_user_id=a.id,
        market="TW", as_of_date=da.as_of_date,
        lessons_payload=[{"category": "other",
                          "lesson_text": "owner A's only lesson 外資 動能"}],
    )
    lesson_id = written[0].id

    # B cannot delete A's lesson
    ok = await svc.delete_lesson(db_session, lesson_id=lesson_id, owner_user_id=b.id)
    assert ok is False
    still_there = await db_session.scalar(
        select(DiscussionLesson).where(DiscussionLesson.id == lesson_id),
    )
    assert still_there is not None

    # A can delete it
    ok = await svc.delete_lesson(db_session, lesson_id=lesson_id, owner_user_id=a.id)
    assert ok is True


# ── small wins ──────────────────────────────────────────────────


def test_normalize_symbols_strips_and_dedups():
    out = svc._normalize_symbols([" 2330 ", "2330", "", None, "2454"])
    assert out == ["2330", "2454"]


def test_summary_to_dict_round_trip():
    s = svc.LessonSummary(
        as_of_date="2026-03-23",
        category="missed_sector",
        lesson_text="x",
        related_symbols=["2330"],
        missed_winners=["6505"],
    )
    out = svc.summary_to_dict(s)
    assert out["category"] == "missed_sector"
    assert out["related_symbols"] == ["2330"]


def test_anchor_uses_today_for_live(monkeypatch):
    """Score with anchor=today gives recency=1 for a lesson dated
    today (sanity check the half-life math doesn't blow up at 0)."""
    today = datetime.now(UTC).date()
    lesson = DiscussionLesson(
        discussion_id=uuid.uuid4(), owner_user_id=uuid.uuid4(),
        market="TW", as_of_date=today, category="other",
        lesson_text="x", lesson_text_hash="h",
        related_symbols=[], missed_winners=[],
    )
    score = svc._score(
        lesson, anchor=today, focus_symbols=set(), halflife_days=60,
    )
    assert score == pytest.approx(1.0)
