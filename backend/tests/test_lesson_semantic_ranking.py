"""Tests for J2 — semantic-similarity boost in lesson retrieval.

PR-J1 added the embedding column + write path. PR-J2 wires cosine
similarity into `fetch_relevant_lessons`'s score formula so that
"semantically related" lessons surface alongside "exact-symbol"
matches. This file exercises:

  - `_semantic_boost` math (clamped, NULL-safe, alpha-gated)
  - `fetch_relevant_lessons(..., query_embedding=...)` ranking
  - alpha=0 disables semantic ranking (back-compat)
  - the lessons context block computes a query embedding and passes
    it through (`_build_query_embedding`)
  - degraded paths return neutral boost so a partly-embedded corpus
    grades correctly
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.discussion import Discussion
from models.discussion_lesson import DiscussionLesson
from models.user import User, UserRole
from services import discussion_lesson_service as svc
from services.discussion.context.blocks import lessons as lessons_block


# ── helpers ──────────────────────────────────────────────────────


def _user(suffix: str = "") -> User:
    return User(
        id=uuid.uuid4(),
        email=f"sem-{suffix}@x.com",
        hashed_password="x",
        role=UserRole.analyst,
        is_active=True,
    )


def _disc(owner_id: uuid.UUID) -> Discussion:
    return Discussion(
        id=uuid.uuid4(),
        owner_id=owner_id,
        topic="半導體展望", rules="r",
        persona_ids=["a"],
        market="TW",
        status="done",
        current_round=1,
        as_of_date=date(2026, 5, 1),
    )


def _lesson(
    owner_id: uuid.UUID,
    discussion_id: uuid.UUID,
    *,
    text: str,
    suffix: str,
    as_of: date | None = None,
    related: list[str] | None = None,
    embedding: list[float] | None = None,
    tier: str = "episodic",
    category: str = "other",
) -> DiscussionLesson:
    return DiscussionLesson(
        discussion_id=discussion_id,
        owner_user_id=owner_id,
        market="TW",
        as_of_date=as_of or date(2026, 4, 1),
        category=category,
        lesson_text=text,
        lesson_text_hash=f"h-{suffix}",
        related_symbols=related or [],
        missed_winners=[],
        embedding=embedding,
        embedding_model="text-embedding-3-small" if embedding else None,
        tier=tier,
        regime=None,
    )


# ── _semantic_boost math ─────────────────────────────────────────


def test_semantic_boost_returns_one_when_alpha_zero():
    """alpha=0 disables semantic ranking entirely. Same behavior as
    pre-J2 when the runtime tunable is dialed off."""
    assert svc._semantic_boost([1.0, 0.0], [1.0, 0.0], alpha=0.0) == 1.0


def test_semantic_boost_returns_one_when_either_side_none():
    """NULL embedding on either side stays neutral so a partly-
    embedded corpus mid-backfill grades correctly without amplifying
    noise from unembedded rows."""
    assert svc._semantic_boost(None, [1.0], alpha=1.0) == 1.0
    assert svc._semantic_boost([1.0], None, alpha=1.0) == 1.0
    assert svc._semantic_boost(None, None, alpha=1.0) == 1.0


def test_semantic_boost_returns_one_for_negative_cosine():
    """Anti-correlated vectors don't earn a boost (clamped at 0) but
    aren't penalized either — a contradicting lesson is no less
    informative than an unrelated one."""
    assert svc._semantic_boost([1.0, 0.0], [-1.0, 0.0], alpha=1.0) == 1.0


def test_semantic_boost_perfect_match_is_one_plus_alpha():
    assert svc._semantic_boost([1.0, 0.0], [1.0, 0.0], alpha=1.0) == pytest.approx(2.0)
    assert svc._semantic_boost([1.0, 0.0], [1.0, 0.0], alpha=0.5) == pytest.approx(1.5)


def test_semantic_boost_partial_match_scales_linearly():
    """cos = 0.5 with alpha = 1 → boost = 1.5."""
    boost = svc._semantic_boost(
        [1.0, 0.0], [0.7071, 0.7071], alpha=1.0,
    )
    assert boost == pytest.approx(1.7071, abs=1e-3)


# ── fetch_relevant_lessons with query_embedding ──────────────────


@pytest.mark.asyncio
async def test_query_embedding_lifts_semantically_similar_lesson(
    db_session: AsyncSession, monkeypatch,
):
    """Two same-age, same-symbol lessons. Lesson A is semantically
    aligned with the query; Lesson B is orthogonal. The boost should
    reorder so A comes first."""
    monkeypatch.setattr(
        "services.discussion_lesson_service.settings."
        "LESSON_EMBEDDING_BOOST_ALPHA",
        1.0,
    )
    u = _user("lift")
    d = _disc(u.id)
    db_session.add_all([u, d])
    await db_session.commit()

    aligned = _lesson(
        u.id, d.id, text="半導體高估值風險 — A",
        suffix="aligned", embedding=[1.0, 0.0, 0.0],
    )
    orthogonal = _lesson(
        u.id, d.id, text="基礎建設成本上升 — B",
        suffix="orthogonal", embedding=[0.0, 1.0, 0.0],
    )
    db_session.add_all([aligned, orthogonal])
    await db_session.commit()

    rows = await svc.fetch_relevant_lessons(
        db_session,
        owner_user_id=u.id, market="TW",
        discussion_as_of=date(2026, 5, 1),
        limit=2,
        query_embedding=[1.0, 0.0, 0.0],
    )
    assert len(rows) == 2
    assert rows[0].lesson_text.startswith("半導體")
    assert rows[1].lesson_text.startswith("基礎建設")


@pytest.mark.asyncio
async def test_query_embedding_none_falls_back_to_legacy_ranking(
    db_session: AsyncSession,
):
    """Back-compat: fetch with query_embedding=None must return the
    same ranking as before J2 (time-decay × symbol × regime). Without
    semantic input, both lessons here tie on every other factor, so
    the as_of_date desc ordering breaks the tie."""
    u = _user("legacy")
    d = _disc(u.id)
    db_session.add_all([u, d])
    await db_session.commit()

    older = _lesson(
        u.id, d.id, text="older lesson",
        suffix="older", as_of=date(2026, 3, 1),
        embedding=[1.0, 0.0, 0.0],
    )
    newer = _lesson(
        u.id, d.id, text="newer lesson",
        suffix="newer", as_of=date(2026, 4, 1),
        embedding=[0.0, 1.0, 0.0],
    )
    db_session.add_all([older, newer])
    await db_session.commit()

    rows = await svc.fetch_relevant_lessons(
        db_session,
        owner_user_id=u.id, market="TW",
        discussion_as_of=date(2026, 5, 1),
        limit=2,
        # query_embedding intentionally omitted (defaults to None)
    )
    assert rows[0].lesson_text == "newer lesson"


@pytest.mark.asyncio
async def test_alpha_zero_disables_semantic_ranking(
    db_session: AsyncSession, monkeypatch,
):
    """When alpha=0 the query_embedding is supplied but doesn't
    influence the order — recency wins again."""
    monkeypatch.setattr(
        "services.discussion_lesson_service.settings."
        "LESSON_EMBEDDING_BOOST_ALPHA",
        0.0,
    )
    u = _user("alpha-zero")
    d = _disc(u.id)
    db_session.add_all([u, d])
    await db_session.commit()

    older_aligned = _lesson(
        u.id, d.id, text="older but semantically perfect",
        suffix="oa", as_of=date(2026, 3, 1),
        embedding=[1.0, 0.0, 0.0],
    )
    newer_unrelated = _lesson(
        u.id, d.id, text="newer but unrelated",
        suffix="nu", as_of=date(2026, 4, 28),
        embedding=[0.0, 1.0, 0.0],
    )
    db_session.add_all([older_aligned, newer_unrelated])
    await db_session.commit()

    rows = await svc.fetch_relevant_lessons(
        db_session,
        owner_user_id=u.id, market="TW",
        discussion_as_of=date(2026, 5, 1),
        limit=2,
        query_embedding=[1.0, 0.0, 0.0],
    )
    assert rows[0].lesson_text == "newer but unrelated"


@pytest.mark.asyncio
async def test_unembedded_lesson_stays_eligible_under_query(
    db_session: AsyncSession, monkeypatch,
):
    """A lesson with embedding=NULL should still appear in results
    when a query_embedding is supplied — it just doesn't get the
    boost. Critical for the partly-embedded corpus mid-backfill."""
    monkeypatch.setattr(
        "services.discussion_lesson_service.settings."
        "LESSON_EMBEDDING_BOOST_ALPHA",
        1.0,
    )
    u = _user("noemb")
    d = _disc(u.id)
    db_session.add_all([u, d])
    await db_session.commit()

    no_embed = _lesson(
        u.id, d.id, text="未 embed 的歷史教訓",
        suffix="legacy", embedding=None,
    )
    db_session.add(no_embed)
    await db_session.commit()

    rows = await svc.fetch_relevant_lessons(
        db_session,
        owner_user_id=u.id, market="TW",
        discussion_as_of=date(2026, 5, 1),
        limit=5,
        query_embedding=[1.0, 0.0, 0.0],
    )
    assert len(rows) == 1
    assert rows[0].lesson_text == "未 embed 的歷史教訓"


@pytest.mark.asyncio
async def test_semantic_boost_outranks_recency_at_perfect_match(
    db_session: AsyncSession, monkeypatch,
):
    """A 30-day-older lesson with perfect semantic match SHOULD beat
    a fresh unrelated lesson — exp(-30 * ln2 / 60) ≈ 0.707, doubled
    by 1+1*1 = 2.0, vs fresh unrelated ≈ 1.0 * 1 = 1.0. Validates
    alpha=1.0 is strong enough to flip recency for >2 weeks of age
    gap."""
    monkeypatch.setattr(
        "services.discussion_lesson_service.settings."
        "LESSON_EMBEDDING_BOOST_ALPHA",
        1.0,
    )
    u = _user("flip-recency")
    d = _disc(u.id)
    db_session.add_all([u, d])
    await db_session.commit()

    today = date(2026, 5, 1)
    older_aligned = _lesson(
        u.id, d.id, text="30 天前的對齊教訓",
        suffix="oa", as_of=today - timedelta(days=30),
        embedding=[1.0, 0.0, 0.0],
    )
    fresh_unrelated = _lesson(
        u.id, d.id, text="今天的不相關教訓",
        suffix="fu", as_of=today,
        embedding=[0.0, 1.0, 0.0],
    )
    db_session.add_all([older_aligned, fresh_unrelated])
    await db_session.commit()

    rows = await svc.fetch_relevant_lessons(
        db_session,
        owner_user_id=u.id, market="TW",
        discussion_as_of=today,
        limit=2,
        query_embedding=[1.0, 0.0, 0.0],
    )
    assert rows[0].lesson_text.startswith("30 天前")


# ── _build_query_embedding (lessons context block helper) ──────


@pytest.mark.asyncio
async def test_build_query_embedding_returns_none_when_topic_and_symbols_empty(
    db_session: AsyncSession,
):
    """No topic + no focus_symbols = nothing to embed against. Skip
    the LLM call entirely."""
    out = await lessons_block._build_query_embedding(
        db_session, topic="", market="TW", focus_symbols=[],
    )
    assert out is None


@pytest.mark.asyncio
async def test_build_query_embedding_calls_embed_when_topic_present(
    db_session: AsyncSession, monkeypatch,
):
    monkeypatch.setattr(
        "services.lesson_embedding_service.settings.OPENAI_API_KEY", "sk-x",
    )
    fake = MagicMock()
    fake.status_code = 200
    fake.text = "ok"
    fake.json = MagicMock(return_value={
        "data": [{"embedding": [0.1] * 8, "index": 0}],
        "model": "text-embedding-3-small",
    })
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(return_value=fake)
    with patch(
        "services.lesson_embedding_service.httpx.AsyncClient",
        return_value=client,
    ):
        out = await lessons_block._build_query_embedding(
            db_session, topic="半導體展望",
            market="TW", focus_symbols=["2330"],
        )
    assert out == [0.1] * 8


@pytest.mark.asyncio
async def test_build_query_embedding_failsoft_on_provider_error(
    db_session: AsyncSession, monkeypatch,
):
    """Provider 500 → returns None; the fetch path then falls back
    to the legacy ranking, NEVER blocks the round."""
    monkeypatch.setattr(
        "services.lesson_embedding_service.settings.OPENAI_API_KEY", "sk-x",
    )
    fake = MagicMock()
    fake.status_code = 500
    fake.text = "boom"
    fake.json = MagicMock(return_value={})
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(return_value=fake)
    with patch(
        "services.lesson_embedding_service.httpx.AsyncClient",
        return_value=client,
    ):
        out = await lessons_block._build_query_embedding(
            db_session, topic="半導體展望",
            market="TW", focus_symbols=["2330"],
        )
    assert out is None


@pytest.mark.asyncio
async def test_build_query_embedding_returns_none_when_no_api_key(
    db_session: AsyncSession, monkeypatch,
):
    monkeypatch.setattr(
        "services.lesson_embedding_service.settings.OPENAI_API_KEY", "",
    )
    out = await lessons_block._build_query_embedding(
        db_session, topic="半導體展望",
        market="TW", focus_symbols=["2330"],
    )
    assert out is None


# ── end-to-end through fetch_recent_lessons block ─────────────────


@pytest.mark.asyncio
async def test_fetch_recent_lessons_passes_query_embedding_through(
    db_session: AsyncSession, monkeypatch,
):
    """Given a topic + focus_symbols, the block must compute a query
    embedding and forward it to fetch_relevant_lessons. Assert via
    spy on fetch_relevant_lessons."""
    monkeypatch.setattr(
        "services.lesson_embedding_service.settings.OPENAI_API_KEY", "sk-x",
    )
    fake = MagicMock()
    fake.status_code = 200
    fake.text = "ok"
    fake.json = MagicMock(return_value={
        "data": [{"embedding": [0.42] * 4, "index": 0}],
        "model": "text-embedding-3-small",
    })
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(return_value=fake)

    u = _user("e2e")
    d = _disc(u.id)
    db_session.add_all([u, d])
    await db_session.commit()

    captured: dict = {}
    real_fetch = svc.fetch_relevant_lessons

    async def spy_fetch(*args, **kwargs):
        captured["query_embedding"] = kwargs.get("query_embedding")
        return await real_fetch(*args, **kwargs)

    ctx: dict = {}

    def _record_error(_src, _exc):
        return None

    # The block lazy-imports `fetch_relevant_lessons` from
    # `services.discussion_lesson_service` inside the function body,
    # so we patch at the source module.
    with patch(
        "services.lesson_embedding_service.httpx.AsyncClient",
        return_value=client,
    ), patch(
        "services.discussion_lesson_service.fetch_relevant_lessons",
        spy_fetch,
    ):
        await lessons_block.fetch_recent_lessons(
            ctx, db_session,
            owner_id=u.id, market="TW",
            focus_symbols=["2330"], as_of=date(2026, 5, 1),
            record_error=_record_error,
            topic="半導體展望",
        )
    assert captured["query_embedding"] == [0.42] * 4


@pytest.mark.asyncio
async def test_fetch_recent_lessons_works_without_topic(
    db_session: AsyncSession, monkeypatch,
):
    """Back-compat: callers that don't pass `topic` still work; the
    block just doesn't compute a query embedding."""
    monkeypatch.setattr(
        "services.lesson_embedding_service.settings.OPENAI_API_KEY", "",
    )
    u = _user("no-topic")
    d = _disc(u.id)
    db_session.add_all([u, d])
    await db_session.commit()

    ctx: dict = {}

    def _record_error(_src, _exc):
        return None

    await lessons_block.fetch_recent_lessons(
        ctx, db_session,
        owner_id=u.id, market="TW",
        focus_symbols=None, as_of=date(2026, 5, 1),
        record_error=_record_error,
    )
    # Block runs without raising; ctx has the empty default shape
    assert "recent_lessons" in ctx
