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
         patch.object(_tw, "get_index", new=AsyncMock(return_value={})):
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
