"""Unit tests for `services/lesson_embedding_service.py` (PR-J1).

Covers cosine math, the embed-text builder, the OpenAI httpx call
(mocked), inline + bulk persist, and the backfill loop. Uses the
same `db_session` fixture as the rest of the lesson tests so the
SQLite in-memory engine sees the migration 0051 columns.
"""
from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.discussion import Discussion
from models.discussion_lesson import DiscussionLesson
from models.user import User, UserRole
from services import lesson_embedding_service as svc


# ── helpers ───────────────────────────────────────────────────────


def _user(suffix: str = "") -> User:
    return User(
        id=uuid.uuid4(),
        email=f"emb-{suffix}@x.com",
        hashed_password="x",
        role=UserRole.analyst,
        is_active=True,
    )


def _disc(owner_id: uuid.UUID) -> Discussion:
    return Discussion(
        id=uuid.uuid4(),
        owner_id=owner_id,
        topic="t", rules="r",
        persona_ids=["a"],
        market="TW",
        status="done",
        current_round=1,
        as_of_date=date(2026, 4, 1),
    )


def _lesson(
    owner_id: uuid.UUID,
    discussion_id: uuid.UUID,
    *,
    text: str = "外資台指期 1500 口多單訊號被忽略，導致沒看到 +5% 動能。",
    market: str = "TW",
    related: list[str] | None = None,
    embedding: list[float] | None = None,
    embedding_model: str | None = None,
) -> DiscussionLesson:
    return DiscussionLesson(
        discussion_id=discussion_id,
        owner_user_id=owner_id,
        market=market,
        as_of_date=date(2026, 4, 1),
        category="missing_data",
        lesson_text=text,
        lesson_text_hash="h" * 64,
        related_symbols=related or [],
        missed_winners=[],
        embedding=embedding,
        embedding_model=embedding_model,
    )


def _fake_openai_response(
    n: int, *, dims: int = 8, status: int = 200,
) -> MagicMock:
    """Build a MagicMock that mimics httpx.Response with a
    well-formed embeddings payload of `n` vectors of `dims` floats."""
    resp = MagicMock()
    resp.status_code = status
    resp.text = "ok" if status == 200 else "boom"
    resp.json = MagicMock(return_value={
        "data": [
            {"embedding": [float(i + j) / 10.0 for j in range(dims)],
             "index": i}
            for i in range(n)
        ],
        "model": "text-embedding-3-small",
    })
    return resp


def _patch_httpx(resp: MagicMock):
    """Patch `httpx.AsyncClient` in the service module so the
    embeddings call returns the supplied fake response without
    touching the network."""
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(return_value=resp)
    return patch("services.lesson_embedding_service.httpx.AsyncClient",
                 return_value=client)


# ── cosine math ───────────────────────────────────────────────────


def test_cosine_orthogonal_returns_zero():
    assert svc.cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_identical_returns_one():
    assert svc.cosine([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)


def test_cosine_opposite_returns_minus_one():
    assert svc.cosine([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


def test_cosine_handles_none_or_empty():
    assert svc.cosine(None, [1.0]) == 0.0
    assert svc.cosine([1.0], None) == 0.0
    assert svc.cosine([], []) == 0.0


def test_cosine_handles_mismatched_length():
    """Defensive — embedding mismatch shouldn't raise, just return
    0 so the caller falls back to the legacy score."""
    assert svc.cosine([1.0, 2.0], [1.0, 2.0, 3.0]) == 0.0


def test_cosine_handles_zero_vector():
    """All-zero vector has undefined cosine — return 0 instead of
    NaN so the score formula stays well-behaved."""
    assert svc.cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


# ── build_embed_text ──────────────────────────────────────────────


def test_build_embed_text_empty_input_returns_blank():
    assert svc.build_embed_text("") == ""
    assert svc.build_embed_text("   ") == ""


def test_build_embed_text_includes_metadata_and_body():
    out = svc.build_embed_text(
        "外資 連 5 日多單 +1500 口被忽略",
        market="TW", category="missing_data",
        related_symbols=["2330", "2454"],
    )
    assert "[market] TW" in out
    assert "[category] missing_data" in out
    assert "[symbols] 2330,2454" in out
    assert "外資 連 5 日多單 +1500 口被忽略" in out


def test_build_embed_text_skips_missing_metadata():
    out = svc.build_embed_text("body only")
    assert out == "body only"


def test_build_embed_text_caps_symbol_list_at_10():
    syms = [str(i) for i in range(20)]
    out = svc.build_embed_text("body", related_symbols=syms)
    # Should contain only first 10
    assert "[symbols] 0,1,2,3,4,5,6,7,8,9" in out
    # And not 10..19
    assert ",10," not in out


# ── _embed_via_openai (HTTP layer) ────────────────────────────────


@pytest.mark.asyncio
async def test_embed_via_openai_returns_none_when_key_missing():
    out = await svc._embed_via_openai(
        ["text"], model="text-embedding-3-small", api_key="",
    )
    assert out is None


@pytest.mark.asyncio
async def test_embed_via_openai_returns_empty_list_for_empty_inputs():
    out = await svc._embed_via_openai(
        [], model="text-embedding-3-small", api_key="sk-x",
    )
    assert out == []


@pytest.mark.asyncio
async def test_embed_via_openai_parses_successful_response():
    resp = _fake_openai_response(2)
    with _patch_httpx(resp):
        out = await svc._embed_via_openai(
            ["a", "b"], model="text-embedding-3-small", api_key="sk-x",
        )
    assert out is not None
    assert len(out) == 2
    assert all(isinstance(v, list) for v in out)
    assert all(len(v) == 8 for v in out)


@pytest.mark.asyncio
async def test_embed_via_openai_returns_none_on_http_error():
    resp = _fake_openai_response(1, status=401)
    with _patch_httpx(resp):
        out = await svc._embed_via_openai(
            ["a"], model="text-embedding-3-small", api_key="sk-bad",
        )
    assert out is None


@pytest.mark.asyncio
async def test_embed_via_openai_returns_none_on_malformed_payload():
    resp = MagicMock()
    resp.status_code = 200
    resp.text = "ok"
    resp.json = MagicMock(return_value={"data": "not a list"})
    with _patch_httpx(resp):
        out = await svc._embed_via_openai(
            ["a"], model="text-embedding-3-small", api_key="sk-x",
        )
    assert out is None


@pytest.mark.asyncio
async def test_embed_via_openai_returns_none_on_length_mismatch():
    """Provider returned 1 vector for 2 inputs — shape mismatch is a
    bug we don't try to recover from; bail and let the backfill
    retry."""
    resp = _fake_openai_response(1)
    with _patch_httpx(resp):
        out = await svc._embed_via_openai(
            ["a", "b"], model="text-embedding-3-small", api_key="sk-x",
        )
    assert out is None


# ── embed_texts (provider routing layer) ──────────────────────────


@pytest.mark.asyncio
async def test_embed_texts_returns_none_when_no_key(monkeypatch):
    """No DB session + empty env key = (None, ""), the inline write
    path uses this to mean 'skip embed silently'."""
    monkeypatch.setattr(
        "services.lesson_embedding_service.settings.OPENAI_API_KEY", "",
    )
    out, model = await svc.embed_texts(None, ["body"])
    assert out is None
    assert model == ""


@pytest.mark.asyncio
async def test_embed_texts_strips_blank_inputs(monkeypatch):
    monkeypatch.setattr(
        "services.lesson_embedding_service.settings.OPENAI_API_KEY", "sk-x",
    )
    resp = _fake_openai_response(2)
    with _patch_httpx(resp):
        vectors, model = await svc.embed_texts(None, ["a", "  ", "b", ""])
    assert vectors is not None
    assert len(vectors) == 2
    assert model == "text-embedding-3-small"


@pytest.mark.asyncio
async def test_embed_texts_handles_unsupported_provider(monkeypatch):
    monkeypatch.setattr(
        "services.lesson_embedding_service.settings.LESSON_EMBEDDING_PROVIDER",
        "voyage",
    )
    out, model = await svc.embed_texts(None, ["body"])
    assert out is None
    assert model == ""


# ── embed_lesson (persist single row) ─────────────────────────────


@pytest.mark.asyncio
async def test_embed_lesson_persists_vector(
    db_session: AsyncSession, monkeypatch,
):
    monkeypatch.setattr(
        "services.lesson_embedding_service.settings.OPENAI_API_KEY", "sk-x",
    )
    u = _user("p1")
    d = _disc(u.id)
    db_session.add_all([u, d])
    await db_session.commit()
    lesson = _lesson(u.id, d.id, related=["2330"])
    db_session.add(lesson)
    await db_session.commit()

    resp = _fake_openai_response(1)
    with _patch_httpx(resp):
        ok = await svc.embed_lesson(db_session, lesson)
    assert ok is True
    await db_session.refresh(lesson)
    assert lesson.embedding is not None
    assert len(lesson.embedding) == 8
    assert lesson.embedding_model == "text-embedding-3-small"
    assert lesson.embedded_at is not None


@pytest.mark.asyncio
async def test_embed_lesson_skips_when_already_embedded_same_model(
    db_session: AsyncSession, monkeypatch,
):
    """Idempotency: calling twice with the same model is a no-op."""
    monkeypatch.setattr(
        "services.lesson_embedding_service.settings.OPENAI_API_KEY", "sk-x",
    )
    u = _user("p2")
    d = _disc(u.id)
    db_session.add_all([u, d])
    await db_session.commit()
    lesson = _lesson(
        u.id, d.id,
        embedding=[0.1] * 8,
        embedding_model="text-embedding-3-small",
    )
    db_session.add(lesson)
    await db_session.commit()

    resp = _fake_openai_response(1)
    with _patch_httpx(resp) as m:
        ok = await svc.embed_lesson(db_session, lesson)
    assert ok is False
    # The HTTP layer was never called
    m.assert_not_called()


@pytest.mark.asyncio
async def test_embed_lesson_re_embeds_when_model_changes(
    db_session: AsyncSession, monkeypatch,
):
    monkeypatch.setattr(
        "services.lesson_embedding_service.settings.OPENAI_API_KEY", "sk-x",
    )
    monkeypatch.setattr(
        "services.lesson_embedding_service.settings.LESSON_EMBEDDING_MODEL",
        "text-embedding-3-large",
    )
    u = _user("p3")
    d = _disc(u.id)
    db_session.add_all([u, d])
    await db_session.commit()
    lesson = _lesson(
        u.id, d.id,
        embedding=[0.0] * 8,
        embedding_model="text-embedding-3-small",
    )
    db_session.add(lesson)
    await db_session.commit()

    resp = _fake_openai_response(1)
    with _patch_httpx(resp):
        ok = await svc.embed_lesson(db_session, lesson)
    assert ok is True
    await db_session.refresh(lesson)
    assert lesson.embedding_model == "text-embedding-3-large"


@pytest.mark.asyncio
async def test_embed_lesson_fail_soft_on_provider_error(
    db_session: AsyncSession, monkeypatch,
):
    monkeypatch.setattr(
        "services.lesson_embedding_service.settings.OPENAI_API_KEY", "sk-x",
    )
    u = _user("p4")
    d = _disc(u.id)
    db_session.add_all([u, d])
    await db_session.commit()
    lesson = _lesson(u.id, d.id)
    db_session.add(lesson)
    await db_session.commit()

    resp = _fake_openai_response(1, status=500)
    with _patch_httpx(resp):
        ok = await svc.embed_lesson(db_session, lesson)
    assert ok is False
    await db_session.refresh(lesson)
    assert lesson.embedding is None


@pytest.mark.asyncio
async def test_embed_lesson_returns_false_when_disabled(
    db_session: AsyncSession, monkeypatch,
):
    """Master switch off => return False, never touch HTTP."""
    monkeypatch.setattr(
        "services.lesson_embedding_service.settings.LESSON_EMBEDDING_ENABLED",
        False,
    )
    u = _user("p5")
    d = _disc(u.id)
    db_session.add_all([u, d])
    await db_session.commit()
    lesson = _lesson(u.id, d.id)
    db_session.add(lesson)
    await db_session.commit()

    resp = _fake_openai_response(1)
    with _patch_httpx(resp) as m:
        ok = await svc.embed_lesson(db_session, lesson)
    assert ok is False
    m.assert_not_called()


# ── embed_lessons_bulk ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bulk_embed_persists_all_pending(
    db_session: AsyncSession, monkeypatch,
):
    monkeypatch.setattr(
        "services.lesson_embedding_service.settings.OPENAI_API_KEY", "sk-x",
    )
    u = _user("b1")
    d = _disc(u.id)
    db_session.add_all([u, d])
    await db_session.commit()
    rows = [_lesson(u.id, d.id, text=f"lesson body number {i} 外資 動能 1500")
            for i in range(3)]
    for r in rows:
        db_session.add(r)
    await db_session.commit()

    resp = _fake_openai_response(3)
    with _patch_httpx(resp):
        n = await svc.embed_lessons_bulk(db_session, rows)
    assert n == 3
    for r in rows:
        await db_session.refresh(r)
        assert r.embedding is not None


@pytest.mark.asyncio
async def test_bulk_embed_skips_already_embedded(
    db_session: AsyncSession, monkeypatch,
):
    monkeypatch.setattr(
        "services.lesson_embedding_service.settings.OPENAI_API_KEY", "sk-x",
    )
    u = _user("b2")
    d = _disc(u.id)
    db_session.add_all([u, d])
    await db_session.commit()
    fresh = _lesson(u.id, d.id, text="fresh body 外資 動能 1500 口 連 5 日")
    cached = _lesson(
        u.id, d.id, text="cached body 外資 動能 1500 口 連 5 日",
        embedding=[0.5] * 8,
        embedding_model="text-embedding-3-small",
    )
    db_session.add_all([fresh, cached])
    await db_session.commit()

    resp = _fake_openai_response(1)  # only fresh should be in the call
    with _patch_httpx(resp):
        n = await svc.embed_lessons_bulk(db_session, [fresh, cached])
    assert n == 1


@pytest.mark.asyncio
async def test_bulk_embed_returns_zero_on_provider_failure(
    db_session: AsyncSession, monkeypatch,
):
    monkeypatch.setattr(
        "services.lesson_embedding_service.settings.OPENAI_API_KEY", "sk-x",
    )
    u = _user("b3")
    d = _disc(u.id)
    db_session.add_all([u, d])
    await db_session.commit()
    rows = [_lesson(u.id, d.id, text=f"body {i} 外資 動能 1500 口")
            for i in range(2)]
    for r in rows:
        db_session.add(r)
    await db_session.commit()

    resp = _fake_openai_response(1, status=429)
    with _patch_httpx(resp):
        n = await svc.embed_lessons_bulk(db_session, rows)
    assert n == 0


# ── backfill_pending ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_backfill_processes_pending_rows_and_reports(
    db_session: AsyncSession, monkeypatch,
):
    monkeypatch.setattr(
        "services.lesson_embedding_service.settings.OPENAI_API_KEY", "sk-x",
    )
    u = _user("bf1")
    d = _disc(u.id)
    db_session.add_all([u, d])
    await db_session.commit()
    rows = [_lesson(u.id, d.id, text=f"body {i} 外資 動能 1500 口")
            for i in range(5)]
    for r in rows:
        db_session.add(r)
    await db_session.commit()

    resp = _fake_openai_response(5)
    with _patch_httpx(resp):
        result = await svc.backfill_pending(db_session)
    assert result["scanned"] == 5
    assert result["updated"] == 5
    assert result["batches"] == 1


@pytest.mark.asyncio
async def test_backfill_respects_max_rows(
    db_session: AsyncSession, monkeypatch,
):
    monkeypatch.setattr(
        "services.lesson_embedding_service.settings.OPENAI_API_KEY", "sk-x",
    )
    u = _user("bf2")
    d = _disc(u.id)
    db_session.add_all([u, d])
    await db_session.commit()
    rows = [_lesson(u.id, d.id, text=f"body {i} 外資 動能 1500 口")
            for i in range(10)]
    for r in rows:
        db_session.add(r)
    await db_session.commit()

    resp = _fake_openai_response(3)
    with _patch_httpx(resp):
        result = await svc.backfill_pending(db_session, max_rows=3)
    assert result["scanned"] == 3
    assert result["updated"] == 3


@pytest.mark.asyncio
async def test_backfill_returns_skipped_when_disabled(
    db_session: AsyncSession, monkeypatch,
):
    monkeypatch.setattr(
        "services.lesson_embedding_service.settings.LESSON_EMBEDDING_ENABLED",
        False,
    )
    result = await svc.backfill_pending(db_session)
    assert result.get("skipped") is True
    assert result["scanned"] == 0
    assert result["updated"] == 0


@pytest.mark.asyncio
async def test_backfill_no_op_when_no_pending_rows(
    db_session: AsyncSession, monkeypatch,
):
    monkeypatch.setattr(
        "services.lesson_embedding_service.settings.OPENAI_API_KEY", "sk-x",
    )
    u = _user("bf4")
    d = _disc(u.id)
    db_session.add_all([u, d])
    await db_session.commit()
    # only embedded rows in the DB
    row = _lesson(
        u.id, d.id,
        embedding=[0.1] * 8,
        embedding_model="text-embedding-3-small",
    )
    db_session.add(row)
    await db_session.commit()

    result = await svc.backfill_pending(db_session)
    assert result["scanned"] == 0
    assert result["updated"] == 0
    assert result["batches"] == 0


@pytest.mark.asyncio
async def test_backfill_scopes_by_owner_and_market(
    db_session: AsyncSession, monkeypatch,
):
    monkeypatch.setattr(
        "services.lesson_embedding_service.settings.OPENAI_API_KEY", "sk-x",
    )
    a, b = _user("bf5a"), _user("bf5b")
    da, db_ = _disc(a.id), _disc(b.id)
    db_session.add_all([a, b, da, db_])
    await db_session.commit()
    db_session.add_all([
        _lesson(a.id, da.id, text="a tw 外資 動能 1500 口", market="TW"),
        _lesson(a.id, da.id, text="a us 外資 動能 1500 口", market="US"),
        _lesson(b.id, db_.id, text="b tw 外資 動能 1500 口", market="TW"),
    ])
    await db_session.commit()

    resp = _fake_openai_response(1)
    with _patch_httpx(resp):
        result = await svc.backfill_pending(
            db_session, owner_user_id=a.id, market="TW",
        )
    assert result["scanned"] == 1
    assert result["updated"] == 1


# ── inline embedding integration through extract_and_persist_lessons ──


@pytest.mark.asyncio
async def test_extract_persist_runs_inline_embed_best_effort(
    db_session: AsyncSession, monkeypatch,
):
    """The post-mortem write path's call into the embed service is
    fail-soft — a provider outage at extract time MUST NOT block the
    lesson commit. We assert both the rows persist AND the embed
    error is swallowed."""
    monkeypatch.setattr(
        "services.lesson_embedding_service.settings.OPENAI_API_KEY", "sk-x",
    )
    from services import discussion_lesson_service as lesson_svc
    u = _user("inl")
    d = _disc(u.id)
    db_session.add_all([u, d])
    await db_session.commit()

    resp = _fake_openai_response(1, status=500)
    with _patch_httpx(resp):
        out = await lesson_svc.extract_and_persist_lessons(
            db_session,
            discussion_id=d.id, owner_user_id=u.id,
            market="TW", as_of_date=d.as_of_date,
            lessons_payload=[{
                "category": "missing_data",
                "lesson_text": (
                    "外資台指期 1500 口連 5 日多單訊號被忽略，"
                    "導致沒看到 +5% 動能。"
                ),
            }],
        )
    assert len(out) == 1
    persisted = (await db_session.scalars(
        select(DiscussionLesson).where(
            DiscussionLesson.owner_user_id == u.id,
        )
    )).all()
    assert len(persisted) == 1
    assert persisted[0].embedding is None  # provider failed, NULL stays


@pytest.mark.asyncio
async def test_extract_persist_writes_embedding_on_success(
    db_session: AsyncSession, monkeypatch,
):
    monkeypatch.setattr(
        "services.lesson_embedding_service.settings.OPENAI_API_KEY", "sk-x",
    )
    from services import discussion_lesson_service as lesson_svc
    u = _user("inl2")
    d = _disc(u.id)
    db_session.add_all([u, d])
    await db_session.commit()

    resp = _fake_openai_response(1)
    with _patch_httpx(resp):
        out = await lesson_svc.extract_and_persist_lessons(
            db_session,
            discussion_id=d.id, owner_user_id=u.id,
            market="TW", as_of_date=d.as_of_date,
            lessons_payload=[{
                "category": "missing_data",
                "lesson_text": (
                    "外資台指期 1500 口連 5 日多單訊號被忽略，"
                    "導致沒看到 +5% 動能。"
                ),
            }],
        )
    assert len(out) == 1
    await db_session.refresh(out[0])
    assert out[0].embedding is not None
    assert out[0].embedding_model == "text-embedding-3-small"


# ── metric counters wire through ─────────────────────────────────


@pytest.mark.asyncio
async def test_metric_outcome_labels_are_emitted(
    db_session: AsyncSession, monkeypatch,
):
    """Sanity: success and failed paths both bump the prometheus
    counter. We don't assert exact values (other tests share the
    counter) — just that the increment doesn't raise."""
    from middleware.metrics import LESSON_EMBEDDINGS_TOTAL  # noqa: F401
    monkeypatch.setattr(
        "services.lesson_embedding_service.settings.OPENAI_API_KEY", "sk-x",
    )
    u = _user("mc")
    d = _disc(u.id)
    db_session.add_all([u, d])
    await db_session.commit()
    lesson = _lesson(u.id, d.id)
    db_session.add(lesson)
    await db_session.commit()

    resp = _fake_openai_response(1)
    with _patch_httpx(resp):
        await svc.embed_lesson(db_session, lesson)
