"""Unit tests for system_task_config_service.

Covers:
  - Default registry (news_sentiment + discussion_synthesizer present)
  - resolve() returns compiled defaults when no override row exists
  - resolve() returns override values once set
  - upsert validates provider whitelist + non-empty model
  - upsert rejects unknown task IDs
  - delete reverts back to compiled default
  - Re-routed sentiment scorer + discussion synthesizer call resolve() —
    verified by patching resolve() and asserting the chosen provider/model
    flow into stream_chat
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.discussion import DiscussionTurn
from models.user import User, UserRole
from services import discussion_service, news_sentiment_service
from services import system_task_config_service as svc


# ── fixtures ──────────────────────────────────────────────────────


@pytest.fixture
async def admin_user(db_session: AsyncSession) -> User:
    user = User(
        email=f"taskcfg-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x",
        role=UserRole.admin,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def _stream_text(text: str):
    async def _gen(*_a, **_kw) -> AsyncIterator[dict]:
        yield {"type": "delta", "text": text}
    return _gen


# ── registry ──────────────────────────────────────────────────────


def test_known_task_ids_includes_both_baseline_tasks():
    ids = svc.known_task_ids()
    assert "news_sentiment" in ids
    assert "discussion_synthesizer" in ids


def test_get_spec_unknown_raises():
    with pytest.raises(ValueError):
        svc.get_spec("not_a_task")


# ── list / resolve ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_tasks_returns_compiled_defaults_when_unset(
    db_session: AsyncSession,
):
    rows = await svc.list_tasks(db_session)
    by_id = {r.task_id: r for r in rows}
    assert "news_sentiment" in by_id
    cfg = by_id["news_sentiment"]
    assert cfg.is_overridden is False
    assert cfg.effective_provider == cfg.default_provider
    assert cfg.effective_model == cfg.default_model


@pytest.mark.asyncio
async def test_resolve_returns_default_when_unset(db_session: AsyncSession):
    spec = svc.get_spec("news_sentiment")
    provider, model = await svc.resolve(db_session, "news_sentiment")
    assert provider == spec.default_provider
    assert model == spec.default_model


@pytest.mark.asyncio
async def test_resolve_unknown_task_raises(db_session: AsyncSession):
    with pytest.raises(ValueError):
        await svc.resolve(db_session, "not_a_task")


# ── upsert / delete ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_then_resolve_returns_override(
    db_session: AsyncSession, admin_user: User,
):
    cfg = await svc.upsert_override(
        db_session, "news_sentiment", "openai", "gpt-4o-mini", admin_user.id,
    )
    assert cfg.is_overridden is True
    assert cfg.effective_provider == "openai"
    assert cfg.effective_model == "gpt-4o-mini"

    provider, model = await svc.resolve(db_session, "news_sentiment")
    assert provider == "openai"
    assert model == "gpt-4o-mini"


@pytest.mark.asyncio
async def test_upsert_replaces_existing_override(
    db_session: AsyncSession, admin_user: User,
):
    await svc.upsert_override(
        db_session, "news_sentiment", "openai", "gpt-4o-mini", admin_user.id,
    )
    await svc.upsert_override(
        db_session, "news_sentiment", "deepseek", "deepseek-chat", admin_user.id,
    )
    provider, model = await svc.resolve(db_session, "news_sentiment")
    assert provider == "deepseek"
    assert model == "deepseek-chat"


@pytest.mark.asyncio
async def test_upsert_rejects_invalid_provider(
    db_session: AsyncSession, admin_user: User,
):
    with pytest.raises(ValueError, match="invalid provider"):
        await svc.upsert_override(
            db_session, "news_sentiment", "made_up_provider", "x", admin_user.id,
        )


@pytest.mark.asyncio
async def test_upsert_rejects_blank_model(
    db_session: AsyncSession, admin_user: User,
):
    with pytest.raises(ValueError, match="model"):
        await svc.upsert_override(
            db_session, "news_sentiment", "openai", "   ", admin_user.id,
        )


@pytest.mark.asyncio
async def test_upsert_rejects_unknown_task(
    db_session: AsyncSession, admin_user: User,
):
    with pytest.raises(ValueError):
        await svc.upsert_override(
            db_session, "not_a_task", "openai", "gpt-4o", admin_user.id,
        )


@pytest.mark.asyncio
async def test_delete_reverts_to_default(
    db_session: AsyncSession, admin_user: User,
):
    spec = svc.get_spec("news_sentiment")
    await svc.upsert_override(
        db_session, "news_sentiment", "openai", "gpt-4o-mini", admin_user.id,
    )
    await svc.delete_override(db_session, "news_sentiment")
    provider, model = await svc.resolve(db_session, "news_sentiment")
    assert provider == spec.default_provider
    assert model == spec.default_model


@pytest.mark.asyncio
async def test_delete_unknown_task_is_noop(db_session: AsyncSession):
    # Delete on a task that has no row simply returns; no exception.
    await svc.delete_override(db_session, "news_sentiment")


# ── integration: callers route through resolve() ──────────────────


@pytest.mark.asyncio
async def test_score_pending_uses_admin_override(
    db_session: AsyncSession, admin_user: User,
):
    """The sentiment scorer should pick up the admin-selected provider/model
    instead of its compiled-in default when an override exists."""
    await svc.upsert_override(
        db_session, "news_sentiment", "deepseek", "deepseek-chat", admin_user.id,
    )

    captured: dict[str, str] = {}

    async def _capturing_stream(*_a, **kwargs) -> AsyncIterator[dict]:
        captured["provider"] = kwargs.get("provider", "")
        captured["model"] = kwargs.get("model", "")
        yield {"type": "delta", "text": "[]"}

    # Need at least one unscored row for the loop to enter the LLM branch.
    from datetime import UTC, datetime
    from services.ingest.repository import NewsArticleRow, insert_news_articles
    await insert_news_articles(db_session, [
        NewsArticleRow(
            market="TW",
            symbol="ROUTE_1",
            published_at=datetime.now(UTC),
            title="路由測試新聞",
            link="https://example.com/route_test",
            publisher="test",
            summary=None,
            payload=None,
            source="finmind",
        ),
    ])

    with patch(
        "services.news_sentiment_service.stream_chat",
        side_effect=_capturing_stream,
    ):
        await news_sentiment_service.score_pending(
            db=db_session, batch_size=10, max_batches=1,
        )

    assert captured["provider"] == "deepseek"
    assert captured["model"] == "deepseek-chat"


@pytest.mark.asyncio
async def test_synthesize_conclusion_uses_admin_override(
    db_session: AsyncSession, admin_user: User,
):
    """The discussion synthesizer should pick up the admin-selected
    provider/model for `discussion_synthesizer`."""
    await svc.upsert_override(
        db_session, "discussion_synthesizer", "openai", "gpt-4o-mini", admin_user.id,
    )

    discussion = await discussion_service.create_discussion(
        db_session,
        owner_id=admin_user.id,
        topic="topic",
        rules="rules",
        persona_ids=["buffett", "lynch"],
    )
    db_session.add(DiscussionTurn(
        discussion_id=discussion.id, round=1, turn_index=0,
        persona_id="buffett", stance="supplement", content="x",
    ))
    await db_session.commit()

    captured: dict[str, str] = {}

    async def _capturing_stream(*_a, **kwargs) -> AsyncIterator[dict]:
        captured["provider"] = kwargs.get("provider", "")
        captured["model"] = kwargs.get("model", "")
        yield {"type": "delta", "text": '{"recommended_symbols":[]}'}

    with patch(
        "services.discussion_service.stream_chat",
        side_effect=_capturing_stream,
    ), patch(
        "services.discussion_service.gather_market_context",
        new=AsyncMock(return_value={"market": "TW"}),
    ):
        await discussion_service.synthesize_conclusion(
            db_session, discussion, user_id=str(admin_user.id),
        )

    assert captured["provider"] == "openai"
    assert captured["model"] == "gpt-4o-mini"
