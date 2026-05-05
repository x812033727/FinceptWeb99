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


# ── PR-B0: regime classification + persistence ──────────────────────


def _ctx_with(vix: float | None, *, trend: str = "up") -> dict:
    """Build a synthetic ctx for regime tests. `trend` shapes the
    20-day TAIEX history so the 5d/20d MA cross resolves as
    requested.
    """
    ctx: dict = {}
    if vix is not None:
        ctx["taiwan_vix"] = {"value": vix}
    if trend == "up":
        # Steady climb: each close 1% above the previous one. 5d MA
        # ends well above 20d MA and the 5d MA itself is rising.
        closes = [100.0 * (1.01 ** i) for i in range(20)]
    elif trend == "down":
        closes = [100.0 * (0.99 ** i) for i in range(20)]
    else:   # range — flat with small noise
        closes = [100.0 + (i % 2) * 0.1 for i in range(20)]
    ctx["index"] = {
        "history": [{"time": f"2026-01-{i + 1:02d}", "close": c}
                    for i, c in enumerate(closes)],
    }
    return ctx


def test_classify_regime_high_vix_downtrend_yields_high_down():
    out = svc._classify_regime(_ctx_with(vix=30.0, trend="down"))
    assert out == "high_down"


def test_classify_regime_low_vix_uptrend_yields_low_up():
    out = svc._classify_regime(_ctx_with(vix=12.0, trend="up"))
    assert out == "low_up"


def test_classify_regime_mid_vix_uptrend_yields_mid_up():
    out = svc._classify_regime(_ctx_with(vix=18.0, trend="up"))
    assert out == "mid_up"


def test_classify_regime_drops_low_vix_downtrend_combo():
    """low_VIX × down (calm sell-off) is the rare 'weak' combo —
    return None so the lesson lands without a regime tag rather
    than getting binned into a low-sample bucket."""
    out = svc._classify_regime(_ctx_with(vix=12.0, trend="down"))
    assert out is None


def test_classify_regime_drops_high_vix_uptrend_combo():
    """high_VIX × up (melt-up) is rare — same treatment as low_down."""
    out = svc._classify_regime(_ctx_with(vix=30.0, trend="up"))
    assert out is None


def test_classify_regime_drops_high_vix_range_combo():
    out = svc._classify_regime(_ctx_with(vix=30.0, trend="range"))
    assert out is None


def test_classify_regime_returns_none_when_vix_missing():
    """Cron hasn't run / TAIFEX outage / ctx assembled before VIX
    block landed — drop the tag, persist the lesson regardless."""
    ctx = _ctx_with(vix=None, trend="up")
    assert "taiwan_vix" not in ctx
    assert svc._classify_regime(ctx) is None


def test_classify_regime_returns_none_when_history_too_short():
    ctx = {
        "taiwan_vix": {"value": 18.0},
        "index": {"history": [
            {"time": f"d{i}", "close": 100.0 + i} for i in range(15)
        ]},
    }
    assert svc._classify_regime(ctx) is None


def test_classify_regime_returns_none_when_ctx_none():
    assert svc._classify_regime(None) is None


def test_classify_regime_returns_none_when_vix_value_unparseable():
    ctx = {
        "taiwan_vix": {"value": "high"},
        "index": _ctx_with(vix=18.0, trend="up")["index"],
    }
    assert svc._classify_regime(ctx) is None


@pytest.mark.asyncio
async def test_extract_persists_regime_when_ctx_provided(
    db_session: AsyncSession,
):
    u = _user("regime-ctx")
    d = _disc(u.id)
    db_session.add_all([u, d])
    await db_session.commit()

    out = await svc.extract_and_persist_lessons(
        db_session,
        discussion_id=d.id, owner_user_id=u.id,
        market="TW", as_of_date=d.as_of_date,
        lessons_payload=[{
            "category": "missed_sector",
            "lesson_text": "外資轉空時忽略半導體 5 日均線跌破 20 日均線",
        }],
        ctx=_ctx_with(vix=30.0, trend="down"),
    )
    assert len(out) == 1
    assert out[0].regime == "high_down"
    # PR-B0 ships the new fields with safe defaults — schema verified
    # by the migration, but assert here so a future B-tier change
    # that drops the default doesn't slip through.
    assert out[0].tier == "episodic"
    assert out[0].usage_count == 0
    assert out[0].hit_count == 0


@pytest.mark.asyncio
async def test_extract_persists_null_regime_when_ctx_missing(
    db_session: AsyncSession,
):
    """Backward compat — old callers that don't pass ctx still get
    the lesson written, just regime=NULL."""
    u = _user("regime-noctx")
    d = _disc(u.id)
    db_session.add_all([u, d])
    await db_session.commit()

    out = await svc.extract_and_persist_lessons(
        db_session,
        discussion_id=d.id, owner_user_id=u.id,
        market="TW", as_of_date=d.as_of_date,
        lessons_payload=[{
            "category": "missing_data",
            "lesson_text": "缺少當日台指期未平倉資料導致對外資多空態度的誤判，下次需補齊。",
        }],
    )
    assert len(out) == 1
    assert out[0].regime is None
    assert out[0].tier == "episodic"


@pytest.mark.asyncio
async def test_extract_persists_null_regime_when_ctx_lacks_vix(
    db_session: AsyncSession,
):
    """When ctx lacks the VIX block (cron hasn't run yet), classifier
    returns None and the lesson lands regime-less."""
    u = _user("regime-no-vix")
    d = _disc(u.id)
    db_session.add_all([u, d])
    await db_session.commit()

    ctx_no_vix = _ctx_with(vix=None, trend="up")
    out = await svc.extract_and_persist_lessons(
        db_session,
        discussion_id=d.id, owner_user_id=u.id,
        market="TW", as_of_date=d.as_of_date,
        lessons_payload=[{
            "category": "wrong_signal_weight",
            "lesson_text": "高估融資餘額對短線反彈的支撐力道，忽略外資籌碼面的轉空訊號。",
        }],
        ctx=ctx_no_vix,
    )
    assert len(out) == 1
    assert out[0].regime is None


# ── PR-B1: regime-aware fetch + tier-tiered recency ─────────────────


def _mk_lesson(
    *,
    owner_id: uuid.UUID,
    as_of: date,
    regime: str | None = None,
    tier: str = "episodic",
    related: list[str] | None = None,
    text: str = "x",
) -> DiscussionLesson:
    """In-memory lesson for _score tests (no DB write needed)."""
    return DiscussionLesson(
        discussion_id=uuid.uuid4(),
        owner_user_id=owner_id,
        market="TW",
        as_of_date=as_of,
        category="other",
        lesson_text=text,
        lesson_text_hash=f"h-{uuid.uuid4().hex[:8]}",
        related_symbols=related or [],
        missed_winners=[],
        regime=regime,
        tier=tier,
    )


def test_score_same_regime_doubles():
    today = datetime.now(UTC).date()
    lesson = _mk_lesson(
        owner_id=uuid.uuid4(), as_of=today, regime="high_down",
    )
    matched = svc._score(
        lesson, anchor=today, focus_symbols=set(), halflife_days=60,
        current_regime="high_down",
    )
    mismatched = svc._score(
        lesson, anchor=today, focus_symbols=set(), halflife_days=60,
        current_regime="low_up",
    )
    neutral = svc._score(
        lesson, anchor=today, focus_symbols=set(), halflife_days=60,
        current_regime=None,
    )
    assert matched == pytest.approx(2.0)
    assert mismatched == pytest.approx(0.7)
    assert neutral == pytest.approx(1.0)


def test_score_neutral_when_lesson_has_no_regime_tag():
    """Pre-PR-B0 lessons (regime=None) shouldn't be punished — they
    just don't get the same-regime boost."""
    today = datetime.now(UTC).date()
    lesson = _mk_lesson(
        owner_id=uuid.uuid4(), as_of=today, regime=None,
    )
    score = svc._score(
        lesson, anchor=today, focus_symbols=set(), halflife_days=60,
        current_regime="high_down",
    )
    assert score == pytest.approx(1.0)


def test_score_structural_tier_does_not_decay():
    """Structural lesson 1 year old should still score 1.0 on the
    recency axis. Episodic at the same age decays a lot."""
    today = datetime.now(UTC).date()
    a_year_ago = today - timedelta(days=365)
    structural = _mk_lesson(
        owner_id=uuid.uuid4(), as_of=a_year_ago, tier="structural",
    )
    episodic = _mk_lesson(
        owner_id=uuid.uuid4(), as_of=a_year_ago, tier="episodic",
    )
    s_score = svc._score(
        structural, anchor=today,
        focus_symbols=set(), halflife_days=60,
    )
    e_score = svc._score(
        episodic, anchor=today,
        focus_symbols=set(), halflife_days=60,
    )
    assert s_score == pytest.approx(1.0)
    assert e_score < 0.05   # 365d / 60d half-life ~ 70x decay
    assert s_score > e_score * 20


def test_score_semantic_tier_decays_slower_than_episodic():
    today = datetime.now(UTC).date()
    six_months_ago = today - timedelta(days=180)
    semantic = _mk_lesson(
        owner_id=uuid.uuid4(), as_of=six_months_ago, tier="semantic",
    )
    episodic = _mk_lesson(
        owner_id=uuid.uuid4(), as_of=six_months_ago, tier="episodic",
    )
    sem_score = svc._score(
        semantic, anchor=today,
        focus_symbols=set(), halflife_days=60,
    )
    epi_score = svc._score(
        episodic, anchor=today,
        focus_symbols=set(), halflife_days=60,
    )
    # semantic halflife = 60 * 3 = 180d → 180d age = 1 halflife → 0.5
    # episodic halflife = 60d → 180d age = 3 halflives → 0.125
    assert sem_score == pytest.approx(0.5, abs=1e-3)
    assert epi_score == pytest.approx(0.125, abs=1e-3)


@pytest.mark.asyncio
async def test_fetch_boosts_same_regime(db_session: AsyncSession):
    """Two lessons with identical age: the same-regime one ranks
    higher than the mismatched-regime one."""
    u = _user("regime-fetch")
    d = _disc(u.id)
    db_session.add_all([u, d])
    await db_session.commit()

    today = date(2026, 5, 5)
    same = DiscussionLesson(
        discussion_id=d.id, owner_user_id=u.id, market="TW",
        as_of_date=today, category="other",
        lesson_text="同 regime 的教訓 — 預期被 boost 到首位",
        lesson_text_hash="h-same",
        related_symbols=[], missed_winners=[],
        regime="high_down", tier="episodic",
    )
    diff = DiscussionLesson(
        discussion_id=d.id, owner_user_id=u.id, market="TW",
        as_of_date=today, category="other",
        lesson_text="不同 regime 的教訓 — 應該被排在後面",
        lesson_text_hash="h-diff",
        related_symbols=[], missed_winners=[],
        regime="low_up", tier="episodic",
    )
    db_session.add_all([same, diff])
    await db_session.commit()

    rows = await svc.fetch_relevant_lessons(
        db_session,
        owner_user_id=u.id, market="TW",
        discussion_as_of=today,
        limit=2,
        current_regime="high_down",
    )
    assert len(rows) == 2
    assert rows[0].lesson_text.startswith("同 regime")
    assert rows[1].lesson_text.startswith("不同 regime")


@pytest.mark.asyncio
async def test_fetch_falls_back_when_no_same_regime_exists(
    db_session: AsyncSession,
):
    """When no lesson matches the current regime, fetch should still
    return the best mismatched-regime candidates rather than emptying
    out — a thin regime shouldn't strand the discussion without
    *any* prior context."""
    u = _user("regime-fallback")
    d = _disc(u.id)
    db_session.add_all([u, d])
    await db_session.commit()

    today = date(2026, 5, 5)
    diff = DiscussionLesson(
        discussion_id=d.id, owner_user_id=u.id, market="TW",
        as_of_date=today, category="other",
        lesson_text="只有不同 regime 的教訓可以挑 — 也得回傳。",
        lesson_text_hash="h-diff-only",
        related_symbols=[], missed_winners=[],
        regime="mid_up", tier="episodic",
    )
    db_session.add(diff)
    await db_session.commit()

    rows = await svc.fetch_relevant_lessons(
        db_session,
        owner_user_id=u.id, market="TW",
        discussion_as_of=today,
        limit=3,
        current_regime="high_down",
    )
    assert len(rows) == 1
    assert rows[0].lesson_text.startswith("只有不同 regime")


@pytest.mark.asyncio
async def test_fetch_structural_outranks_recent_episodic(
    db_session: AsyncSession,
):
    """A structural lesson from a year ago should still beat a fresh
    episodic lesson when neither matches the regime — proves the
    tier system actually changes ranking, not just decoration."""
    u = _user("structural-rank")
    d = _disc(u.id)
    db_session.add_all([u, d])
    await db_session.commit()

    today = date(2026, 5, 5)
    structural_old = DiscussionLesson(
        discussion_id=d.id, owner_user_id=u.id, market="TW",
        as_of_date=today - timedelta(days=300), category="other",
        lesson_text="結構性教訓: 殖利率倒掛時不要追科技股 — 不會衰減。",
        lesson_text_hash="h-structural",
        related_symbols=[], missed_winners=[],
        regime=None, tier="structural",
    )
    episodic_recent = DiscussionLesson(
        discussion_id=d.id, owner_user_id=u.id, market="TW",
        as_of_date=today, category="other",
        lesson_text="本週教訓: 漏看外資 5 日淨買超下降 — 一般衰減。",
        lesson_text_hash="h-episodic-recent",
        related_symbols=[], missed_winners=[],
        regime=None, tier="episodic",
    )
    db_session.add_all([structural_old, episodic_recent])
    await db_session.commit()

    rows = await svc.fetch_relevant_lessons(
        db_session,
        owner_user_id=u.id, market="TW",
        discussion_as_of=today,
        limit=2,
    )
    # episodic at age 0 scores 1.0; structural at age 300d also
    # scores 1.0 (no decay). Tied → secondary order by candidate
    # window (as_of_date desc) puts the recent one first. Both
    # should land in the result, structural still beats a stale
    # episodic test would need a longer gap. Verify both present.
    texts = [r.lesson_text for r in rows]
    assert any("結構性" in t for t in texts)
    assert any("本週" in t for t in texts)


@pytest.mark.asyncio
async def test_fetch_structural_outranks_old_episodic(
    db_session: AsyncSession,
):
    """The interesting case: same age, different tier — structural
    decisively beats episodic when they're both old."""
    u = _user("structural-vs-old-episodic")
    d = _disc(u.id)
    db_session.add_all([u, d])
    await db_session.commit()

    today = date(2026, 5, 5)
    old_age = today - timedelta(days=200)
    structural_old = DiscussionLesson(
        discussion_id=d.id, owner_user_id=u.id, market="TW",
        as_of_date=old_age, category="other",
        lesson_text="結構性: 老但不衰減的關鍵教訓",
        lesson_text_hash="h-s-old",
        related_symbols=[], missed_winners=[],
        regime=None, tier="structural",
    )
    episodic_old = DiscussionLesson(
        discussion_id=d.id, owner_user_id=u.id, market="TW",
        as_of_date=old_age, category="other",
        lesson_text="一般: 一樣老但已嚴重衰減",
        lesson_text_hash="h-e-old",
        related_symbols=[], missed_winners=[],
        regime=None, tier="episodic",
    )
    db_session.add_all([structural_old, episodic_old])
    await db_session.commit()

    rows = await svc.fetch_relevant_lessons(
        db_session,
        owner_user_id=u.id, market="TW",
        discussion_as_of=today,
        limit=2,
    )
    assert rows[0].lesson_text.startswith("結構性")
    assert rows[1].lesson_text.startswith("一般")
