"""Unit tests for the discussion orchestrator.

Covers:
  - Discussion CRUD round trips
  - Persona-roster validation (dedup, cap, unknown ID rejection)
  - Status guards on update (can only edit drafts)
  - Turn JSON parsing — clean, code-fenced, malformed
  - run_round end-to-end with mocked stream_chat — yields the expected
    event sequence and persists turns
  - synthesize_conclusion structured-result coercion + status flip

stream_chat is mocked; market context helpers are patched so the test
suite doesn't reach out to TWSE / FinMind.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.discussion import Discussion, DiscussionTurn
from models.user import User, UserRole
from services import discussion_service


# ── fixtures ──────────────────────────────────────────────────────


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    user = User(
        email=f"disc-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x",
        role=UserRole.analyst,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


def _stream_events(text: str):
    """Build an async generator that mimics llm_router.stream_chat for one
    plain-text response. Yields one delta event then ends.
    """
    async def _gen(*_a, **_kw) -> AsyncIterator[dict]:
        yield {"type": "delta", "text": text}
    return _gen


def _stream_events_sequence(texts: list[str]):
    """Round-robin a list of replies — one per LLM call. Lets us simulate
    several personas in a row each returning their own JSON."""
    counter = {"i": 0}

    async def _gen(*_a, **_kw) -> AsyncIterator[dict]:
        idx = min(counter["i"], len(texts) - 1)
        counter["i"] += 1
        yield {"type": "delta", "text": texts[idx]}
    return _gen


# ── parse helpers ─────────────────────────────────────────────────


def test_parse_turn_response_clean_json():
    stance, content = discussion_service._parse_turn_response(
        '{"stance": "agree", "content": "我同意"}'
    )
    assert stance == "agree"
    assert content == "我同意"


def test_parse_turn_response_strips_code_fence():
    raw = '```json\n{"stance": "dissent", "content": "反對！"}\n```'
    stance, content = discussion_service._parse_turn_response(raw)
    assert stance == "dissent"
    assert content == "反對！"


def test_parse_turn_response_unknown_stance_falls_back():
    stance, content = discussion_service._parse_turn_response(
        '{"stance": "abstain", "content": "持中立"}'
    )
    assert stance == discussion_service.DEFAULT_STANCE
    assert content == "持中立"


def test_parse_turn_response_malformed_keeps_raw_text():
    raw = "我覺得台積電會漲，但沒辦法給 JSON"
    stance, content = discussion_service._parse_turn_response(raw)
    assert stance == discussion_service.DEFAULT_STANCE
    assert content == raw


# ── persona validation ────────────────────────────────────────────


def test_normalize_persona_ids_dedupes_and_caps():
    raw = ["buffett", "buffett", "graham", "munger", "lynch", "fisher",
           "smith", "marks", "klarman", "dalio", "soros"]
    out = discussion_service._normalize_persona_ids(raw)
    # _MAX_PERSONAS cap
    assert len(out) == discussion_service._MAX_PERSONAS
    assert out[0] == "buffett"
    assert len(set(out)) == len(out)


def test_normalize_persona_ids_rejects_unknown():
    with pytest.raises(ValueError, match="At least 2"):
        discussion_service._normalize_persona_ids(["not_a_real_persona"])


def test_normalize_persona_ids_min_two():
    with pytest.raises(ValueError):
        discussion_service._normalize_persona_ids(["buffett"])


# ── CRUD ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_discussion_persists(db_session: AsyncSession, owner: User):
    row = await discussion_service.create_discussion(
        db_session,
        owner_id=owner.id,
        topic="本週短線績優股",
        rules="每位專家發言 ≤ 200 字，必須引用至少一個數據點。",
        persona_ids=["buffett", "lynch", "simons"],
    )
    assert row.id is not None
    assert row.status == discussion_service.STATUS_DRAFT
    assert row.current_round == 0
    assert row.persona_ids == ["buffett", "lynch", "simons"]


@pytest.mark.asyncio
async def test_update_discussion_only_in_draft(db_session: AsyncSession, owner: User):
    row = await discussion_service.create_discussion(
        db_session,
        owner_id=owner.id,
        topic="topic",
        rules="rules",
        persona_ids=["buffett", "lynch"],
    )
    row.status = discussion_service.STATUS_DONE
    await db_session.commit()
    with pytest.raises(ValueError, match="already started"):
        await discussion_service.update_discussion(
            db_session, row, topic="new",
        )


@pytest.mark.asyncio
async def test_delete_discussion_cascades_turns(
    db_session: AsyncSession, owner: User,
):
    row = await discussion_service.create_discussion(
        db_session,
        owner_id=owner.id,
        topic="topic",
        rules="rules",
        persona_ids=["buffett", "lynch"],
    )
    db_session.add(DiscussionTurn(
        discussion_id=row.id, round=1, turn_index=0,
        persona_id="buffett", stance="agree", content="ok",
    ))
    await db_session.commit()

    deleted = await discussion_service.delete_discussion(
        db_session, discussion_id=row.id, owner_id=owner.id,
    )
    assert deleted is True

    leftover = (await db_session.scalars(
        select(DiscussionTurn).where(DiscussionTurn.discussion_id == row.id)
    )).all()
    assert leftover == []


@pytest.mark.asyncio
async def test_get_discussion_owner_scoped(
    db_session: AsyncSession, owner: User,
):
    other_owner_id = uuid.uuid4()
    row = await discussion_service.create_discussion(
        db_session,
        owner_id=owner.id,
        topic="topic",
        rules="rules",
        persona_ids=["buffett", "lynch"],
    )
    found = await discussion_service.get_discussion(
        db_session, discussion_id=row.id, owner_id=other_owner_id,
    )
    assert found is None


# ── run_round ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_round_emits_full_event_sequence(
    db_session: AsyncSession, owner: User,
):
    row = await discussion_service.create_discussion(
        db_session,
        owner_id=owner.id,
        topic="本週短線績優股",
        rules="≤200字",
        persona_ids=["buffett", "lynch"],
    )

    replies = [
        '{"stance": "supplement", "content": "我看好台積電"}',
        '{"stance": "dissent", "content": "我反對 buffett，看好聯發科"}',
    ]
    with patch(
        "services.discussion_service.stream_chat",
        side_effect=_stream_events_sequence(replies),
    ), patch(
        "services.discussion_service.gather_market_context",
        new=AsyncMock(return_value={"market": "TW", "top_gainers": []}),
    ):
        events = []
        async for ev in discussion_service.run_round(db_session, row, user_id=str(owner.id)):
            events.append((ev.type, ev.payload))

    types = [t for t, _ in events]
    assert types[0] == "round_start"
    assert types[1] == "context"
    # turn_start + delta + turn_end per persona
    assert types.count("turn_start") == 2
    assert types.count("turn_end") == 2
    assert types[-1] == "round_end"

    # turns persisted
    turns = (await db_session.scalars(
        select(DiscussionTurn)
        .where(DiscussionTurn.discussion_id == row.id)
        .order_by(DiscussionTurn.turn_index)
    )).all()
    assert [t.persona_id for t in turns] == ["buffett", "lynch"]
    assert turns[0].stance == "supplement"
    assert turns[0].content == "我看好台積電"
    assert turns[1].stance == "dissent"

    # status reset to draft (ready for next round) and round counter advanced
    refreshed = await db_session.get(Discussion, row.id)
    assert refreshed.status == discussion_service.STATUS_DRAFT
    assert refreshed.current_round == 1


@pytest.mark.asyncio
async def test_run_round_persists_partial_round_on_llm_error(
    db_session: AsyncSession, owner: User,
):
    """If a persona's LLM call errors mid-stream, we still persist the
    earlier turns and a placeholder for the failed turn."""
    row = await discussion_service.create_discussion(
        db_session,
        owner_id=owner.id,
        topic="topic",
        rules="rules",
        persona_ids=["buffett", "lynch"],
    )

    call_count = {"n": 0}

    async def _flaky(*_a, **_kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            yield {"type": "delta", "text": '{"stance":"supplement","content":"first"}'}
        else:
            yield {"type": "error", "message": "rate limit"}

    with patch(
        "services.discussion_service.stream_chat", side_effect=_flaky,
    ), patch(
        "services.discussion_service.gather_market_context",
        new=AsyncMock(return_value={"market": "TW"}),
    ):
        async for _ in discussion_service.run_round(db_session, row, user_id=str(owner.id)):
            pass

    turns = (await db_session.scalars(
        select(DiscussionTurn).where(DiscussionTurn.discussion_id == row.id)
        .order_by(DiscussionTurn.turn_index)
    )).all()
    assert len(turns) == 2
    assert turns[0].content == "first"
    # second turn fell back to default stance + placeholder content
    assert turns[1].stance == discussion_service.DEFAULT_STANCE
    assert "錯誤" in turns[1].content or "中止" in turns[1].content


# ── synthesize_conclusion ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_synthesize_conclusion_coerces_shape(
    db_session: AsyncSession, owner: User,
):
    row = await discussion_service.create_discussion(
        db_session,
        owner_id=owner.id,
        topic="topic",
        rules="rules",
        persona_ids=["buffett", "lynch"],
    )
    db_session.add(DiscussionTurn(
        discussion_id=row.id, round=1, turn_index=0,
        persona_id="buffett", stance="supplement", content="台積電",
    ))
    await db_session.commit()

    raw = (
        '{"recommended_symbols": ["2330", "2454", "00878"], '
        '"reasoning": "全員看好半導體", '
        '"risks": ["地緣政治", "高基期"], '
        '"time_horizon": "short_term", '
        '"consensus_score": 0.8}'
    )
    with patch(
        "services.discussion_service.stream_chat",
        side_effect=_stream_events(raw),
    ), patch(
        "services.discussion_service.gather_market_context",
        new=AsyncMock(return_value={"market": "TW"}),
    ):
        result = await discussion_service.synthesize_conclusion(
            db_session, row, user_id=str(owner.id),
        )

    assert result["recommended_symbols"] == ["2330", "2454", "00878"]
    assert result["consensus_score"] == 0.8
    assert result["time_horizon"] == "short_term"

    refreshed = await db_session.get(Discussion, row.id)
    assert refreshed.status == discussion_service.STATUS_DONE
    assert refreshed.conclusion["recommended_symbols"] == ["2330", "2454", "00878"]


@pytest.mark.asyncio
async def test_synthesize_conclusion_handles_malformed_output(
    db_session: AsyncSession, owner: User,
):
    row = await discussion_service.create_discussion(
        db_session,
        owner_id=owner.id,
        topic="topic",
        rules="rules",
        persona_ids=["buffett", "lynch"],
    )
    db_session.add(DiscussionTurn(
        discussion_id=row.id, round=1, turn_index=0,
        persona_id="buffett", stance="supplement", content="x",
    ))
    await db_session.commit()

    with patch(
        "services.discussion_service.stream_chat",
        side_effect=_stream_events("LLM 拒絕回答（沒有 JSON）"),
    ), patch(
        "services.discussion_service.gather_market_context",
        new=AsyncMock(return_value={"market": "TW"}),
    ):
        result = await discussion_service.synthesize_conclusion(
            db_session, row, user_id=str(owner.id),
        )

    assert result["recommended_symbols"] == []
    assert result["_parse_error"] is True
    assert result["consensus_score"] == 0.0


def test_safe_conclusion_clamps_consensus_and_caps_lists():
    raw = (
        '{"recommended_symbols": ["A","B","C","D","E","F","G"], '
        '"reasoning": "x", "risks": [1,2,3,4,5,6,7,8,9,10,11,12], '
        '"time_horizon": "lifetime", '
        '"consensus_score": 5.0}'
    )
    out = discussion_service._safe_conclusion(raw)
    assert len(out["recommended_symbols"]) == 5
    assert len(out["risks"]) == 10
    assert out["time_horizon"] == "short_term"   # invalid → fallback
    assert out["consensus_score"] == 1.0          # clamped to [0,1]
