"""Tests for the finer per-round ctx usage feature:

  - `run_round` folds per-persona exact tokens + the prompt-composition
    breakdown into `turn_end` and persists the breakdown on the turn.
  - `llm_usage_service.discussion_round_persona_usage` returns the exact
    per-(round, persona) token / cost / tool tally backing the
    `/round-usage/detail` endpoint.

Service-level (no HTTP client) — mocks `stream_chat` + `gather_market_
context` the same way the existing run_round tests do.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.discussion import DiscussionTurn
from models.user import User, UserRole
from services import discussion_service, llm_usage_service


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    user = User(
        email=f"usage-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x",
        role=UserRole.analyst,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def _stream_with_usage(replies: list[str], prompts: list[int]):
    """Round-robin replies + a usage event per persona so per-persona
    token attribution can be asserted."""
    counter = {"i": 0}

    async def _gen(*_a, **_kw) -> AsyncIterator[dict]:
        idx = min(counter["i"], len(replies) - 1)
        counter["i"] += 1
        yield {"type": "delta", "text": replies[idx]}
        yield {
            "type": "usage",
            "prompt_tokens": prompts[idx],
            "completion_tokens": 50,
        }
    return _gen


@pytest.mark.asyncio
async def test_run_round_emits_and_persists_input_breakdown(
    db_session: AsyncSession, owner: User,
):
    from unittest.mock import AsyncMock, patch

    row = await discussion_service.create_discussion(
        db_session,
        owner_id=owner.id,
        topic="本週短線績優股",
        rules="≤200字",
        persona_ids=["buffett", "lynch"],
    )
    ctx = {
        "market": "TW",
        "focus_briefs": [{"symbol": "2330", "pe": 18.5, "yield": None}],
        "macro": {"fed": 5.25},
        "news_sentiment": {"bullish": 3, "bearish": 1},
    }
    replies = [
        '{"stance": "supplement", "content": "看好台積電"}',
        '{"stance": "dissent", "content": "看好聯發科"}',
    ]
    with patch(
        "services.discussion_service.stream_chat",
        side_effect=_stream_with_usage(replies, [1000, 2000]),
    ), patch(
        "services.discussion_service.gather_market_context",
        new=AsyncMock(return_value=ctx),
    ):
        events = []
        async for ev in discussion_service.run_round(
            db_session, row, user_id=str(owner.id),
        ):
            events.append((ev.type, ev.payload))

    turn_ends = [p for t, p in events if t == "turn_end"]
    assert len(turn_ends) == 2
    # turn_end carries per-persona exact tokens + the breakdown live.
    for p in turn_ends:
        assert p["prompt_tokens"] > 0
        assert p["completion_tokens"] == 50
        assert "tool_call_count" in p
        bd = p["breakdown"]
        assert bd is not None
        assert "sections" in bd and "blocks" in bd
        assert bd["sections"]["system"] > 0
        assert bd["total_chars"] == sum(bd["sections"].values())

    # Breakdown persisted on each turn; focus_briefs surfaces as a block.
    turns = (await db_session.scalars(
        select(DiscussionTurn)
        .where(DiscussionTurn.discussion_id == row.id)
        .order_by(DiscussionTurn.turn_index)
    )).all()
    assert len(turns) == 2
    for t in turns:
        assert t.input_breakdown is not None
        assert "focus_briefs" in t.input_breakdown["blocks"]
        # `market` is always-included; the filtered ctx is non-trivial.
        assert t.input_breakdown["sections"]["context_json"] > 0


@pytest.mark.asyncio
async def test_discussion_round_persona_usage_attribution(
    db_session: AsyncSession, owner: User,
):
    from unittest.mock import AsyncMock, patch

    row = await discussion_service.create_discussion(
        db_session,
        owner_id=owner.id,
        topic="topic",
        rules="rules",
        persona_ids=["buffett", "lynch"],
    )
    replies = [
        '{"stance": "supplement", "content": "a"}',
        '{"stance": "supplement", "content": "b"}',
    ]
    with patch(
        "services.discussion_service.stream_chat",
        side_effect=_stream_with_usage(replies, [1000, 2000]),
    ), patch(
        "services.discussion_service.gather_market_context",
        new=AsyncMock(return_value={"market": "TW", "macro": {"fed": 5.25}}),
    ):
        async for _ in discussion_service.run_round(
            db_session, row, user_id=str(owner.id),
        ):
            pass

    detail = await llm_usage_service.discussion_round_persona_usage(
        db_session, discussion_id=row.id,
    )
    by_persona = {d["persona_id"]: d for d in detail}
    assert set(by_persona) == {"buffett", "lynch"}
    assert all(d["round"] == 1 for d in detail)
    # First call (buffett) got prompt_tokens=1000, second (lynch)=2000.
    assert by_persona["buffett"]["prompt_tokens"] == 1000
    assert by_persona["lynch"]["prompt_tokens"] == 2000
    assert by_persona["buffett"]["total_tokens"] == 1050
    assert by_persona["buffett"]["cost_usd"] >= 0
