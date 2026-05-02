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

import json
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


# ── strip_think_blocks ────────────────────────────────────────────


def test_strip_think_blocks_removes_single_block():
    raw = '<think>internal monologue</think>{"stance": "agree"}'
    assert discussion_service.strip_think_blocks(raw) == '{"stance": "agree"}'


def test_strip_think_blocks_handles_multiline():
    raw = '<think>\nline 1\nline 2\n</think>\n\n{"stance": "agree"}'
    assert discussion_service.strip_think_blocks(raw) == '{"stance": "agree"}'


def test_strip_think_blocks_removes_multiple():
    raw = '<think>a</think>before<think>b</think>after'
    assert discussion_service.strip_think_blocks(raw) == "beforeafter"


def test_strip_think_blocks_no_blocks_unchanged():
    raw = "plain output, no thinking"
    assert discussion_service.strip_think_blocks(raw) == "plain output, no thinking"


def test_strip_think_blocks_unclosed_left_alone():
    """Edge case: model emitted `<think>` but never closed it. Regex
    won't match, so we leave the text alone — the streaming filter
    handles the unclosed-tag case separately by dropping the tail."""
    raw = "<think>never closed{...}"
    assert discussion_service.strip_think_blocks(raw) == "<think>never closed{...}"


# ── _ThinkBlockFilter (streaming filter) ─────────────────────────


def test_think_filter_passes_clean_text_through():
    f = discussion_service._ThinkBlockFilter()
    assert f.feed("hello world") == "hello world"
    assert f.flush() == ""


def test_think_filter_drops_complete_block_in_one_chunk():
    f = discussion_service._ThinkBlockFilter()
    out = f.feed("before<think>HIDDEN</think>after")
    assert out == "beforeafter"


def test_think_filter_drops_block_split_across_chunks():
    f = discussion_service._ThinkBlockFilter()
    out1 = f.feed("be<thi")
    out2 = f.feed("nk>HID")
    out3 = f.feed("DEN</thi")
    out4 = f.feed("nk>after")
    assert (out1 + out2 + out3 + out4) == "beafter"


def test_think_filter_drops_unclosed_tail():
    """If the stream ends mid-think (model never closed the tag), the
    held-back text is discarded on flush — better silent gap than
    flashing chain-of-thought to the user."""
    f = discussion_service._ThinkBlockFilter()
    head = f.feed("intro<think>still thinking when stream died")
    tail = f.flush()
    assert head == "intro"
    assert tail == ""


def test_think_filter_holds_partial_open_tag_at_chunk_boundary():
    """A bare `<` at end of a chunk could be the start of `<think>` —
    don't emit it until we know."""
    f = discussion_service._ThinkBlockFilter()
    out1 = f.feed("text<")
    out2 = f.feed("more")
    # The "<" plus "more" doesn't form `<think>`, so the held char gets
    # released as part of the next chunk.
    assert "text" in out1 + out2
    assert (out1 + out2) == "text<more"


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


def test_parse_turn_response_strips_think_block_before_json_parse():
    """Reasoning models prefix their JSON with a `<think>...</think>` block.
    The parser must strip it so the JSON is found, not lost behind a
    DEFAULT_STANCE fallback."""
    raw = (
        "<think>\nLet me analyze the data...\nthe stock looks bullish.\n</think>\n\n"
        '{"stance": "supplement", "content": "看好台積電"}'
    )
    stance, content = discussion_service._parse_turn_response(raw)
    assert stance == "supplement"
    assert content == "看好台積電"


def test_parse_turn_response_handles_literal_newlines_in_content():
    """LLMs (especially zh-TW reasoning models) frequently emit JSON
    where the `content` string contains literal newline / tab characters
    instead of `\\n` escapes. Strict JSON would reject these; the
    `strict=False` path keeps the parse alive."""
    raw = '{"stance": "supplement", "content": "第一段\n\n第二段\n\t縮排"}'
    stance, content = discussion_service._parse_turn_response(raw)
    assert stance == "supplement"
    assert "第一段" in content
    assert "第二段" in content


def test_parse_turn_response_extracts_json_from_prose_prefix():
    """Sometimes the model prefixes its JSON with prose explaining what
    it's about to output. The salvage path extracts the balanced
    object so we still get a clean stance + content."""
    raw = (
        "Here is my analysis of the situation:\n\n"
        '{"stance": "dissent", "content": "I disagree with Buffett."}\n\n'
        "Hope this helps!"
    )
    stance, content = discussion_service._parse_turn_response(raw)
    assert stance == "dissent"
    assert content == "I disagree with Buffett."


def test_parse_turn_response_extracts_json_with_nested_braces_in_string():
    """The salvage tracker must respect string boundaries — a `}` inside
    a JSON string shouldn't close the outer object."""
    raw = '{"stance": "supplement", "content": "use the formula x = {a + b}"}'
    stance, content = discussion_service._parse_turn_response(raw)
    assert stance == "supplement"
    assert "{a + b}" in content


def test_extract_json_object_returns_none_when_no_brace():
    out = discussion_service._extract_json_object("plain text, no JSON here")
    assert out is None


def test_extract_json_object_handles_escaped_quotes():
    """Escaped `\\"` inside a string must not toggle string mode."""
    raw = 'prefix {"key": "value with \\"quotes\\" inside"} suffix'
    out = discussion_service._extract_json_object(raw)
    assert out is not None
    assert json.loads(out, strict=False)["key"] == 'value with "quotes" inside'


def test_parse_turn_response_falls_back_with_thinking_stripped_when_json_invalid():
    """Even if JSON parse fails, the fallback content should be the
    thinking-stripped text — not the raw prose with `<think>` noise."""
    raw = (
        "<think>I'm overthinking this</think>"
        "I refuse to output JSON, here is my prose answer."
    )
    stance, content = discussion_service._parse_turn_response(raw)
    assert stance == discussion_service.DEFAULT_STANCE
    assert "<think>" not in content
    assert "prose answer" in content


def test_parse_turn_response_salvages_truncated_content_field():
    """LLM hits `max_tokens` mid-content — JSON wrapper has no closing
    `"}` so `_extract_json_object` returns None. The truncation salvage
    path extracts the partial content + already-emitted stance instead
    of dropping the user back to "raw JSON wrapper" view."""
    raw = (
        '{"stance":"supplement","content":"台積電2330短線看好，'
        '建議分批進場到610元，停損點設在565元（-7%）。目標價'
    )
    stance, content = discussion_service._parse_turn_response(raw)
    assert stance == "supplement"
    # The persona's actual analysis comes through, not the JSON wrapper.
    assert content.startswith("台積電2330短線看好")
    assert "目標價" in content
    assert '"stance"' not in content
    assert '"content"' not in content


def test_parse_turn_response_salvages_truncation_with_unknown_stance():
    """If the LLM emitted an off-list stance before truncating, the
    salvage path falls it back to DEFAULT_STANCE just like the regular
    parse would for a complete message with a bad stance."""
    raw = '{"stance":"abstain","content":"無法決定，等更多資料'
    stance, content = discussion_service._parse_turn_response(raw)
    assert stance == discussion_service.DEFAULT_STANCE
    assert content == "無法決定，等更多資料"


def test_parse_turn_response_salvages_truncation_decodes_escapes():
    """Truncated content with JSON escape sequences (`\\n`, `\\\"`) gets
    decoded to real characters so the persisted text is readable rather
    than littered with backslashes."""
    raw = (
        '{"stance":"supplement","content":"第一段重點\\n\\n第二段重點：'
        '引用Buffett \\"安全邊際\\" 概念，目前估值'
    )
    stance, content = discussion_service._parse_turn_response(raw)
    assert stance == "supplement"
    assert "第一段重點\n\n第二段重點" in content
    assert '"安全邊際"' in content
    # Trailing backslash-escape was clean, no garbage carried through.
    assert content.endswith("目前估值")


def test_parse_turn_response_salvages_truncation_drops_dangling_escape():
    """If truncation lands mid-escape (lone `\\` at end), the decoder
    drops the dangling backslash rather than carrying it through to the
    rendered text."""
    raw = '{"stance":"agree","content":"重點一是估值合理，重點二是\\'
    _, content = discussion_service._parse_turn_response(raw)
    assert not content.endswith("\\")
    assert "重點一" in content
    assert "重點二" in content


def test_parse_turn_response_salvage_skipped_when_no_content_field():
    """If the truncated text doesn't contain a `"content":"` opener at
    all, the salvage path is skipped and we fall back to the raw-text
    DEFAULT_STANCE behaviour — no false positives."""
    raw = "Just some prose, no JSON wrapper at all."
    stance, content = discussion_service._parse_turn_response(raw)
    assert stance == discussion_service.DEFAULT_STANCE
    assert content == raw


def test_salvage_truncated_json_returns_none_without_content_opener():
    out = discussion_service._salvage_truncated_json(
        '{"stance":"agree","other":"value"'
    )
    assert out is None


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
async def test_run_round_forwards_tool_call_and_tool_result_events(
    db_session: AsyncSession, owner: User,
):
    """When the upstream stream emits `tool_call` / `tool_result`
    (claude_agent SDK or _openai_compat_tool_loop), run_round must
    forward them as TurnEvents of the same type so the SSE consumer
    can show what tools were invoked. Each forwarded event carries
    the surrounding `round` / `turn_index` / `persona_id` so the
    frontend can attach the tool log to the right speaker."""
    row = await discussion_service.create_discussion(
        db_session,
        owner_id=owner.id,
        topic="t", rules="r",
        persona_ids=["buffett", "lynch"],
    )

    async def _stream_with_tools(*_a, **_kw):
        # Persona makes one tool call, gets a result, then writes the
        # final structured JSON.
        yield {
            "type": "tool_call", "id": "call-1",
            "name": "get_quote", "args": {"symbol": "2330"},
        }
        yield {
            "type": "tool_result", "id": "call-1",
            "name": "get_quote", "summary": "{\"price\": 950}", "is_error": False,
        }
        yield {"type": "delta",
               "text": '{"stance": "supplement", "content": "依工具回報 2330 報 950"}'}

    with patch(
        "services.discussion_service.stream_chat",
        side_effect=_stream_with_tools,
    ), patch(
        "services.discussion_service.gather_market_context",
        new=AsyncMock(return_value={"market": "TW"}),
    ):
        events = []
        async for ev in discussion_service.run_round(
            db_session, row, user_id=str(owner.id),
        ):
            events.append((ev.type, ev.payload))

    types = [t for t, _ in events]
    # Forwarded events appear inside each persona's turn.
    assert types.count("tool_call") == 2  # 2 personas × 1 tool call
    assert types.count("tool_result") == 2

    # Each forwarded event tags the speaker so the UI can route them.
    tool_calls = [p for t, p in events if t == "tool_call"]
    assert all("persona_id" in p and "round" in p for p in tool_calls)
    assert tool_calls[0]["name"] == "get_quote"
    assert tool_calls[0]["args"] == {"symbol": "2330"}


@pytest.mark.asyncio
async def test_run_round_increments_round_number_on_subsequent_calls(
    db_session: AsyncSession, owner: User,
):
    """Two back-to-back calls to `run_round` must yield round=1 then round=2
    in their event payloads, AND persist `current_round=2` on the
    discussion. Regression test for the bug where the sidebar / turn cards
    kept showing R1 across multiple rounds — would only catch a backend
    increment failure (frontend cache invalidation is tested separately)."""
    row = await discussion_service.create_discussion(
        db_session,
        owner_id=owner.id,
        topic="topic",
        rules="rules",
        persona_ids=["buffett", "lynch"],
    )

    replies_round_1 = [
        '{"stance":"supplement","content":"r1-buffett"}',
        '{"stance":"supplement","content":"r1-lynch"}',
    ]
    replies_round_2 = [
        '{"stance":"supplement","content":"r2-buffett"}',
        '{"stance":"supplement","content":"r2-lynch"}',
    ]

    with patch(
        "services.discussion_service.gather_market_context",
        new=AsyncMock(return_value={"market": "TW", "top_gainers": []}),
    ):
        with patch(
            "services.discussion_service.stream_chat",
            side_effect=_stream_events_sequence(replies_round_1),
        ):
            r1_events = []
            async for ev in discussion_service.run_round(db_session, row, user_id=str(owner.id)):
                r1_events.append((ev.type, ev.payload))

        with patch(
            "services.discussion_service.stream_chat",
            side_effect=_stream_events_sequence(replies_round_2),
        ):
            r2_events = []
            async for ev in discussion_service.run_round(db_session, row, user_id=str(owner.id)):
                r2_events.append((ev.type, ev.payload))

    r1_round_starts = [p for t, p in r1_events if t == "round_start"]
    r2_round_starts = [p for t, p in r2_events if t == "round_start"]
    assert r1_round_starts == [{"round": 1}]
    assert r2_round_starts == [{"round": 2}]

    r1_turn_ends = [p for t, p in r1_events if t == "turn_end"]
    r2_turn_ends = [p for t, p in r2_events if t == "turn_end"]
    assert all(p["round"] == 1 for p in r1_turn_ends), r1_turn_ends
    assert all(p["round"] == 2 for p in r2_turn_ends), r2_turn_ends

    refreshed = await db_session.get(Discussion, row.id)
    assert refreshed.current_round == 2

    persisted_rounds = sorted(
        t.round
        for t in (await db_session.scalars(
            select(DiscussionTurn).where(DiscussionTurn.discussion_id == row.id)
        )).all()
    )
    assert persisted_rounds == [1, 1, 2, 2]


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
async def test_synthesize_conclusion_reuses_round_context_snapshot(
    db_session: AsyncSession, owner: User,
):
    """When a round has already snapshotted its context to
    `discussion_round_contexts`, the synthesizer must reuse that
    captured payload instead of re-running `gather_market_context`
    — otherwise post-close synthesises would silently drift from
    the data the personas reasoned over."""
    from models.discussion_round_context import DiscussionRoundContext

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
    db_session.add(DiscussionRoundContext(
        discussion_id=row.id, round=1,
        context={"market": "TW", "marker": "from-snapshot"},
    ))
    await db_session.commit()

    raw = (
        '{"recommended_symbols": ["2330"], "reasoning": "ok", '
        '"risks": [], "time_horizon": "short_term", '
        '"consensus_score": 0.5}'
    )
    gather_mock = AsyncMock(return_value={"market": "TW"})
    with patch(
        "services.discussion_service.stream_chat",
        side_effect=_stream_events(raw),
    ), patch(
        "services.discussion_service.gather_market_context",
        new=gather_mock,
    ):
        await discussion_service.synthesize_conclusion(
            db_session, row, user_id=str(owner.id),
        )
    # Fresh fetch must NOT have been called when a snapshot existed.
    gather_mock.assert_not_called()


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


# ── focus symbol extraction (B5) ─────────────────────────────────


def test_extract_focus_symbols_pulls_tw_codes():
    out = discussion_service.extract_focus_symbols(
        "找出 2330、2454 與 00878 短線買點"
    )
    assert "2330" in out
    assert "2454" in out
    assert "00878" in out


def test_extract_focus_symbols_dedupes_and_caps():
    out = discussion_service.extract_focus_symbols(
        "2330 2330 2454 2317 1101 2412 1216 2882 2891"
    )
    assert len(out) <= discussion_service._MAX_FOCUS_SYMBOLS
    assert len(out) == len(set(out))


def test_extract_focus_symbols_empty_when_none():
    assert discussion_service.extract_focus_symbols("純策略討論不提具體標的") == []


def test_extract_focus_symbols_filters_year_like_tw():
    """4-digit year tokens (1900-2099) must NOT be treated as TW codes;
    `2026 Q1 預估` shouldn't pollute per-symbol news lookups."""
    out = discussion_service.extract_focus_symbols(
        "2026 Q1 與 2030 展望，主軸在 2330", market="TW",
    )
    assert "2330" in out
    assert "2026" not in out
    assert "2030" not in out


def test_extract_focus_symbols_us_market_uses_uppercase_tickers():
    out = discussion_service.extract_focus_symbols(
        "Should I rotate from NVDA into AMD given USD weakness?",
        market="US",
    )
    assert "NVDA" in out
    assert "AMD" in out
    # Stopword guard — bare uppercase 'USD' must not count.
    assert "USD" not in out


def test_extract_focus_symbols_cashtag_works_in_any_market():
    out = discussion_service.extract_focus_symbols(
        "$AAPL beats $MSFT this week — same window 2330 也漲", market="TW",
    )
    # Cashtags first, then TW codes after.
    assert out[:2] == ["AAPL", "MSFT"]
    assert "2330" in out


def test_extract_focus_symbols_global_picks_crypto_universe_only():
    out = discussion_service.extract_focus_symbols(
        "BTC + ETH + SOL leading the rally; AAA random ticker noise",
        market="GLOBAL",
    )
    assert "BTC" in out and "ETH" in out and "SOL" in out
    # `AAA` not in the curated Top-20 universe — must NOT be picked up.
    assert "AAA" not in out


# ── _normalize_market ─────────────────────────────────────────────


def test_normalize_market_defaults_when_blank():
    assert discussion_service._normalize_market(None) == "TW"
    assert discussion_service._normalize_market("") == "TW"


def test_normalize_market_uppercases():
    assert discussion_service._normalize_market("us") == "US"
    assert discussion_service._normalize_market("global") == "GLOBAL"


def test_normalize_market_rejects_unknown():
    with pytest.raises(ValueError):
        discussion_service._normalize_market("JP")


# ── create + update wires market through ─────────────────────────


@pytest.mark.asyncio
async def test_create_discussion_persists_market(
    db_session: AsyncSession, owner: User,
):
    row = await discussion_service.create_discussion(
        db_session,
        owner_id=owner.id,
        topic="NVDA earnings preview",
        rules="≤200 字",
        persona_ids=["buffett", "lynch"],
        market="us",
    )
    assert row.market == "US"


@pytest.mark.asyncio
async def test_create_discussion_defaults_to_tw(
    db_session: AsyncSession, owner: User,
):
    row = await discussion_service.create_discussion(
        db_session,
        owner_id=owner.id,
        topic="topic",
        rules="rules",
        persona_ids=["buffett", "lynch"],
    )
    assert row.market == "TW"


@pytest.mark.asyncio
async def test_update_discussion_changes_market_in_draft(
    db_session: AsyncSession, owner: User,
):
    row = await discussion_service.create_discussion(
        db_session,
        owner_id=owner.id,
        topic="t", rules="r",
        persona_ids=["buffett", "lynch"],
    )
    await discussion_service.update_discussion(
        db_session, row, market="GLOBAL",
    )
    assert row.market == "GLOBAL"


def test_is_speculative_etf_flags_leveraged_inverse_futures_only():
    """`top_gainers` must not include 2x leveraged / inverse / futures-
    tracking ETFs — they mean-revert the next session and persuade the
    persona to recommend tomorrow's reversal candidate. Plain ETFs and
    ordinary stocks must pass through unchanged."""
    is_spec = discussion_service._is_speculative_etf
    # Drop these
    assert is_spec("00715L") is True   # 2x leveraged Brent oil
    assert is_spec("00642U") is True   # S&P oil futures
    assert is_spec("00632R") is True   # 元大台灣 50 反 1
    # Keep these
    assert is_spec("0050") is False    # plain index ETF
    assert is_spec("00878") is False   # dividend ETF
    assert is_spec("2330") is False    # ordinary stock
    assert is_spec("00713B") is False  # bond ETF — not speculative
    # Defensive: non-string / None must not crash
    assert is_spec(None) is False
    assert is_spec(2330) is False


@pytest.mark.asyncio
async def test_gather_market_context_includes_per_symbol_block_when_focused(
    db_session: AsyncSession,
):
    """When focus_symbols is supplied, per_symbol_news_sentiment gets the
    aggregated rows for any symbol with scored news; symbols with no
    scored news are simply omitted (not present as empty stubs)."""
    from datetime import UTC, datetime
    from sqlalchemy import select as _select
    # Pre-import so patch() can find them — gather_market_context does
    # local lazy imports and patching a not-yet-imported module fails.
    import services.tw_market_service as _tw   # noqa: F401
    from models.news_article import NewsArticle
    from services.ingest.repository import NewsArticleRow, insert_news_articles

    await insert_news_articles(db_session, [
        NewsArticleRow(
            market="TW", symbol="9999",
            published_at=datetime.now(UTC),
            title="9999 重大利多消息",
            link="https://example.com/per_sym_a",
            publisher="test", summary=None, payload=None, source="finmind",
        ),
    ])
    row = await db_session.scalar(
        _select(NewsArticle).where(NewsArticle.symbol == "9999")
    )
    row.sentiment_score = 0.7
    row.sentiment_label = "bullish"
    row.sentiment_scored_at = datetime.now(UTC)
    await db_session.commit()

    with patch.object(_tw, "get_screener", new=AsyncMock(return_value=[])), \
         patch.object(_tw, "get_index", new=AsyncMock(return_value={})), \
         patch(
            "services.discussion_service._assemble_macro_block",
            new=AsyncMock(return_value={}),
         ), \
         patch(
            "services.discussion_service._assemble_focus_briefs",
            new=AsyncMock(return_value=[]),
         ):
        ctx = await discussion_service.gather_market_context(
            db_session, focus_symbols=["9999", "8888"],
        )

    assert "per_symbol_news_sentiment" in ctx
    assert "9999" in ctx["per_symbol_news_sentiment"]
    # 8888 has no scored news → omitted
    assert "8888" not in ctx["per_symbol_news_sentiment"]
    block = ctx["per_symbol_news_sentiment"]["9999"]
    assert block["bullish"] == 1
    assert block["count"] == 1


@pytest.mark.asyncio
async def test_gather_market_context_includes_international_sentiment(
    db_session: AsyncSession,
):
    """`international_sentiment` block is populated from market='GLOBAL'
    rows regardless of the discussion's primary market — so a TW
    persona can reason about Fed / FOMC context that the
    `ingest_news_international` cron writes."""
    from datetime import UTC, datetime
    from sqlalchemy import select as _select
    import services.tw_market_service as _tw   # noqa: F401
    from models.news_article import NewsArticle
    from services.ingest.repository import NewsArticleRow, insert_news_articles

    await insert_news_articles(db_session, [
        NewsArticleRow(
            market="GLOBAL", symbol=None,
            published_at=datetime.now(UTC),
            title="聯準會連三降息 美股收高",
            link="https://example.com/intl-1",
            publisher="鉅亨網", summary=None, payload=None,
            source="google_news_intl",
        ),
    ])
    row = await db_session.scalar(
        _select(NewsArticle).where(NewsArticle.market == "GLOBAL")
    )
    row.sentiment_score = 0.6
    row.sentiment_label = "bullish"
    row.sentiment_scored_at = datetime.now(UTC)
    await db_session.commit()

    with patch.object(_tw, "get_screener", new=AsyncMock(return_value=[])), \
         patch.object(_tw, "get_index", new=AsyncMock(return_value={})), \
         patch(
            "services.discussion_service._assemble_macro_block",
            new=AsyncMock(return_value={}),
         ), \
         patch(
            "services.discussion_service._assemble_focus_briefs",
            new=AsyncMock(return_value=[]),
         ):
        ctx = await discussion_service.gather_market_context(db_session)

    assert "international_sentiment" in ctx
    block = ctx["international_sentiment"]
    assert block is not None
    assert block["bullish"] == 1
    assert any("聯準會" in h["title"] for h in block["headlines"])
    # The TW market_news block should NOT pick up the GLOBAL row even
    # when the GLOBAL row is the only news in the DB — different market
    # codes mean different aggregation buckets.
    tw_block = ctx["news_sentiment"]
    assert tw_block is not None
    assert tw_block["bullish"] == 0


@pytest.mark.asyncio
async def test_gather_market_context_includes_chip_metrics_for_tw(
    db_session: AsyncSession,
):
    """`top_foreign_buyers` + `margin_balance_trend` are populated for
    market='TW' from the chip-metric daily archives. Other markets
    keep them at the default empty/None so the prompt template
    doesn't have to special-case missing TW data when the discussion
    is e.g. US-focused."""
    from datetime import date as _date
    import services.tw_market_service as _tw   # noqa: F401
    from services.ingest.repository import (
        InstitutionalDailyRow,
        MarginDailyRow,
        upsert_institutional_daily,
        upsert_margin_daily,
    )

    today = _date.today()
    await upsert_institutional_daily(db_session, [
        InstitutionalDailyRow(
            market="TW", symbol="2330", ts=today,
            fini_buy=100_000, fini_sell=20_000,
            sitc_buy=1_000, sitc_sell=500,
            dealer_buy=200, dealer_sell=100,
            source="twse",
        ),
        InstitutionalDailyRow(
            market="TW", symbol="2454", ts=today,
            fini_buy=50_000, fini_sell=10_000,
            sitc_buy=500, sitc_sell=200,
            dealer_buy=100, dealer_sell=50,
            source="twse",
        ),
    ])
    await upsert_margin_daily(db_session, [
        MarginDailyRow(
            market="TW", symbol="2330", ts=today,
            margin_purchase=5_000, margin_balance=20_000,
            short_sale=1_000, short_balance=3_000,
            source="twse",
        ),
    ])

    with patch.object(_tw, "get_screener", new=AsyncMock(return_value=[])), \
         patch.object(_tw, "get_index", new=AsyncMock(return_value={})), \
         patch(
            "services.discussion_service._assemble_macro_block",
            new=AsyncMock(return_value={}),
         ), \
         patch(
            "services.discussion_service._assemble_focus_briefs",
            new=AsyncMock(return_value=[]),
         ):
        ctx = await discussion_service.gather_market_context(
            db_session, market="TW",
        )

    # 2330's net foreign buy (80k) > 2454's (40k), so 2330 leads.
    assert ctx["top_foreign_buyers"][0]["symbol"] == "2330"
    assert ctx["top_foreign_buyers"][0]["net_foreign_buy"] == 80_000

    trend = ctx["margin_balance_trend"]
    assert trend is not None
    assert trend["total_margin_balance"] == 20_000
    assert trend["total_short_balance"] == 3_000
    assert trend["days_observed"] == 1


@pytest.mark.asyncio
async def test_gather_market_context_skips_chip_metrics_for_non_tw(
    db_session: AsyncSession,
):
    """Non-TW markets don't get chip-metric blocks populated — TWSE-
    specific data shouldn't bleed into a US discussion's context."""
    import services.tw_market_service as _tw   # noqa: F401

    with patch.object(_tw, "get_screener", new=AsyncMock(return_value=[])), \
         patch.object(_tw, "get_index", new=AsyncMock(return_value={})), \
         patch(
            "services.discussion_service._assemble_macro_block",
            new=AsyncMock(return_value={}),
         ), \
         patch(
            "services.discussion_service._assemble_focus_briefs",
            new=AsyncMock(return_value=[]),
         ):
        ctx = await discussion_service.gather_market_context(
            db_session, market="US",
        )

    assert ctx["top_foreign_buyers"] == []
    assert ctx["margin_balance_trend"] is None


# ── round context snapshots (PR #135) ────────────────────────────


@pytest.mark.asyncio
async def test_run_round_persists_context_snapshot(
    db_session: AsyncSession, owner: User,
):
    """Each round writes its assembled context dict into
    `discussion_round_contexts` keyed on (discussion_id, round) so
    re-opening the discussion can replay 'what data the personas
    saw at the time'."""
    from models.discussion_round_context import DiscussionRoundContext

    row = await discussion_service.create_discussion(
        db_session,
        owner_id=owner.id,
        topic="本週短線", rules="≤200字",
        persona_ids=["buffett", "lynch"],
    )

    snapshot_ctx = {
        "market": "TW",
        "top_gainers": [{"symbol": "2330"}],
        "captured_at": "2026-04-30T08:00:00+00:00",
    }

    replies = [
        '{"stance":"supplement","content":"x"}',
        '{"stance":"agree","content":""}',
    ]
    with patch(
        "services.discussion_service.stream_chat",
        side_effect=_stream_events_sequence(replies),
    ), patch(
        "services.discussion_service.gather_market_context",
        new=AsyncMock(return_value=snapshot_ctx),
    ):
        async for _ in discussion_service.run_round(
            db_session, row, user_id=str(owner.id),
        ):
            pass

    snaps = (await db_session.scalars(
        select(DiscussionRoundContext)
        .where(DiscussionRoundContext.discussion_id == row.id)
    )).all()
    assert len(snaps) == 1
    assert snaps[0].round == 1
    assert snaps[0].context == snapshot_ctx


@pytest.mark.asyncio
async def test_run_round_snapshot_failure_does_not_abort_round(
    db_session: AsyncSession, owner: User,
):
    """A flaky context snapshot write should NOT bring down the
    round — personas still reply, turns still persist, status still
    resets. Just the snapshot row is missing."""
    from models.discussion_round_context import DiscussionRoundContext

    row = await discussion_service.create_discussion(
        db_session,
        owner_id=owner.id,
        topic="x", rules="y",
        persona_ids=["buffett", "lynch"],
    )

    replies = [
        '{"stance":"supplement","content":"x"}',
        '{"stance":"agree","content":""}',
    ]
    with patch(
        "services.discussion_service.stream_chat",
        side_effect=_stream_events_sequence(replies),
    ), patch(
        "services.discussion_service.gather_market_context",
        new=AsyncMock(return_value={"market": "TW"}),
    ), patch(
        "services.discussion_service._upsert_round_context",
        new=AsyncMock(side_effect=RuntimeError("disk full")),
    ):
        async for _ in discussion_service.run_round(
            db_session, row, user_id=str(owner.id),
        ):
            pass

    # Round still completed: turns persisted, status reset.
    refreshed = await db_session.get(Discussion, row.id)
    assert refreshed.status == discussion_service.STATUS_DRAFT
    assert refreshed.current_round == 1
    turns = await discussion_service.get_turns(db_session, discussion_id=row.id)
    assert len(turns) == 2
    # But the snapshot didn't land.
    snaps = (await db_session.scalars(
        select(DiscussionRoundContext)
        .where(DiscussionRoundContext.discussion_id == row.id)
    )).all()
    assert snaps == []


@pytest.mark.asyncio
async def test_get_round_contexts_returns_ordered_snapshots(
    db_session: AsyncSession, owner: User,
):
    """`get_round_contexts` returns snapshots ordered ascending by
    round so the API endpoint can render them chronologically."""
    from models.discussion_round_context import DiscussionRoundContext

    row = await discussion_service.create_discussion(
        db_session,
        owner_id=owner.id,
        topic="x", rules="y",
        persona_ids=["buffett", "lynch"],
    )
    # Seed two rounds out of order to verify the order_by clause.
    db_session.add_all([
        DiscussionRoundContext(
            discussion_id=row.id, round=2,
            context={"market": "TW", "round_marker": 2},
        ),
        DiscussionRoundContext(
            discussion_id=row.id, round=1,
            context={"market": "TW", "round_marker": 1},
        ),
    ])
    await db_session.commit()

    rows = await discussion_service.get_round_contexts(
        db_session, discussion_id=row.id,
    )
    assert [r.round for r in rows] == [1, 2]
    assert rows[0].context["round_marker"] == 1


@pytest.mark.asyncio
async def test_upsert_round_context_overwrites_on_duplicate(
    db_session: AsyncSession, owner: User,
):
    """A second write for the same (discussion_id, round) overwrites
    instead of failing — defensive against a stuck-RUNNING auto-
    recover that re-fires run_round on the same round number."""
    from models.discussion_round_context import DiscussionRoundContext

    row = await discussion_service.create_discussion(
        db_session,
        owner_id=owner.id,
        topic="x", rules="y",
        persona_ids=["buffett", "lynch"],
    )

    await discussion_service._upsert_round_context(
        db_session,
        discussion_id=row.id, round_number=1,
        context={"v": 1},
    )
    await discussion_service._upsert_round_context(
        db_session,
        discussion_id=row.id, round_number=1,
        context={"v": 2},
    )

    snaps = (await db_session.scalars(
        select(DiscussionRoundContext)
        .where(DiscussionRoundContext.discussion_id == row.id)
    )).all()
    assert len(snaps) == 1
    assert snaps[0].context == {"v": 2}


# ── persona timeout (C8) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_round_persists_timeout_placeholder_and_continues(
    db_session: AsyncSession, owner: User,
):
    """If a persona's stream takes longer than the configured timeout, we
    persist a placeholder turn (stance=DEFAULT_STANCE, content mentions
    timeout) and proceed to the next persona without aborting the round.

    Simulates the timeout path by having the first persona's stream raise
    TimeoutError directly — equivalent to what `asyncio.timeout()` would
    raise but without abandoning a sleeping generator that could leave
    the shared SQLite connection in a half-cancelled state.
    """
    row = await discussion_service.create_discussion(
        db_session,
        owner_id=owner.id,
        topic="topic",
        rules="rules",
        persona_ids=["buffett", "lynch"],
    )

    call_count = {"n": 0}

    async def _timeout_then_ok(*_a, **_kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise TimeoutError("simulated timeout")
        yield {"type": "delta", "text": '{"stance":"agree","content":"ok"}'}

    with patch(
        "services.discussion_service.stream_chat", side_effect=_timeout_then_ok,
    ), patch(
        "services.discussion_service.gather_market_context",
        new=AsyncMock(return_value={"market": "TW"}),
    ):
        events = []
        async for ev in discussion_service.run_round(
            db_session, row, user_id=str(owner.id),
        ):
            events.append((ev.type, ev.payload))

    types = [t for t, _ in events]
    assert types.count("turn_end") == 2
    assert any(t == "error" for t in types)

    turns = (await db_session.scalars(
        select(DiscussionTurn).where(DiscussionTurn.discussion_id == row.id)
        .order_by(DiscussionTurn.turn_index)
    )).all()
    assert len(turns) == 2
    assert (
        "未回覆" in turns[0].content
        or "中止" in turns[0].content
        or "錯誤" in turns[0].content
    )


# ── status reset guarantee (#1 critical fix) ─────────────────────


@pytest.mark.asyncio
async def test_force_reset_status_unsticks_running_discussion(
    db_session: AsyncSession, owner: User,
):
    """A discussion stuck in RUNNING (e.g. previous round died before
    its finally-block reset) must be reset to DRAFT atomically so the
    user can run another round. Pins the bug class where the in-memory
    `discussion.status = STATUS_DRAFT` reset silently failed to commit
    and left the row permanently in RUNNING."""
    row = await discussion_service.create_discussion(
        db_session,
        owner_id=owner.id,
        topic="topic",
        rules="rules",
        persona_ids=["buffett", "lynch"],
    )
    row.status = discussion_service.STATUS_RUNNING
    await db_session.commit()

    await discussion_service.force_reset_status(db_session, row)

    refreshed = await db_session.get(Discussion, row.id)
    await db_session.refresh(refreshed)
    assert refreshed.status == discussion_service.STATUS_DRAFT
    # In-memory entity is also synced so subsequent reads see the new state.
    assert row.status == discussion_service.STATUS_DRAFT


@pytest.mark.asyncio
async def test_run_round_resets_status_to_draft_on_unexpected_exception(
    db_session: AsyncSession, owner: User,
):
    """If the round body raises before completing all turns (e.g. a DB
    commit blew up while persisting a turn), the discussion's status
    must still be reset to DRAFT in the finally block — otherwise the
    user is locked out of starting another round.
    """
    row = await discussion_service.create_discussion(
        db_session,
        owner_id=owner.id,
        topic="topic",
        rules="rules",
        persona_ids=["buffett", "lynch"],
    )

    async def _crash_on_context(*_a, **_kw):
        raise RuntimeError("simulated crash before any persona ran")

    # Patch gather_market_context to raise — the finally block should
    # still reset status to DRAFT even though no turns persisted.
    with patch(
        "services.discussion_service.gather_market_context",
        side_effect=_crash_on_context,
    ):
        with pytest.raises(RuntimeError):
            async for _ in discussion_service.run_round(
                db_session, row, user_id=str(owner.id),
            ):
                pass

    # Refresh + assert: status is DRAFT, not stuck on RUNNING.
    refreshed = await db_session.get(Discussion, row.id)
    assert refreshed.status == discussion_service.STATUS_DRAFT


# ── batch persona override loading (#4 major fix) ────────────────


@pytest.mark.asyncio
async def test_resolve_persona_specs_returns_compiled_defaults_without_overrides(
    db_session: AsyncSession,
):
    """Bare metadata (no PersonaOverride rows) → every persona resolves
    to its compiled-in default."""
    specs = await discussion_service._resolve_persona_specs(
        db_session, ["buffett", "lynch"],
    )
    from ai.agents import get_agent
    assert specs["buffett"].default_provider == get_agent("buffett").default_provider
    assert specs["lynch"].default_provider == get_agent("lynch").default_provider


@pytest.mark.asyncio
async def test_resolve_persona_specs_applies_overrides(
    db_session: AsyncSession, owner: User,
):
    from models.persona_override import PersonaOverride
    db_session.add(PersonaOverride(
        persona_id="buffett", provider="openai", model="gpt-4o-mini",
        updated_by_id=owner.id,
    ))
    await db_session.commit()

    specs = await discussion_service._resolve_persona_specs(
        db_session, ["buffett", "lynch"],
    )
    assert specs["buffett"].default_provider == "openai"
    assert specs["buffett"].default_model == "gpt-4o-mini"
    # name/description/system_prompt come from compiled spec
    from ai.agents import get_agent
    assert specs["buffett"].system_prompt == get_agent("buffett").system_prompt


@pytest.mark.asyncio
async def test_resolve_persona_specs_skips_unknown_persona(
    db_session: AsyncSession,
):
    specs = await discussion_service._resolve_persona_specs(
        db_session, ["buffett", "_not_a_persona_"],
    )
    assert "buffett" in specs
    assert "_not_a_persona_" not in specs


# ── connector errors surface in context (#7 minor fix) ───────────


@pytest.mark.asyncio
async def test_gather_market_context_records_connector_errors(
    db_session: AsyncSession,
):
    """Broken connectors must populate `context.errors` so the personas
    can mention "data was incomplete" instead of confidently citing
    missing fields."""
    import services.tw_market_service as _tw   # noqa: F401

    async def _broken_screener(*_a, **_kw):
        raise RuntimeError("polygon down")

    with patch.object(_tw, "get_screener", side_effect=_broken_screener), \
         patch.object(_tw, "get_index", new=AsyncMock(return_value={})):
        ctx = await discussion_service.gather_market_context(db_session)

    assert "errors" in ctx
    sources = [e["source"] for e in ctx["errors"]]
    assert "screener" in sources


@pytest.mark.asyncio
async def test_run_round_records_per_persona_usage(
    db_session: AsyncSession, owner: User,
):
    """Each persona's LLM call must produce an `LLMUsageEvent` row tagged
    with that persona's ID — without this, the bulk of discussion cost
    (N personas × rounds) was invisible in the admin UsageCard.

    Stream emits a `usage` event after deltas, mimicking what
    `ai/llm_router._anthropic_stream` / `_openai_stream` actually
    yield in production.
    """
    from models.llm_usage_event import LLMUsageEvent

    row = await discussion_service.create_discussion(
        db_session,
        owner_id=owner.id,
        topic="topic",
        rules="rules",
        persona_ids=["buffett", "lynch"],
    )

    call_n = {"i": 0}

    async def _stream_with_usage(*_a, **_kw):
        call_n["i"] += 1
        # Different per-persona token counts so we can verify each
        # one was independently recorded (not a single summed event).
        prompt = 100 * call_n["i"]
        completion = 30 * call_n["i"]
        yield {"type": "delta",
               "text": f'{{"stance":"agree","content":"persona {call_n["i"]}"}}'}
        yield {"type": "usage",
               "prompt_tokens": prompt, "completion_tokens": completion}

    with patch(
        "services.discussion_service.stream_chat", side_effect=_stream_with_usage,
    ), patch(
        "services.discussion_service.gather_market_context",
        new=AsyncMock(return_value={"market": "TW"}),
    ):
        async for _ in discussion_service.run_round(
            db_session, row, user_id=str(owner.id),
        ):
            pass

    rows = (await db_session.scalars(
        select(LLMUsageEvent)
        .where(LLMUsageEvent.persona_id.in_(["buffett", "lynch"]))
        .order_by(LLMUsageEvent.created_at)
    )).all()
    by_persona = {r.persona_id: r for r in rows}
    assert "buffett" in by_persona
    assert "lynch" in by_persona
    # First call (buffett): 100 / 30. Second call (lynch): 200 / 60.
    assert by_persona["buffett"].prompt_tokens == 100
    assert by_persona["buffett"].completion_tokens == 30
    assert by_persona["lynch"].prompt_tokens == 200
    assert by_persona["lynch"].completion_tokens == 60


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


def test_safe_conclusion_handles_line_comments_copied_from_prompt():
    """Synthesizer prompt template included `// 最多5檔` style range
    hints — some LLMs (gemini, qwen) copy these verbatim into their
    response. Strict JSON dies; the relaxed loader strips them.
    """
    raw = """{
        "recommended_symbols": ["2330", "0050"],   // 最多5檔
        "reasoning": "台積電與大盤型 ETF 風險分散",
        "risks": ["匯率波動"],
        "time_horizon": "medium_term",
        "consensus_score": 0.6   // 0=完全分歧 1=完全共識
    }"""
    out = discussion_service._safe_conclusion(raw)
    assert "_parse_error" not in out
    assert out["recommended_symbols"] == ["2330", "0050"]
    assert out["time_horizon"] == "medium_term"
    assert out["consensus_score"] == 0.6


def test_safe_conclusion_handles_trailing_commas():
    raw = (
        '{"recommended_symbols": ["2330",], '
        '"reasoning": "x", "risks": ["A",], '
        '"time_horizon": "short_term", "consensus_score": 0.5,}'
    )
    out = discussion_service._safe_conclusion(raw)
    assert "_parse_error" not in out
    assert out["recommended_symbols"] == ["2330"]


def test_safe_conclusion_handles_block_comments():
    raw = """{
        /* synthesizer notes */
        "recommended_symbols": ["2330"],
        "reasoning": "y",
        "risks": [],
        "time_horizon": "long_term",
        "consensus_score": 0.8
    }"""
    out = discussion_service._safe_conclusion(raw)
    assert "_parse_error" not in out
    assert out["time_horizon"] == "long_term"


def test_safe_conclusion_preserves_double_slash_inside_string_values():
    """The // stripper must respect string boundaries — a URL or
    literal `//` inside a `reasoning` field should survive untouched."""
    raw = (
        '{"recommended_symbols": ["2330"], '
        '"reasoning": "see https://example.com/research // for details", '
        '"risks": [], "time_horizon": "short_term", "consensus_score": 0.5}'
    )
    out = discussion_service._safe_conclusion(raw)
    assert "_parse_error" not in out
    assert "https://example.com" in out["reasoning"]
    assert "//" in out["reasoning"]


def test_safe_conclusion_handles_full_width_braces():
    """Some Chinese-language LLMs occasionally Sinicize the braces too."""
    raw = (
        '｛"recommended_symbols": ["2330"]，'
        '"reasoning": "ok"，"risks": []，'
        '"time_horizon": "short_term"，"consensus_score": 0.5｝'
    )
    out = discussion_service._safe_conclusion(raw)
    assert "_parse_error" not in out
    assert out["recommended_symbols"] == ["2330"]


# ── focus_briefs / macro helpers ──────────────────────────────────


def _bars(closes: list[float]) -> list[dict]:
    """Build a synthetic bar series from a list of closes — open/high/low
    are filled with the close so the technicals helpers can do their
    52w / MA / RSI work without spurious volatility."""
    return [
        {"time": f"2026-01-{i + 1:02d}", "open": c, "high": c,
         "low": c, "close": float(c), "volume": 1_000}
        for i, c in enumerate(closes)
    ]


def test_compute_technicals_returns_none_for_short_history():
    """`<20` bars → None so the persona doesn't see a misleading 20-bar
    MA computed from 8 days."""
    assert discussion_service._compute_technicals(_bars([100.0] * 5)) is None


def test_compute_technicals_emits_full_block_for_long_history():
    closes = [100.0 + i for i in range(120)]
    out = discussion_service._compute_technicals(_bars(closes))
    assert out is not None
    assert out["last_close"] == 219.0
    assert out["high_52w"] == 219.0
    assert out["low_52w"] == 100.0
    # Distance to 52w high is 0 because the last bar IS the high.
    assert out["dist_high_52w_pct"] == 0.0
    # 60-day perf: closes[-61] is 159, last 219 → +37.74%
    assert out["perf_60d_pct"] == 37.74
    # MA20 of last 20 closes (200..219) = 209.5
    assert out["ma20"] == 209.5


def test_rsi_returns_50_for_flat_series():
    """All-flat series → 0/0 case must short-circuit to 50, not crash."""
    assert discussion_service._rsi([100.0] * 30) == 50.0


def test_rsi_caps_at_100_for_strict_uptrend():
    closes = [100.0 + i for i in range(30)]
    assert discussion_service._rsi(closes, window=14) == 100.0


def test_summarize_revenue_takes_last_six_only():
    rows = [{"date": f"2025-{m:02d}-01", "revenue_yoy": m, "revenue_mom": m}
            for m in range(1, 13)]
    out = discussion_service._summarize_revenue(rows)
    assert len(out) == 6
    # Tail order preserved (oldest of the 6 first, newest last).
    assert out[0]["month"] == "2025-07"
    assert out[-1]["month"] == "2025-12"


def test_summarize_institutional_sums_recent_window():
    rows = [
        {"fini_buy": 200, "fini_sell": 50,
         "sitc_buy": 30, "sitc_sell": 20,
         "dealer_buy": 10, "dealer_sell": 15}
    ] * 3
    out = discussion_service._summarize_institutional(rows)
    assert out is not None
    assert out["fini_net_5d"] == (200 - 50) * 3
    assert out["sitc_net_5d"] == (30 - 20) * 3
    assert out["dealer_net_5d"] == (10 - 15) * 3
    assert out["days"] == 3


def test_summarize_institutional_returns_none_for_empty():
    assert discussion_service._summarize_institutional([]) is None


def test_summarize_margin_picks_latest():
    rows = [
        {"date": "2026-04-28", "margin_balance": 100, "short_balance": 5},
        {"date": "2026-04-29", "margin_balance": 110, "short_balance": 6},
        {"date": "2026-04-30", "margin_balance": 120, "short_balance": 7},
    ]
    out = discussion_service._summarize_margin(rows)
    assert out["as_of"] == "2026-04-30"
    assert out["margin_balance"] == 120


def test_macro_summary_computes_deltas_against_anchored_dates():
    series = [
        {"date": "2025-01-30", "value": 4.50},   # ~15m ago — used for change_1y
        {"date": "2025-04-30", "value": 4.75},   # close to 1y mark, but later than cutoff
        {"date": "2026-01-30", "value": 5.00},   # 3m ago anchor
        {"date": "2026-04-30", "value": 4.25},   # latest
    ]
    out = discussion_service._macro_summary_from_series(series)
    assert out is not None
    assert out["latest_value"] == 4.25
    # 365 days before 2026-04-30 = 2025-04-30 → matches that point.
    assert out["change_1y"] == round(4.25 - 4.75, 4)
    # 90 days before 2026-04-30 = 2026-01-30 → matches that point.
    assert out["change_3m"] == round(4.25 - 5.00, 4)


def test_macro_summary_returns_none_for_empty_or_invalid():
    assert discussion_service._macro_summary_from_series([]) is None
    # Garbage rows filter out and the series degrades to None.
    bad = [{"date": "??", "value": "n/a"}]
    assert discussion_service._macro_summary_from_series(bad) is None


@pytest.mark.asyncio
async def test_assemble_macro_block_degrades_on_missing_series():
    """No FRED key + no yfinance fallback → every series is empty.
    Must not crash, must return one entry per series with `summary=None`."""
    with patch(
        "services.us_market_service.get_macro_indicator",
        new=AsyncMock(return_value=[]),
    ):
        block = await discussion_service._assemble_macro_block()
    assert set(block.keys()) == {n for n, _ in discussion_service._MACRO_SERIES}
    assert all(v["summary"] is None for v in block.values())


@pytest.mark.asyncio
async def test_assemble_focus_briefs_returns_empty_when_no_symbols():
    """Caller passes `[]` → assembler short-circuits without touching
    any market service."""
    out = await discussion_service._assemble_focus_briefs(
        db=None, market="TW", symbols=[],
    )
    assert out == []


# ── _build_persona_tool_kwargs ────────────────────────────────────


def test_build_persona_tool_kwargs_off_for_viewer_role():
    """Viewers must not get tools. The MCP toolset's `query_user_data`
    reads the caller's portfolio — handing it to a low-quota viewer
    would let them silently exfiltrate via tool-call deltas."""
    out = discussion_service._build_persona_tool_kwargs(
        provider="claude_agent",
        user_role="viewer",
        user_id="abc",
    )
    assert out == {}


def test_build_persona_tool_kwargs_off_when_user_id_missing():
    """No user_id (e.g. auto-run cron with no role injected) → tools
    off so unattended runs don't fan out N tool calls per persona."""
    out = discussion_service._build_persona_tool_kwargs(
        provider="claude_agent",
        user_role="analyst",
        user_id=None,
    )
    assert out == {}


def test_build_persona_tool_kwargs_off_for_unsupported_provider():
    """openai / anthropic / gemini / ollama don't have a tool-loop in
    this codebase — return {} so stream_chat falls through to plain
    streaming."""
    for prov in ("openai", "anthropic", "gemini", "ollama", ""):
        out = discussion_service._build_persona_tool_kwargs(
            provider=prov, user_role="analyst", user_id="u1",
        )
        assert out == {}, f"expected no tools for provider={prov!r}"


class _SettingsStub:
    """Replaces `services.discussion_service.settings` for tests that
    need to flip `claude_agent_effective_enabled` (the real one is a
    Pydantic computed property and isn't writable).

    Attributes are filled in by the test as needed; missing attrs
    fall through to the real settings object via `__getattr__`."""

    def __init__(self, **overrides):
        self._overrides = overrides

    def __getattr__(self, name):
        if name in self._overrides:
            return self._overrides[name]
        from config import settings as _real
        return getattr(_real, name)


def test_build_persona_tool_kwargs_claude_agent_off_when_disabled(monkeypatch):
    """When `Settings.claude_agent_effective_enabled` is False (SDK
    not importable, or env flag off) the helper must short-circuit
    instead of throwing during build_toolset import."""
    monkeypatch.setattr(
        discussion_service, "settings",
        _SettingsStub(claude_agent_effective_enabled=False),
    )
    out = discussion_service._build_persona_tool_kwargs(
        provider="claude_agent",
        user_role="analyst",
        user_id="u1",
    )
    assert out == {}


def test_build_persona_tool_kwargs_openai_compat_swallows_build_failure(monkeypatch):
    """A broken `build_openai_compat_toolset` (e.g. provider key
    fetcher hits a transient DB failure) must NOT propagate — the
    persona just gets a tool-less turn this round.

    Uses a sys.modules stub for `ai.tools.openai_compat` so the test
    runs even on environments without `claude_agent_sdk` installed
    (which would otherwise trip on `ai/tools/__init__.py`'s top-
    level SDK import)."""
    import sys
    import types

    def boom(_user_id):
        raise RuntimeError("provider key fetch failed")

    fake_mod = types.ModuleType("ai.tools.openai_compat")
    fake_mod.build_openai_compat_toolset = boom
    monkeypatch.setitem(sys.modules, "ai.tools.openai_compat", fake_mod)

    out = discussion_service._build_persona_tool_kwargs(
        provider="groq",
        user_role="analyst",
        user_id="u1",
    )
    assert out == {}


def test_build_persona_tool_kwargs_openai_compat_returns_kwargs(monkeypatch):
    """Happy path for an OpenAI-compat provider: returns the schema +
    dispatch + max_turns triple. Must NOT include claude_agent-only
    keys (`mcp_server`, `allowed_tools`)."""
    import sys
    import types

    def fake_build(_user_id):
        return ([{"type": "function", "name": "get_quote"}], {"get_quote": object()})

    fake_mod = types.ModuleType("ai.tools.openai_compat")
    fake_mod.build_openai_compat_toolset = fake_build
    monkeypatch.setitem(sys.modules, "ai.tools.openai_compat", fake_mod)

    out = discussion_service._build_persona_tool_kwargs(
        provider="groq",
        user_role="analyst",
        user_id="u1",
    )
    assert "openai_tool_schemas" in out
    assert "openai_tool_dispatch" in out
    assert "max_turns" in out
    assert out["max_turns"] >= 1
    # claude_agent keys must be absent — different code path.
    assert "mcp_server" not in out
    assert "allowed_tools" not in out


# ── _assemble_user_context ────────────────────────────────────────


@pytest.mark.asyncio
async def test_assemble_user_context_empty_when_no_portfolio_or_watchlist(
    db_session: AsyncSession, owner: User,
):
    """Brand-new user with no holdings + no watchlist → all blocks
    empty but the structural keys still present so the prompt
    template doesn't have to handle a None."""
    out = await discussion_service._assemble_user_context(
        db_session, owner_id=owner.id, focus_symbols=["2330"],
    )
    assert out["portfolios"] == []
    assert out["holdings"] == []
    assert out["watchlist_symbols"] == []
    assert out["focus_overlap"] == {"held": [], "watching": []}


@pytest.mark.asyncio
async def test_assemble_user_context_returns_holdings_and_overlap(
    db_session: AsyncSession, owner: User,
):
    """Owner has 2 portfolios with 3 holdings; topic mentions 2330 +
    AAPL. Expect holdings list, portfolio metadata, and the
    focus_overlap.held set picking up 2330 (held) but not AAPL
    (not held)."""
    from models.portfolio import Holding, Market, Portfolio

    p1 = Portfolio(user_id=owner.id, name="Main", currency="TWD")
    p2 = Portfolio(user_id=owner.id, name="USD", currency="USD")
    db_session.add_all([p1, p2])
    await db_session.flush()
    db_session.add_all([
        Holding(portfolio_id=p1.id, symbol="2330", market=Market.TW,
                quantity=1000, avg_cost=850, cost_currency="TWD"),
        Holding(portfolio_id=p1.id, symbol="2454", market=Market.TW,
                quantity=200, avg_cost=1100, cost_currency="TWD"),
        Holding(portfolio_id=p2.id, symbol="MSFT", market=Market.US,
                quantity=50, avg_cost=400, cost_currency="USD"),
    ])
    await db_session.commit()

    out = await discussion_service._assemble_user_context(
        db_session, owner_id=owner.id, focus_symbols=["2330", "AAPL"],
    )
    assert {p["name"] for p in out["portfolios"]} == {"Main", "USD"}
    held_syms = {h["symbol"] for h in out["holdings"]}
    assert held_syms == {"2330", "2454", "MSFT"}
    # Largest absolute cost first: 2330 (1000×850 = 850k) leads.
    assert out["holdings"][0]["symbol"] == "2330"
    assert out["focus_overlap"]["held"] == ["2330"]
    assert out["focus_overlap"]["watching"] == []


@pytest.mark.asyncio
async def test_assemble_user_context_picks_up_watchlist_overlap(
    db_session: AsyncSession, owner: User,
):
    """Symbol in topic that's only in the watchlist (not held) appears
    under focus_overlap.watching instead of held."""
    from models.portfolio import Market
    from models.watchlist import Watchlist, WatchlistItem

    wl = Watchlist(user_id=owner.id, name="觀察清單")
    db_session.add(wl)
    await db_session.flush()
    db_session.add_all([
        WatchlistItem(watchlist_id=wl.id, symbol="2330", market=Market.TW),
        WatchlistItem(watchlist_id=wl.id, symbol="NVDA", market=Market.US),
    ])
    await db_session.commit()

    out = await discussion_service._assemble_user_context(
        db_session, owner_id=owner.id, focus_symbols=["NVDA", "AMD"],
    )
    assert out["focus_overlap"]["held"] == []
    assert out["focus_overlap"]["watching"] == ["NVDA"]
    assert {w["symbol"] for w in out["watchlist_symbols"]} == {"2330", "NVDA"}


@pytest.mark.asyncio
async def test_gather_market_context_skips_user_block_without_owner_id(
    db_session: AsyncSession,
):
    """Tests / API callers that don't supply owner_id must NOT see a
    user_context block populated — privacy invariant."""
    import services.tw_market_service as _tw  # noqa: F401
    with patch.object(_tw, "get_screener", new=AsyncMock(return_value=[])), \
         patch.object(_tw, "get_index", new=AsyncMock(return_value={})), \
         patch(
            "services.discussion_service._assemble_macro_block",
            new=AsyncMock(return_value={}),
         ), \
         patch(
            "services.discussion_service._assemble_focus_briefs",
            new=AsyncMock(return_value=[]),
         ):
        ctx = await discussion_service.gather_market_context(
            db_session, market="TW",
        )
    assert ctx["user_context"] is None


@pytest.mark.asyncio
async def test_gather_market_context_populates_user_block_with_owner_id(
    db_session: AsyncSession, owner: User,
):
    """When owner_id is supplied, gather_market_context wires it
    through to `_assemble_user_context` and surfaces the result on
    `ctx["user_context"]`."""
    import services.tw_market_service as _tw  # noqa: F401

    with patch.object(_tw, "get_screener", new=AsyncMock(return_value=[])), \
         patch.object(_tw, "get_index", new=AsyncMock(return_value={})), \
         patch(
            "services.discussion_service._assemble_macro_block",
            new=AsyncMock(return_value={}),
         ), \
         patch(
            "services.discussion_service._assemble_focus_briefs",
            new=AsyncMock(return_value=[]),
         ):
        ctx = await discussion_service.gather_market_context(
            db_session, market="TW", owner_id=owner.id,
        )
    assert ctx["user_context"] is not None
    assert "portfolios" in ctx["user_context"]


@pytest.mark.asyncio
async def test_assemble_focus_briefs_fan_out_collects_per_symbol():
    """Each symbol gets its own brief built concurrently, returned in
    the input order. A coroutine that raises is logged + skipped, not
    propagated, so a single bad symbol doesn't kill the round."""
    async def fake_brief(_db, sym: str) -> dict:
        if sym == "BAD":
            raise RuntimeError("upstream blew up")
        return {"symbol": sym, "quote": {"price": 100.0}}

    with patch(
        "services.discussion_service._build_tw_focus_brief",
        new=fake_brief,
    ):
        out = await discussion_service._assemble_focus_briefs(
            db=None, market="TW", symbols=["2330", "BAD", "2454"],
        )
    syms = [b["symbol"] for b in out]
    assert "2330" in syms and "2454" in syms and "BAD" not in syms
