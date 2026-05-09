"""PR-2: tests for `extract_winning_thesis_lessons` — single-LLM-call
lesson extraction for backtest discussions that hit their win
threshold. Closes the production-win gap in the learning loop where
miss/marginal post-mortem rounds wrote lessons but pure wins didn't.
"""
from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.discussion import Discussion
from models.user import User, UserRole
from services import discussion_service


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    user = User(
        email=f"winls-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x",
        role=UserRole.analyst,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def _backtest_discussion(owner_id: uuid.UUID) -> Discussion:
    return Discussion(
        id=uuid4(), owner_id=owner_id,
        topic="2330 backtest", rules="",
        market="TW", persona_ids=["market_analyst"],
        status="concluded", current_round=1,
        as_of_date=date(2026, 4, 1),
        conclusion={
            "recommended_symbols": ["2330"],
            "recommendations": [{"symbol": "2330", "confidence": 0.7}],
            "reasoning": "ok", "risks": [],
            "time_horizon": "short_term", "consensus_score": 0.7,
        },
    )


def _stream_yielding(text: str) -> AsyncIterator[dict[str, Any]]:
    """Mock stream_chat output: emit the raw JSON as a single delta
    then a usage event then close. Mirrors the shape `stream_chat`
    yields so the helper's loop accumulates correctly."""
    async def gen() -> AsyncIterator[dict[str, Any]]:
        yield {"type": "delta", "text": text}
        yield {"type": "usage", "prompt_tokens": 100, "completion_tokens": 50}
    return gen()


# ── early-return guards ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_skipped_when_not_backtest(db_session: AsyncSession, owner: User):
    """Live discussions (as_of_date=None) shouldn't extract — lessons
    are only meaningful with a verifiable backtest anchor."""
    disc = _backtest_discussion(owner.id)
    disc.as_of_date = None
    n = await discussion_service.extract_winning_thesis_lessons(
        db_session, disc, win_prompt_text="prompt", user_id=str(owner.id),
    )
    assert n == 0


@pytest.mark.asyncio
async def test_skipped_when_conclusion_missing(db_session: AsyncSession, owner: User):
    disc = _backtest_discussion(owner.id)
    disc.conclusion = None
    n = await discussion_service.extract_winning_thesis_lessons(
        db_session, disc, win_prompt_text="prompt", user_id=str(owner.id),
    )
    assert n == 0


@pytest.mark.asyncio
async def test_skipped_when_conclusion_parse_error(
    db_session: AsyncSession, owner: User,
):
    disc = _backtest_discussion(owner.id)
    disc.conclusion = {"_parse_error": True, "recommendations": []}
    n = await discussion_service.extract_winning_thesis_lessons(
        db_session, disc, win_prompt_text="prompt", user_id=str(owner.id),
    )
    assert n == 0


@pytest.mark.asyncio
async def test_skipped_when_prompt_empty(db_session: AsyncSession, owner: User):
    disc = _backtest_discussion(owner.id)
    n = await discussion_service.extract_winning_thesis_lessons(
        db_session, disc, win_prompt_text="", user_id=str(owner.id),
    )
    assert n == 0


# ── happy path ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_writes_lessons_from_llm_response(
    db_session: AsyncSession, owner: User,
):
    """LLM emits a clean lessons array → persisted via
    extract_and_persist_lessons with verdict='win'."""
    disc = _backtest_discussion(owner.id)
    db_session.add(disc)
    await db_session.commit()
    await db_session.refresh(disc)

    fake_response = json.dumps({
        "lessons": [
            {
                "category": "correct_signal_combo",
                "lesson_text": (
                    "外資台指期由空轉多 + 半導體單日五日均量爆量 + 殖利率倒掛緩解"
                ),
                "related_symbols": ["2330"],
                "missed_winners": [],
            },
            {
                "category": "successful_thesis",
                "lesson_text": (
                    "風險偏好回升時半導體類股先行於整體市場反彈，可作領先指標"
                ),
                "related_symbols": ["2330", "2454"],
                "missed_winners": [],
            },
        ],
    })
    with patch("services.discussion.lessons.stream_chat",
        return_value=_stream_yielding(fake_response),
    ), patch(
        "services.system_task_config_service.resolve",
        AsyncMock(return_value=("openai", "gpt-4o-mini")),
    ):
        n = await discussion_service.extract_winning_thesis_lessons(
            db_session, disc,
            win_prompt_text="【事後檢討 — 命中經驗】 ... reflection prompt ...",
            user_id=str(owner.id),
        )
    assert n == 2

    # Verify the lessons were actually persisted
    from models.discussion_lesson import DiscussionLesson
    from sqlalchemy import select
    rows = (await db_session.scalars(
        select(DiscussionLesson).where(
            DiscussionLesson.discussion_id == disc.id,
        )
    )).all()
    assert len(rows) == 2
    categories = {r.category for r in rows}
    assert "correct_signal_combo" in categories
    assert "successful_thesis" in categories


@pytest.mark.asyncio
async def test_returns_zero_on_unparseable_llm_output(
    db_session: AsyncSession, owner: User,
):
    """LLM emits free text instead of JSON → 0 lessons, no exception."""
    disc = _backtest_discussion(owner.id)
    db_session.add(disc)
    await db_session.commit()
    await db_session.refresh(disc)

    with patch("services.discussion.lessons.stream_chat",
        return_value=_stream_yielding("This is not JSON at all"),
    ), patch(
        "services.system_task_config_service.resolve",
        AsyncMock(return_value=("openai", "gpt-4o-mini")),
    ):
        n = await discussion_service.extract_winning_thesis_lessons(
            db_session, disc,
            win_prompt_text="prompt",
            user_id=str(owner.id),
        )
    assert n == 0


@pytest.mark.asyncio
async def test_returns_zero_on_llm_exception(
    db_session: AsyncSession, owner: User,
):
    """LLM call raises → 0, logged but not propagated."""
    disc = _backtest_discussion(owner.id)
    db_session.add(disc)
    await db_session.commit()
    await db_session.refresh(disc)

    async def _boom(*a, **kw):
        raise RuntimeError("LLM down")
        yield  # pragma: no cover

    with patch("services.discussion.lessons.stream_chat", side_effect=_boom,
    ), patch(
        "services.system_task_config_service.resolve",
        AsyncMock(return_value=("openai", "gpt-4o-mini")),
    ):
        n = await discussion_service.extract_winning_thesis_lessons(
            db_session, disc,
            win_prompt_text="prompt",
            user_id=str(owner.id),
        )
    assert n == 0


@pytest.mark.asyncio
async def test_empty_lessons_array_returns_zero(
    db_session: AsyncSession, owner: User,
):
    """LLM correctly identifies 'no reusable lesson' (luck case) →
    returns empty array → 0 written, no error."""
    disc = _backtest_discussion(owner.id)
    db_session.add(disc)
    await db_session.commit()
    await db_session.refresh(disc)

    with patch("services.discussion.lessons.stream_chat",
        return_value=_stream_yielding('{"lessons": []}'),
    ), patch(
        "services.system_task_config_service.resolve",
        AsyncMock(return_value=("openai", "gpt-4o-mini")),
    ):
        n = await discussion_service.extract_winning_thesis_lessons(
            db_session, disc,
            win_prompt_text="prompt",
            user_id=str(owner.id),
        )
    assert n == 0
