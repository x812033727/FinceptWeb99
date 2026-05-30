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


def test_parse_turn_response_strips_think_block_embedded_inside_content_field():
    """C2-3 pin (`misty-mixing-harbor.md`): reasoning models occasionally
    embed `<think>...</think>` INSIDE the JSON `content` string itself,
    not just before the JSON wrapper — typically when the model treats
    its scratch work as part of the persona's answer. The persisted
    `DiscussionTurn.content` must not retain the tag (storage cost +
    "scratch work" leaks into the transcript). Pins that the
    `strip_think_blocks` pass at the top of `_parse_turn_response`
    fires before JSON parse so the content field is already clean by
    the time `data["content"]` is extracted.

    Edge case not previously covered by
    `test_parse_turn_response_strips_think_block_before_json_parse`,
    which only had `<think>` BEFORE the JSON wrapper. The strip-then-
    parse ordering is what makes this case work — flipping to parse-
    then-strip would leave the `<think>` text inside the content
    string until the very end.
    """
    raw = (
        '{"stance": "supplement", '
        '"content": "<think>scratch work to ignore</think>'
        '台積電的護城河在於先進製程的良率優勢。"}'
    )
    stance, content = discussion_service._parse_turn_response(raw)
    assert stance == "supplement"
    assert "<think>" not in content
    assert "scratch work" not in content
    assert content == "台積電的護城河在於先進製程的良率優勢。"


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
async def test_create_discussion_survives_missing_recent_column(
    db_session: AsyncSession, owner: User,
):
    """Regression: create_discussion used to call `db.refresh(row)` with
    no `attribute_names`, which made SQLAlchemy issue a SELECT over the
    full model column list. On any deployment where the DB schema was
    behind the latest migration (e.g. operator hadn't run
    `alembic upgrade head` past 0050 so `post_mortem_diff` was missing)
    the post-INSERT refresh blew up with `UndefinedColumnError` even
    though the INSERT itself only writes the columns we set explicitly
    and succeeds.

    Simulate that schema drift by patching `db.refresh` to raise the
    same kind of error a missing column would. The targeted refresh
    in the fix uses `attribute_names=("created_at", "updated_at")` so
    SQLAlchemy issues a narrow SELECT — but the patched mock still
    asserts that the call site only requests those two attributes,
    locking in the contract that future edits can't broaden.
    """
    captured: list[dict] = []
    real_refresh = db_session.refresh

    async def _capturing_refresh(instance, attribute_names=None):
        captured.append({"attribute_names": attribute_names})
        # Forward the narrow refresh through; if `attribute_names` is
        # None we'd hit the full SELECT and that's the bug we're
        # guarding against — fail loud so the test catches a regression.
        assert attribute_names is not None, (
            "create_discussion must restrict refresh to specific columns "
            "to survive a partial migration state"
        )
        return await real_refresh(instance, attribute_names=attribute_names)

    with patch.object(db_session, "refresh", new=_capturing_refresh):
        row = await discussion_service.create_discussion(
            db_session,
            owner_id=owner.id,
            topic="schema-drift safety",
            rules="rules",
            persona_ids=["buffett", "lynch"],
        )

    assert row.id is not None
    assert row.created_at is not None
    assert row.updated_at is not None
    # Exactly one refresh call, narrow attribute set.
    assert len(captured) == 1
    assert set(captured[0]["attribute_names"]) == {"created_at", "updated_at"}


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


# ── prune_old_round_contexts (PR #217 — snapshot retention) ─────


@pytest.mark.asyncio
async def test_prune_old_round_contexts_deletes_only_older_than_cutoff(
    db_session: AsyncSession, owner: User,
):
    """Helper must delete rows with `captured_at` strictly before the
    cutoff and leave the rest. Discussions + turns are untouched."""
    from datetime import UTC as _UTC, datetime as _dt, timedelta as _td

    from models.discussion_round_context import DiscussionRoundContext

    row = await discussion_service.create_discussion(
        db_session, owner_id=owner.id,
        topic="t", rules="r",
        persona_ids=["buffett", "lynch"],
    )

    # 3 snapshots: 1y old, 30d old, today.
    now = _dt.now(_UTC)
    db_session.add_all([
        DiscussionRoundContext(
            discussion_id=row.id, round=1,
            context={"marker": "ancient"},
            captured_at=now - _td(days=400),
        ),
        DiscussionRoundContext(
            discussion_id=row.id, round=2,
            context={"marker": "fresh"},
            captured_at=now - _td(days=30),
        ),
        DiscussionRoundContext(
            discussion_id=row.id, round=3,
            context={"marker": "today"},
            captured_at=now,
        ),
    ])
    await db_session.commit()

    deleted = await discussion_service.prune_old_round_contexts(
        db_session, older_than_days=90,
    )
    assert deleted == 1   # only the 400d-old row crossed the cutoff

    # Survivors: round 2 (30d) + round 3 (today).
    remaining = list((await db_session.scalars(
        select(DiscussionRoundContext)
        .where(DiscussionRoundContext.discussion_id == row.id)
    )).all())
    markers = {r.context["marker"] for r in remaining}
    assert markers == {"fresh", "today"}

    # Parent discussion untouched.
    parent = await db_session.get(Discussion, row.id)
    assert parent is not None


@pytest.mark.asyncio
async def test_prune_old_round_contexts_returns_zero_when_nothing_old(
    db_session: AsyncSession, owner: User,
):
    """Empty table or no rows past the cutoff → zero deletes, no
    crash, idempotent."""
    deleted = await discussion_service.prune_old_round_contexts(
        db_session, older_than_days=90,
    )
    assert deleted == 0


# ── _persona_schema_annotation (dynamic prompt header) ───────────


def test_turn_prompt_template_carries_signal_citation_guidance():
    """The 訊號引用準則 section is the structural countermeasure to the
    'persona ignores the JSON, talks generic narrative' failure mode.
    Three rules must be present:
      1. 引用具體數值 not 一般性形容詞
      2. 每個與本主題相關的訊號區塊都要至少點名一次
      3. 訊號間互相印證或衝突要明寫
    Any drift in the template (re-flow, translation, accidental delete)
    that drops one of these will silently regress citation rate.
    """
    tpl = discussion_service._TURN_PROMPT_TEMPLATE
    assert "## 訊號引用準則" in tpl
    # Rule 1 — concrete number requirement
    assert "引用具體數值" in tpl
    assert "RSI 67.3" in tpl, (
        "Rule 1 example must show a numeric citation pattern personas "
        "can mimic; without it weak models produce 'RSI 偏高' even when "
        "the rule is stated abstractly."
    )
    # Rule 2 — coverage requirement
    assert "都要至少點名一次" in tpl
    # Rule 3 — cross-signal reconciliation
    assert "互相印證或衝突要明寫" in tpl


def test_turn_prompt_template_renders_with_standard_substitutions():
    """All six placeholders (`{topic}/{rules}/{annotation}/
    {freshness_preamble}/{context}/{history}`) must remain — adding
    sections to the template mustn't accidentally consume one of them
    via brace mismatch. `freshness_preamble` landed as part of the
    expert-quote-freshness phase so personas can never confuse
    captured_at (wall-clock NOW) with the trading session whose
    numbers are actually in ctx.
    """
    rendered = discussion_service._TURN_PROMPT_TEMPLATE.format(
        topic="2330 短線分析",
        rules="字數 ≤ 500",
        annotation="## annotation",
        freshness_preamble="- 基準交易日：2026-05-27",
        context='{"k":1}',
        history="（首輪）",
    )
    assert "2330 短線分析" in rendered
    assert "字數 ≤ 500" in rendered
    assert "## annotation" in rendered
    assert "2026-05-27" in rendered
    assert "## 資料時效" in rendered
    assert '{"k":1}' in rendered
    assert "（首輪）" in rendered
    # The literal JSON example in the task instruction must survive
    # str.format() — its `{{...}}` escaping is what keeps `format`
    # from interpreting it as a placeholder.
    assert '"stance": "agree|dissent|supplement"' in rendered


def test_persona_schema_annotation_lists_only_present_blocks():
    """The annotation must mention only blocks actually present in
    `ctx`. macro_analyst's filtered ctx drops focus_briefs +
    risk_warnings — the prompt mustn't advertise them, otherwise
    the LLM hallucinates `## 市場現況` keys it can't find."""
    ctx = {
        "market":          "TW",
        "captured_at":     "t",
        "errors":          [],
        "index":           {"price": 1},
        "macro":           {"fed_funds_rate": {}},
        "news_sentiment":  {"avg_score": 0.1},
    }
    out = discussion_service._persona_schema_annotation(ctx)
    assert "- index" in out
    assert "- macro" in out
    assert "- news_sentiment" in out
    # Blocks not in ctx must NOT appear in the bullets.
    assert "- focus_briefs" not in out
    assert "- risk_warnings" not in out
    assert "- top_gainers" not in out
    assert "- user_context" not in out


def test_persona_schema_annotation_skips_empty_blocks():
    """An empty list / {} / None for a block (block is in ctx but
    nothing was populated) must NOT show in the annotation —
    advertising "top_foreign_buyers" when its value is `[]` makes
    the LLM apologise about empty data unnecessarily."""
    ctx = {
        "market":             "TW",
        "captured_at":        "t",
        "errors":             [],
        "top_gainers":        [],
        "top_losers":         [],
        "index":              None,
        "focus_briefs":       [],
        "macro":              None,
        "user_context":       None,
        "prior_discussions": [],
        "risk_warnings": {
            "active_dispositions": [],
            "recent_suspensions": [],
            "high_day_trading_ratio": [],
        },
    }
    out = discussion_service._persona_schema_annotation(ctx)
    # Only `errors` (always-on) should appear in the bullets.
    assert "- errors" in out
    assert "- index" not in out
    assert "- focus_briefs" not in out
    assert "- macro" not in out
    # risk_warnings dict is non-empty (has keys), so it DOES show.
    # The "block not empty" semantics are: dict with keys counts as
    # populated; empty dict {} doesn't.
    assert "- risk_warnings" in out


def test_persona_schema_annotation_always_keeps_errors_bullet():
    """`errors` is always populated (initialised to []) on a clean
    run, but its bullet must still appear so personas know what
    `errors: []` means without us having to special-case it in
    every persona's prompt."""
    out = discussion_service._persona_schema_annotation(
        {"market": "TW", "captured_at": "t", "errors": []},
    )
    assert "- errors" in out


def test_format_freshness_preamble_surfaces_session_date_and_hint():
    """The freshness preamble is what stops personas confusing
    `captured_at` (wall-clock NOW) with the actual trading session
    the numbers came from. It must literally show both the
    session_date and the human hint so the LLM can't miss them.
    """
    ctx = {
        "captured_session": {
            "session_date": "2026-05-26",
            "phase": "intraday",
            "is_intraday": False,
            "hint_zh": "TWSE 盤中 (2026-05-27)。多數區塊報價仍反映 2026-05-26 收盤。",
        },
    }
    out = discussion_service._format_freshness_preamble(ctx)
    assert "2026-05-26" in out
    assert "intraday" in out
    assert "2026-05-27" in out  # from the hint
    assert "TWSE" in out


def test_format_freshness_preamble_falls_back_on_legacy_ctx():
    """Old round_context snapshots written before this PR landed
    don't carry `captured_session`. The preamble must NOT raise; it
    emits a generic notice so the prompt template still renders
    and personas drop back to per-block `as_of_session` reads.
    """
    out = discussion_service._format_freshness_preamble({})
    assert "captured_session" in out
    assert "as_of_session" in out


def test_persona_schema_annotation_preserves_block_order():
    """Output order follows `_BLOCK_ANNOTATIONS` insertion order,
    not dict-insertion order on the input ctx — the LLM benefits
    from a stable prompt structure across rounds."""
    # Pass blocks in scrambled order; output should still be macro
    # before user_context before prior_discussions per the registry.
    ctx = {
        "market":            "TW",
        "captured_at":       "t",
        "errors":            [],
        "prior_discussions": [{"id": "a"}],
        "user_context":      {"portfolios": [{"name": "Main"}]},
        "macro":             {"fed_funds_rate": {}},
    }
    out = discussion_service._persona_schema_annotation(ctx)
    pos_macro = out.index("- macro")
    pos_user = out.index("- user_context")
    pos_prior = out.index("- prior_discussions")
    assert pos_macro < pos_user < pos_prior


# ── per-persona context filtering ─────────────────────────────────


def _full_ctx_for_filter():
    """Synthetic ctx covering every block the filter knows about."""
    return {
        "market":                    "TW",
        "captured_at":               "2026-05-01T00:00:00+00:00",
        "errors":                    [],
        "top_gainers":               ["g"],
        "top_losers":                ["l"],
        "index":                     {"price": 1},
        "news_sentiment":            {"avg_score": 0},
        "per_symbol_news_sentiment": {"2330": {}},
        "international_sentiment":   {"avg_score": 0},
        "top_foreign_buyers":        [],
        "margin_balance_trend":      {},
        "top_revenue_growers":       [],
        "active_buybacks":           [],
        "govt_bank_flow_5d":         [],
        "risk_warnings":             {},
        "market_institutional_5d":   [],
        "focus_briefs":              [],
        "macro":                     {},
        "user_context":              None,
    }


def test_filter_context_unknown_persona_returns_full_ctx():
    """Personas not in the registry must NOT silently lose data — fail
    open so a typo / new persona doesn't hide blocks the LLM expects."""
    ctx = _full_ctx_for_filter()
    out = discussion_service._filter_context_for_persona(ctx, "unknown_persona")
    assert set(out.keys()) == set(ctx.keys())


def test_filter_context_macro_analyst_drops_chip_metrics():
    """macro_analyst's view excludes TW chip-flow + risk_warnings —
    those would mislead a Fed-policy thesis. PR #219 adds
    `focus_briefs` to the macro profile so symbolic macro topics
    ("Fed 降息 → 2330 受惠") can ground in real data."""
    ctx = _full_ctx_for_filter()
    out = discussion_service._filter_context_for_persona(ctx, "macro_analyst")
    assert "macro" in out
    assert "international_sentiment" in out
    assert "top_foreign_buyers" in out
    # focus_briefs IS now in scope (PR #219); empty list means
    # nothing renders, but the schema bullet is available when
    # populated.
    assert "focus_briefs" in out
    # Negative space — chip / risk blocks dropped.
    assert "risk_warnings" not in out
    assert "active_buybacks" not in out


def test_filter_context_dalio_now_sees_focus_briefs():
    """PR #219: dalio (macro archetype) used to lose focus_briefs;
    now gets them so a Fed-rates thesis can cite specific names'
    valuation bands instead of staying purely top-down."""
    ctx = _full_ctx_for_filter()
    out = discussion_service._filter_context_for_persona(ctx, "dalio")
    assert "focus_briefs" in out
    assert "macro" in out
    # Macro-specific blocks still present.
    assert "international_sentiment" in out
    assert "govt_bank_flow_5d" in out
    # Quant / chip-detail blocks still dropped.
    assert "top_gainers" not in out
    assert "risk_warnings" not in out


def test_filter_context_buffett_keeps_value_blocks_drops_quant_breadth():
    ctx = _full_ctx_for_filter()
    out = discussion_service._filter_context_for_persona(ctx, "buffett")
    assert "focus_briefs" in out
    assert "active_buybacks" in out
    assert "top_revenue_growers" in out
    # Quant breadth blocks dropped.
    assert "top_gainers" not in out
    assert "market_institutional_5d" not in out


def test_filter_context_portfolio_advisor_keeps_user_context():
    """portfolio_advisor specifically needs the owner's holdings to
    give portfolio-fit advice."""
    ctx = _full_ctx_for_filter()
    out = discussion_service._filter_context_for_persona(ctx, "portfolio_advisor")
    assert "user_context" in out
    assert "focus_briefs" in out


def test_filter_context_claude_research_sees_everything():
    """The tool-using persona gets the full picture so it can decide
    which tool to call without re-fetching context blocks."""
    ctx = _full_ctx_for_filter()
    out = discussion_service._filter_context_for_persona(ctx, "claude_research")
    assert set(out.keys()) == set(ctx.keys())


def test_filter_context_always_includes_metadata_keys():
    """`market`, `captured_at`, `errors` must appear in every persona
    view — the prompt template references them unconditionally."""
    ctx = _full_ctx_for_filter()
    for pid in ("macro_analyst", "buffett", "simons", "marks"):
        out = discussion_service._filter_context_for_persona(ctx, pid)
        assert "market" in out
        assert "captured_at" in out
        assert "errors" in out


# ── _assemble_prior_discussions ───────────────────────────────────


async def _make_done_discussion(
    db: AsyncSession,
    *,
    owner_id,
    topic: str,
    recommended: list[str],
    market: str = "TW",
    horizon: str = "short_term",
    consensus: float = 0.6,
    verdict: str | None = None,
    days_old: int = 0,
) -> Discussion:
    """Build a concluded discussion fixture for prior-discussion tests.
    Bypasses run_round / synthesize_conclusion and writes the
    Discussion row directly with a hand-crafted conclusion."""
    from datetime import UTC as _UTC, datetime as _dt, timedelta as _td

    row = await discussion_service.create_discussion(
        db,
        owner_id=owner_id,
        topic=topic,
        rules="r",
        persona_ids=["buffett", "lynch"],
        market=market,
    )
    row.status = discussion_service.STATUS_DONE
    row.conclusion = {
        "recommended_symbols": list(recommended),
        "reasoning": "ok",
        "risks": [],
        "time_horizon": horizon,
        "consensus_score": consensus,
    }
    if verdict is not None:
        row.verdict = verdict
    if days_old > 0:
        row.created_at = _dt.now(_UTC) - _td(days=days_old)
    await db.commit()
    await db.refresh(row)
    return row


@pytest.mark.asyncio
async def test_prior_discussions_empty_when_no_focus_symbols(
    db_session: AsyncSession, owner: User,
):
    """No focus_symbols → no point looking up overlap. Skip the
    query entirely; returns []."""
    out = await discussion_service._assemble_prior_discussions(
        db_session, owner_id=owner.id, focus_symbols=None,
    )
    assert out == []
    out = await discussion_service._assemble_prior_discussions(
        db_session, owner_id=owner.id, focus_symbols=[],
    )
    assert out == []


@pytest.mark.asyncio
async def test_prior_discussions_matches_topic_and_recommended_symbols(
    db_session: AsyncSession, owner: User,
):
    """Two prior discussions: one with the symbol in the topic, the
    other with it only in `conclusion.recommended_symbols`. Both
    should be returned."""
    a = await _make_done_discussion(
        db_session, owner_id=owner.id,
        topic="本週短線 2330 走勢", recommended=["2454"],
    )
    b = await _make_done_discussion(
        db_session, owner_id=owner.id,
        topic="台股大盤觀察", recommended=["2330"],
    )
    out = await discussion_service._assemble_prior_discussions(
        db_session, owner_id=owner.id, focus_symbols=["2330"],
    )
    ids = {row["id"] for row in out}
    assert str(a.id) in ids
    assert str(b.id) in ids
    # `matched_symbols` reflects which focus symbols overlapped.
    for row in out:
        assert "2330" in row["matched_symbols"]


@pytest.mark.asyncio
async def test_prior_discussions_excludes_self(
    db_session: AsyncSession, owner: User,
):
    """Re-running a round on the SAME discussion must not pull
    that discussion's own conclusion in as 'past memory'."""
    self_row = await _make_done_discussion(
        db_session, owner_id=owner.id,
        topic="2330", recommended=["2330"],
    )
    other = await _make_done_discussion(
        db_session, owner_id=owner.id,
        topic="2330 again", recommended=["2330"],
    )
    out = await discussion_service._assemble_prior_discussions(
        db_session, owner_id=owner.id,
        focus_symbols=["2330"], exclude_id=self_row.id,
    )
    ids = {row["id"] for row in out}
    assert str(self_row.id) not in ids
    assert str(other.id) in ids


@pytest.mark.asyncio
async def test_prior_discussions_owner_scoped(
    db_session: AsyncSession, owner: User,
):
    """Discussions from another user must not leak into this user's
    prior list — privacy boundary."""
    other_user = User(
        email=f"other-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x",
    )
    db_session.add(other_user)
    await db_session.commit()
    await db_session.refresh(other_user)
    await _make_done_discussion(
        db_session, owner_id=other_user.id,
        topic="2330 leak", recommended=["2330"],
    )
    out = await discussion_service._assemble_prior_discussions(
        db_session, owner_id=owner.id, focus_symbols=["2330"],
    )
    assert out == []


@pytest.mark.asyncio
async def test_prior_discussions_skips_drafts_and_unconcluded(
    db_session: AsyncSession, owner: User,
):
    """Only `status=done` rows with a non-null conclusion count.
    A draft / running discussion has no committed view to cite."""
    draft_row = await discussion_service.create_discussion(
        db_session, owner_id=owner.id,
        topic="2330 still drafting",
        rules="r", persona_ids=["buffett", "lynch"],
    )
    # Leave status=draft, conclusion=null.
    out = await discussion_service._assemble_prior_discussions(
        db_session, owner_id=owner.id, focus_symbols=["2330"],
    )
    assert all(row["id"] != str(draft_row.id) for row in out)


@pytest.mark.asyncio
async def test_prior_discussions_drops_old_rows_outside_lookback(
    db_session: AsyncSession, owner: User,
):
    """A 6-month-old discussion shouldn't bloat the prompt of a
    fresh discussion — `_PRIOR_DISCUSSIONS_LOOKBACK_DAYS` keeps
    only the recent window."""
    fresh = await _make_done_discussion(
        db_session, owner_id=owner.id,
        topic="2330 fresh", recommended=["2330"], days_old=10,
    )
    stale = await _make_done_discussion(
        db_session, owner_id=owner.id,
        topic="2330 stale", recommended=["2330"], days_old=200,
    )
    out = await discussion_service._assemble_prior_discussions(
        db_session, owner_id=owner.id, focus_symbols=["2330"],
    )
    ids = {row["id"] for row in out}
    assert str(fresh.id) in ids
    assert str(stale.id) not in ids


@pytest.mark.asyncio
async def test_prior_discussions_caps_at_limit(
    db_session: AsyncSession, owner: User,
):
    """Even if 12 past discussions overlap, the block caps at
    `_PRIOR_DISCUSSIONS_CAP` so the prompt doesn't balloon."""
    for i in range(12):
        await _make_done_discussion(
            db_session, owner_id=owner.id,
            topic=f"2330 round {i}", recommended=["2330"],
            days_old=i,
        )
    out = await discussion_service._assemble_prior_discussions(
        db_session, owner_id=owner.id, focus_symbols=["2330"],
    )
    assert len(out) == discussion_service._PRIOR_DISCUSSIONS_CAP


def test_prior_discussions_block_always_included_in_persona_filter():
    """Prior consensus matters to every archetype, so the filter's
    always-included set carries the block — even macro_analyst sees
    it despite not getting `top_gainers` / chip metrics."""
    assert "prior_discussions" in discussion_service._ALWAYS_INCLUDED_BLOCKS
    ctx = _full_ctx_for_filter() | {"prior_discussions": [{"id": "x"}]}
    out = discussion_service._filter_context_for_persona(ctx, "macro_analyst")
    assert "prior_discussions" in out


# ── inject_user_message ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_inject_user_message_persists_turn_at_current_round(
    db_session: AsyncSession, owner: User,
):
    """A user_input turn lands at `round=current_round` after the
    last persona reply (or any prior injection on the same round)."""
    row = await discussion_service.create_discussion(
        db_session,
        owner_id=owner.id,
        topic="t", rules="r",
        persona_ids=["buffett", "lynch"],
    )
    # Simulate a completed round 1 with two persona replies.
    row.current_round = 1
    db_session.add_all([
        DiscussionTurn(
            discussion_id=row.id, round=1, turn_index=0,
            persona_id="buffett", stance="supplement", content="ok",
        ),
        DiscussionTurn(
            discussion_id=row.id, round=1, turn_index=1,
            persona_id="lynch", stance="supplement", content="agree",
        ),
    ])
    await db_session.commit()

    turn = await discussion_service.inject_user_message(
        db_session, row, content="請聚焦在 2330",
    )
    assert turn.persona_id == discussion_service.USER_PERSONA_ID
    assert turn.stance == discussion_service.USER_INJECTION_STANCE
    assert turn.round == 1
    assert turn.turn_index == 2  # after lynch's index=1
    assert "2330" in turn.content


@pytest.mark.asyncio
async def test_inject_user_message_retries_on_unique_collision(
    db_session: AsyncSession, owner: User,
):
    """The PR #218 unique constraint on (discussion_id, round,
    turn_index) catches the race where two concurrent injects both
    compute `turn_index = max + 1`. The service must retry by re-
    reading max + bumping until it lands a free slot."""
    from sqlalchemy.exc import IntegrityError

    row = await discussion_service.create_discussion(
        db_session, owner_id=owner.id,
        topic="t", rules="r",
        persona_ids=["buffett", "lynch"],
    )
    row.current_round = 1
    db_session.add(DiscussionTurn(
        discussion_id=row.id, round=1, turn_index=0,
        persona_id="buffett", stance="supplement", content="ok",
    ))
    await db_session.commit()

    # First call from outside the test simulates a concurrent inject
    # winning the race: it inserts turn_index=1 between our SELECT
    # and our INSERT. We model it by having the first commit raise
    # IntegrityError, then succeed on the retry.
    real_commit = db_session.commit
    calls = {"n": 0}

    async def flaky_commit():
        calls["n"] += 1
        if calls["n"] == 1:
            raise IntegrityError("simulated", {}, Exception("uniq"))
        await real_commit()

    with patch.object(db_session, "commit", side_effect=flaky_commit), \
         patch.object(db_session, "rollback", new=AsyncMock()):
        turn = await discussion_service.inject_user_message(
            db_session, row, content="請聚焦在 2330",
        )
    assert turn.persona_id == discussion_service.USER_PERSONA_ID
    assert calls["n"] >= 2  # at least one retry happened


@pytest.mark.asyncio
async def test_inject_user_message_rejected_when_round_in_progress(
    db_session: AsyncSession, owner: User,
):
    """Injecting while a round is streaming would race the persona
    write order — reject 4XX-style at the service layer (router
    surfaces as 400)."""
    row = await discussion_service.create_discussion(
        db_session,
        owner_id=owner.id,
        topic="t", rules="r",
        persona_ids=["buffett", "lynch"],
    )
    row.current_round = 1
    row.status = discussion_service.STATUS_RUNNING
    await db_session.commit()
    with pytest.raises(ValueError, match="round is in progress"):
        await discussion_service.inject_user_message(
            db_session, row, content="x",
        )


@pytest.mark.asyncio
async def test_inject_user_message_allows_done_status_for_post_mortem(
    db_session: AsyncSession, owner: User,
):
    """The post-mortem flow injects a self-critique prompt against a
    discussion that has already concluded (status=done). The earlier
    `status != draft` guard rejected this case with the misleading
    "round is in progress" error even though no round was running.

    Contract under test (PR #266): injection is allowed on `done`
    status; only `running` is rejected.
    """
    row = await discussion_service.create_discussion(
        db_session,
        owner_id=owner.id,
        topic="t", rules="r",
        persona_ids=["buffett", "lynch"],
    )
    row.current_round = 1
    row.status = discussion_service.STATUS_DONE
    await db_session.commit()

    turn = await discussion_service.inject_user_message(
        db_session, row, content="post-mortem content",
    )
    assert turn.persona_id == discussion_service.USER_PERSONA_ID
    assert turn.stance == discussion_service.USER_INJECTION_STANCE
    assert turn.content == "post-mortem content"


@pytest.mark.asyncio
async def test_inject_user_message_rejected_before_first_round(
    db_session: AsyncSession, owner: User,
):
    """No personas have spoken yet → nothing to react to. The user
    can edit topic / rules directly instead."""
    row = await discussion_service.create_discussion(
        db_session,
        owner_id=owner.id,
        topic="t", rules="r",
        persona_ids=["buffett", "lynch"],
    )
    with pytest.raises(ValueError, match="before the first round"):
        await discussion_service.inject_user_message(
            db_session, row, content="x",
        )


@pytest.mark.asyncio
async def test_inject_user_message_validates_text_bounds(
    db_session: AsyncSession, owner: User,
):
    row = await discussion_service.create_discussion(
        db_session,
        owner_id=owner.id,
        topic="t", rules="r",
        persona_ids=["buffett", "lynch"],
    )
    row.current_round = 1
    await db_session.commit()
    with pytest.raises(ValueError):
        await discussion_service.inject_user_message(
            db_session, row, content="   ",
        )
    with pytest.raises(ValueError):
        await discussion_service.inject_user_message(
            db_session, row, content="x" * 3000,
        )


def test_format_history_renders_user_injection_with_directive_label():
    """User-injected turns must NOT be formatted as `_user · user_input：`
    (which the LLM would parse as a persona stance) — render them with
    a dedicated 【討論發起人插話】 marker instead."""
    turns = []
    t1 = DiscussionTurn()
    t1.round = 1
    t1.turn_index = 0
    t1.persona_id = "buffett"
    t1.stance = "supplement"
    t1.content = "看好台積電"
    turns.append(t1)

    inject = DiscussionTurn()
    inject.round = 1
    inject.turn_index = 1
    inject.persona_id = discussion_service.USER_PERSONA_ID
    inject.stance = discussion_service.USER_INJECTION_STANCE
    inject.content = "請聚焦在 2330"
    turns.append(inject)

    out = discussion_service._format_history(turns)
    assert "討論發起人插話" in out
    # The raw `_user · user_input：` form must NOT appear — that would
    # confuse the persona into reasoning about a stance.
    assert "_user · user_input" not in out


# ── _format_history compression ───────────────────────────────────


def test_summarize_turn_content_truncates_and_strips_markdown():
    """Long markdown-laden content collapses to a single line under
    `_HISTORY_SUMMARY_CHARS`; bold markers and stray newlines drop."""
    # Build something obviously larger than the cap so the truncation
    # branch fires (the inline data + filler combined is > 120 chars).
    raw = (
        "**台積電 (2330)** 已突破 60 日均\n"
        "目標價 1080 元\n\n"
        "停損 920 元，外資連 5 日買超 1.2 億。"
        + "額外文字" * 80
    )
    assert len(raw) > discussion_service._HISTORY_SUMMARY_CHARS
    out = discussion_service._summarize_turn_content(raw)
    # No markdown bold left.
    assert "**" not in out
    # No newlines — collapsed to single line.
    assert "\n" not in out
    # Capped at threshold + ellipsis when over budget.
    assert len(out) <= discussion_service._HISTORY_SUMMARY_CHARS + 1
    assert out.endswith("…")


def test_summarize_turn_content_handles_empty():
    assert "同意" in discussion_service._summarize_turn_content("")
    assert "同意" in discussion_service._summarize_turn_content("   \n")


def test_format_history_short_window_is_all_full_text():
    """Fewer than `_FULL_HISTORY_TURNS` turns → no summary section,
    everything shown verbatim. The persona reading round 2 of an
    8-persona discussion has already seen 8 turns; no compression
    yet because none is older than our recency window."""
    turns = []
    for i in range(4):
        t = DiscussionTurn()
        t.round = 1
        t.turn_index = i
        t.persona_id = "buffett"
        t.stance = "supplement"
        t.content = f"verbatim-{i}"
        turns.append(t)
    out = discussion_service._format_history(turns)
    assert "（較早輪次摘要）" not in out
    assert "（最近發言全文）" not in out
    assert "verbatim-0" in out
    assert "verbatim-3" in out


def test_format_history_long_window_summarises_older_keeps_recent_full():
    """When the transcript exceeds `_FULL_HISTORY_TURNS`, older turns
    appear under the summary banner with truncated content; the
    most recent `_FULL_HISTORY_TURNS` appear under the full-text
    banner verbatim. Use enough total turns that older + recent
    each have several rows."""
    full_window = discussion_service._FULL_HISTORY_TURNS
    older_count = 4
    total = full_window + older_count
    turns = []
    for i in range(total):
        t = DiscussionTurn()
        t.round = (i // 2) + 1
        t.turn_index = i % 2
        t.persona_id = "buffett"
        t.stance = "supplement"
        if i < older_count:
            t.content = f"OLD-{i} " + "x" * 300
        else:
            t.content = f"NEW-{i} 全文應原樣保留 " + "y" * 300
        turns.append(t)
    out = discussion_service._format_history(turns)
    assert "（較早輪次摘要）" in out
    assert "（最近發言全文）" in out
    # All OLD- markers are in the summary band but their 300-char
    # x-tail is truncated.
    for i in range(older_count):
        assert f"OLD-{i}" in out
    # All NEW- markers are in the full band.
    for i in range(older_count, total):
        assert f"NEW-{i}" in out
    # The OLD lines' 300-char x-filler is bounded by the summary
    # cap, so no single OLD line carries 200 consecutive x's.
    older_band_end = out.index("（最近發言全文）")
    older_band = out[:older_band_end]
    assert "x" * 200 not in older_band


def test_format_history_empty_returns_first_speaker_marker():
    assert "第一位" in discussion_service._format_history([])


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
async def test_run_round_accumulates_usage_across_tool_loop_iterations(
    db_session: AsyncSession, owner: User,
):
    """When a persona's provider runs a tool loop, the upstream stream
    emits a `usage` event PER iteration (PR #216). run_round must
    SUM them — the previous overwrite-only behaviour silently dropped
    intermediate iterations and made UsageCard report a fraction of
    actual cost.

    Verified by observing what `record_usage` ends up with: a
    persona that fires 3 LLM calls (200/100, 250/120, 180/80
    tokens respectively) should be recorded as 630 prompt + 300
    completion, not 180/80 (last only).
    """
    row = await discussion_service.create_discussion(
        db_session,
        owner_id=owner.id,
        topic="t", rules="r",
        persona_ids=["buffett", "lynch"],
    )

    async def _stream_with_loop(*_a, **_kw):
        # Iteration 1: tool call + usage event
        yield {"type": "tool_call", "id": "c1", "name": "get_quote",
               "args": {"symbol": "2330"}}
        yield {"type": "usage", "prompt_tokens": 200, "completion_tokens": 100}
        # Iteration 2: another tool call + usage event
        yield {"type": "tool_call", "id": "c2", "name": "run_dcf",
               "args": {"symbol": "2330"}}
        yield {"type": "usage", "prompt_tokens": 250, "completion_tokens": 120}
        # Iteration 3 (final): text content + usage event
        yield {"type": "delta",
               "text": '{"stance": "supplement", "content": "依工具回報"}'}
        yield {"type": "usage", "prompt_tokens": 180, "completion_tokens": 80}

    record_calls: list[dict] = []

    async def _capture_record_usage(_db, **kwargs):
        record_calls.append(kwargs)

    with patch(
        "services.discussion_service.stream_chat",
        side_effect=_stream_with_loop,
    ), patch(
        "services.discussion_service.gather_market_context",
        new=AsyncMock(return_value={"market": "TW"}),
    ), patch(
        "services.llm_usage_service.record_usage",
        new=_capture_record_usage,
    ):
        async for _ev in discussion_service.run_round(
            db_session, row, user_id=str(owner.id),
        ):
            pass

    # buffett + lynch each fire the same fake stream; record per
    # persona. Verify the SUMMED tokens, not just the last event's.
    assert len(record_calls) == 2
    for kw in record_calls:
        assert kw["prompt_tokens"] == 200 + 250 + 180  # 630
        assert kw["completion_tokens"] == 100 + 120 + 80  # 300


@pytest.mark.asyncio
async def test_run_round_tracks_tool_call_count_and_breakdown(
    db_session: AsyncSession, owner: User,
):
    """Persona that fires `get_quote` twice + `run_dcf` once must record
    `tool_call_count=3` and `tool_call_breakdown={"get_quote": 2,
    "run_dcf": 1}`. Without the breakdown the admin UsageCard's "why
    is this discussion slow" diagnostic has no signal beyond raw
    token totals.
    """
    row = await discussion_service.create_discussion(
        db_session,
        owner_id=owner.id,
        topic="t", rules="r",
        persona_ids=["buffett", "lynch"],
    )

    async def _stream(*_a, **_kw):
        yield {"type": "tool_call", "id": "c1", "name": "get_quote",
               "args": {"symbol": "2330"}}
        yield {"type": "tool_call", "id": "c2", "name": "get_quote",
               "args": {"symbol": "2454"}}
        yield {"type": "tool_call", "id": "c3", "name": "run_dcf",
               "args": {"symbol": "2330"}}
        yield {"type": "delta",
               "text": '{"stance": "supplement", "content": "OK"}'}
        yield {"type": "usage", "prompt_tokens": 100, "completion_tokens": 50}

    record_calls: list[dict] = []

    async def _capture(_db, **kwargs):
        record_calls.append(kwargs)

    with patch(
        "services.discussion_service.stream_chat",
        side_effect=_stream,
    ), patch(
        "services.discussion_service.gather_market_context",
        new=AsyncMock(return_value={"market": "TW"}),
    ), patch(
        "services.llm_usage_service.record_usage",
        new=_capture,
    ):
        async for _ev in discussion_service.run_round(
            db_session, row, user_id=str(owner.id),
        ):
            pass

    # buffett + lynch each fire the same fake stream — both rows
    # should carry identical tool_call counts.
    assert len(record_calls) == 2
    for kw in record_calls:
        assert kw["tool_call_count"] == 3
        assert kw["tool_call_breakdown"] == {"get_quote": 2, "run_dcf": 1}


@pytest.mark.asyncio
async def test_run_round_tool_call_breakdown_none_when_no_tools(
    db_session: AsyncSession, owner: User,
):
    """A persona turn with zero tool calls writes `tool_call_count=0`
    and `tool_call_breakdown=None`. The NULL-vs-{} distinction lets
    the admin tell apart "this row predates tool tracking" from
    "this row had tools available but didn't use any" — important
    for tracking gradual rollout."""
    row = await discussion_service.create_discussion(
        db_session,
        owner_id=owner.id,
        topic="t", rules="r",
        persona_ids=["buffett", "lynch"],
    )

    async def _stream(*_a, **_kw):
        yield {"type": "delta",
               "text": '{"stance": "supplement", "content": "OK"}'}
        yield {"type": "usage", "prompt_tokens": 100, "completion_tokens": 50}

    record_calls: list[dict] = []

    async def _capture(_db, **kwargs):
        record_calls.append(kwargs)

    with patch(
        "services.discussion_service.stream_chat",
        side_effect=_stream,
    ), patch(
        "services.discussion_service.gather_market_context",
        new=AsyncMock(return_value={"market": "TW"}),
    ), patch(
        "services.llm_usage_service.record_usage",
        new=_capture,
    ):
        async for _ev in discussion_service.run_round(
            db_session, row, user_id=str(owner.id),
        ):
            pass

    assert len(record_calls) == 2
    for kw in record_calls:
        assert kw["tool_call_count"] == 0
        assert kw["tool_call_breakdown"] is None


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
async def test_synthesize_conclusion_seeds_verify_after_date(
    db_session: AsyncSession, owner: User,
):
    """PR #218: synthesize_conclusion must set verify_after_date so
    the daily verifier picks the row up — used to be set only by the
    auto-run cron, leaving manual discussions permanently un-graded
    and `prior_discussions.verdict` = null forever."""
    row = await discussion_service.create_discussion(
        db_session, owner_id=owner.id,
        topic="topic", rules="rules",
        persona_ids=["buffett", "lynch"],
    )
    db_session.add(DiscussionTurn(
        discussion_id=row.id, round=1, turn_index=0,
        persona_id="buffett", stance="supplement", content="ok",
    ))
    await db_session.commit()
    assert row.verify_after_date is None

    raw = (
        '{"recommended_symbols": ["2330"], "reasoning": "ok", '
        '"risks": [], "time_horizon": "short_term", '
        '"consensus_score": 0.5}'
    )
    with patch(
        "services.discussion_service.stream_chat",
        side_effect=_stream_events(raw),
    ), patch(
        "services.discussion_service.gather_market_context",
        new=AsyncMock(return_value={"market": "TW"}),
    ):
        await discussion_service.synthesize_conclusion(
            db_session, row, user_id=str(owner.id),
        )
    refreshed = await db_session.get(Discussion, row.id)
    assert refreshed.verify_after_date is not None
    # Roughly 5 trading days (~7 calendar days) after today.
    from datetime import UTC as _UTC, datetime as _dt, timedelta as _td
    today = _dt.now(_UTC).date()
    assert (refreshed.verify_after_date - today) <= _td(days=14)
    assert (refreshed.verify_after_date - today) >= _td(days=3)


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
async def test_synthesize_conclusion_routes_post_mortem_to_separate_column(
    db_session: AsyncSession, owner: User,
):
    """PR #272: when the transcript already contains a post-mortem
    self-critique (`_user/user_input` turn whose content starts with
    "【事後檢討"), the synthesizer's output must land in
    `post_mortem_conclusion`, leaving the original `conclusion`
    intact. Operators rely on the side-by-side comparison."""
    row = await discussion_service.create_discussion(
        db_session, owner_id=owner.id,
        topic="topic", rules="rules",
        persona_ids=["buffett", "lynch"],
    )
    # Pre-existing original conclusion that must NOT be clobbered.
    row.conclusion = {
        "recommended_symbols": ["2330"],
        "reasoning": "original take",
        "risks": [],
        "time_horizon": "short_term",
        "consensus_score": 0.6,
    }
    # Round 1 persona turn + post-mortem injection (round 1
    # last turn) + round 2 persona reflection.
    db_session.add(DiscussionTurn(
        discussion_id=row.id, round=1, turn_index=0,
        persona_id="buffett", stance="agree", content="initial",
    ))
    db_session.add(DiscussionTurn(
        discussion_id=row.id, round=1, turn_index=1,
        persona_id=discussion_service.USER_PERSONA_ID,
        stance=discussion_service.USER_INJECTION_STANCE,
        content="【事後檢討 — 對答案】請反思",
    ))
    db_session.add(DiscussionTurn(
        discussion_id=row.id, round=2, turn_index=0,
        persona_id="buffett", stance="supplement",
        content="reflection: changing recommendation to 2454",
    ))
    await db_session.commit()

    raw = (
        '{"recommended_symbols": ["2454"], '
        '"reasoning": "post-mortem revised", '
        '"risks": [], "time_horizon": "short_term", '
        '"consensus_score": 0.55}'
    )
    with patch(
        "services.discussion_service.stream_chat",
        side_effect=_stream_events(raw),
    ), patch(
        "services.discussion_service.gather_market_context",
        new=AsyncMock(return_value={"market": "TW"}),
    ):
        await discussion_service.synthesize_conclusion(
            db_session, row, user_id=str(owner.id),
        )

    refreshed = await db_session.get(Discussion, row.id)
    # Original conclusion preserved verbatim.
    assert refreshed.conclusion is not None
    assert refreshed.conclusion["recommended_symbols"] == ["2330"]
    assert refreshed.conclusion["reasoning"] == "original take"
    # New conclusion landed in post_mortem_conclusion.
    assert refreshed.post_mortem_conclusion is not None
    assert refreshed.post_mortem_conclusion["recommended_symbols"] == ["2454"]
    assert refreshed.post_mortem_conclusion["reasoning"] == "post-mortem revised"


@pytest.mark.asyncio
async def test_synthesize_conclusion_writes_to_main_column_without_post_mortem(
    db_session: AsyncSession, owner: User,
):
    """No post-mortem in transcript → existing behaviour: write to
    `conclusion`, leave `post_mortem_conclusion` NULL. Guards
    against accidentally routing first-time syntheses to the
    post-mortem column."""
    row = await discussion_service.create_discussion(
        db_session, owner_id=owner.id,
        topic="topic", rules="rules",
        persona_ids=["buffett", "lynch"],
    )
    db_session.add(DiscussionTurn(
        discussion_id=row.id, round=1, turn_index=0,
        persona_id="buffett", stance="agree", content="just a regular turn",
    ))
    await db_session.commit()

    raw = (
        '{"recommended_symbols": ["2330"], "reasoning": "ok", '
        '"risks": [], "time_horizon": "short_term", '
        '"consensus_score": 0.5}'
    )
    with patch(
        "services.discussion_service.stream_chat",
        side_effect=_stream_events(raw),
    ), patch(
        "services.discussion_service.gather_market_context",
        new=AsyncMock(return_value={"market": "TW"}),
    ):
        await discussion_service.synthesize_conclusion(
            db_session, row, user_id=str(owner.id),
        )

    refreshed = await db_session.get(Discussion, row.id)
    assert refreshed.conclusion is not None
    assert refreshed.conclusion["recommended_symbols"] == ["2330"]
    assert refreshed.post_mortem_conclusion is None


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


def test_extract_focus_symbols_tw_falls_back_to_company_name(monkeypatch):
    """PR #221: topics written with TW company names instead of digit
    codes ("討論台積電 / 鴻海 短線") used to extract nothing — the
    digit regex saw no codes. Now the in-memory name_map fallback
    surfaces 2330 + 2317 so per-symbol news + focus_briefs +
    prior_discussions actually find them."""
    import services.tw_market_service as tw

    saved = dict(tw._name_map)
    try:
        tw._name_map.clear()
        tw._name_map.update({
            "2330": "台積電", "2454": "聯發科", "2317": "鴻海",
        })
        out = discussion_service.extract_focus_symbols(
            "討論台積電 / 鴻海 短線走勢", market="TW",
        )
        assert "2330" in out
        assert "2317" in out
        # 聯發科 wasn't mentioned — must NOT show up.
        assert "2454" not in out
    finally:
        tw._name_map.clear()
        tw._name_map.update(saved)


def test_extract_focus_symbols_tw_mixed_digit_and_name():
    """A topic mixing digit codes and names should yield deduped
    union — 2330 from regex, 聯發科 from name fallback."""
    import services.tw_market_service as tw

    saved = dict(tw._name_map)
    try:
        tw._name_map.clear()
        tw._name_map.update({"2330": "台積電", "2454": "聯發科"})
        out = discussion_service.extract_focus_symbols(
            "2330 vs 聯發科 哪個值得買", market="TW",
        )
        assert "2330" in out
        assert "2454" in out
    finally:
        tw._name_map.clear()
        tw._name_map.update(saved)


def test_extract_focus_symbols_us_skips_name_fallback():
    """US market topics ignore TW name lookups (different convention,
    bare-ticker regex already covers AAPL etc). Even if the user
    types '台積電 ADR' in a US discussion, the name fallback should
    NOT inject 2330."""
    import services.tw_market_service as tw

    saved = dict(tw._name_map)
    try:
        tw._name_map.clear()
        tw._name_map.update({"2330": "台積電"})
        out = discussion_service.extract_focus_symbols(
            "$AAPL vs 台積電 ADR (TSM)", market="US",
        )
        assert "AAPL" in out
        # Name fallback skipped for US — 2330 not added.
        assert "2330" not in out
    finally:
        tw._name_map.clear()
        tw._name_map.update(saved)


def test_extract_focus_symbols_name_fallback_swallows_lookup_failure(monkeypatch):
    """If the name-lookup helper raises (fresh deploy, partial
    init), extract_focus_symbols falls through with whatever the
    regex pass found instead of crashing the whole round."""
    def boom(_text, *, limit):
        raise RuntimeError("symbol map not loaded")

    monkeypatch.setattr(
        "services.tw_market_service.find_symbols_by_names_in_text",
        boom,
    )
    out = discussion_service.extract_focus_symbols(
        "2330 跟 台積電", market="TW",
    )
    assert "2330" in out  # regex still produced it


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
async def test_create_discussion_accepts_as_of_date(
    db_session: AsyncSession, owner: User,
):
    """PR #224: as_of_date stored on the row enables backtest mode."""
    from datetime import date as _date
    row = await discussion_service.create_discussion(
        db_session,
        owner_id=owner.id,
        topic="2330 backtest",
        rules="r",
        persona_ids=["buffett", "lynch"],
        as_of_date=_date(2025, 1, 15),
    )
    assert row.as_of_date == _date(2025, 1, 15)


@pytest.mark.asyncio
async def test_create_discussion_rejects_future_as_of_date(
    db_session: AsyncSession, owner: User,
):
    """Future as_of has no historical data to filter on AND the
    verifier's 'next 5 trading days' window doesn't exist yet —
    reject with a clear ValueError."""
    from datetime import UTC as _UTC, datetime as _dt, timedelta as _td
    future = (_dt.now(_UTC) + _td(days=10)).date()
    with pytest.raises(ValueError, match="future"):
        await discussion_service.create_discussion(
            db_session,
            owner_id=owner.id,
            topic="bad", rules="r",
            persona_ids=["buffett", "lynch"],
            as_of_date=future,
        )


@pytest.mark.asyncio
async def test_synthesize_conclusion_anchors_verify_after_to_as_of_date(
    db_session: AsyncSession, owner: User,
):
    """Backtest mode: verify_after_date should be 5 trading days
    after `as_of_date`, not 5 trading days after today. So an
    as_of=2025-01-15 backtest run today produces a row the
    verifier picks up immediately (window already in the past)."""
    from datetime import date as _date

    row = await discussion_service.create_discussion(
        db_session, owner_id=owner.id,
        topic="t", rules="r",
        persona_ids=["buffett", "lynch"],
        as_of_date=_date(2025, 1, 15),
    )
    db_session.add(DiscussionTurn(
        discussion_id=row.id, round=1, turn_index=0,
        persona_id="buffett", stance="supplement", content="ok",
    ))
    await db_session.commit()

    raw = (
        '{"recommended_symbols": ["2330"], "reasoning": "ok", '
        '"risks": [], "time_horizon": "short_term", '
        '"consensus_score": 0.5}'
    )
    with patch(
        "services.discussion_service.stream_chat",
        side_effect=_stream_events(raw),
    ), patch(
        "services.discussion_service.gather_market_context",
        new=AsyncMock(return_value={"market": "TW"}),
    ):
        await discussion_service.synthesize_conclusion(
            db_session, row, user_id=str(owner.id),
        )
    refreshed = await db_session.get(Discussion, row.id)
    # Roughly 7 days (5 trading days) after as_of=2025-01-15.
    from datetime import timedelta as _td
    expected_lo = _date(2025, 1, 15) + _td(days=5)
    expected_hi = _date(2025, 1, 15) + _td(days=14)
    assert expected_lo <= refreshed.verify_after_date <= expected_hi


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
async def test_gather_market_context_backtest_drops_empty_news_blocks(
    db_session: AsyncSession,
):
    """Backtest mode: when the news archive doesn't reach `as_of` (we
    only started ingesting on deploy day; no paid history API), the
    aggregator returns an empty `headlines` list with `bullish=0,
    bearish=0, neutral=0` counts. Live mode preserves that block
    (operators read empty as 'cron hasn't run yet'), but backtest mode
    should drop it to `null` — otherwise the LLM sees `0/0/0` and
    interprets it as 'market has no sentiment' rather than 'we have
    no data for that period'.

    The `backtest` flag on the ctx tells the frontend to render a
    'news archive doesn't reach this date' warning instead of the
    usual `bullish N / bearish M` line."""
    from datetime import date as _date
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
            db_session, as_of=_date(2025, 1, 1),
        )

    assert ctx["backtest"] is True
    assert ctx["as_of"] == "2025-01-01"
    # Both news blocks dropped to None — no headlines, backtest mode.
    assert ctx["news_sentiment"] is None
    assert ctx["international_sentiment"] is None


@pytest.mark.asyncio
async def test_gather_market_context_backtest_threads_as_of_into_chip_blocks(
    db_session: AsyncSession,
):
    """Backtest mode (PR #226): chip metric reads must respect `as_of`
    so a discussion anchored at 2025-01-15 doesn't see foreign-flow /
    margin / revenue / govt-bank rows from 2025-04-30. Insert rows
    on both dates, run with as_of=2025-01-15, assert only the older
    rows come back."""
    from datetime import date as _date
    import services.tw_market_service as _tw   # noqa: F401
    from services.ingest.repository import (
        GovtBankFlowRow,
        InstitutionalDailyRow,
        MarginDailyRow,
        MarketInstitutionalRow,
        upsert_govt_bank_flows,
        upsert_institutional_daily,
        upsert_margin_daily,
        upsert_market_institutional_daily,
    )

    backtest_day = _date(2025, 1, 15)
    later_day = _date(2025, 4, 30)

    # Foreign buyers — 2330 leads on backtest_day, 2454 leads on later_day.
    await upsert_institutional_daily(db_session, [
        InstitutionalDailyRow(
            market="TW", symbol="2330", ts=backtest_day,
            fini_buy=100_000, fini_sell=20_000,
            sitc_buy=0, sitc_sell=0, dealer_buy=0, dealer_sell=0,
            source="twse",
        ),
        InstitutionalDailyRow(
            market="TW", symbol="2454", ts=later_day,
            fini_buy=999_999, fini_sell=0,
            sitc_buy=0, sitc_sell=0, dealer_buy=0, dealer_sell=0,
            source="twse",
        ),
    ])
    # Margin — different totals on each day.
    await upsert_margin_daily(db_session, [
        MarginDailyRow(
            market="TW", symbol="2330", ts=backtest_day,
            margin_purchase=0, margin_balance=11_111,
            short_sale=0, short_balance=2_222, source="twse",
        ),
        MarginDailyRow(
            market="TW", symbol="2330", ts=later_day,
            margin_purchase=0, margin_balance=99_999,
            short_sale=0, short_balance=8_888, source="twse",
        ),
    ])
    # Govt bank flow — different net on each day.
    await upsert_govt_bank_flows(db_session, [
        GovtBankFlowRow(
            market="TW", ts=backtest_day, bank_name="bank1",
            buy_amount=10_000, sell_amount=5_000, source="twse",
        ),
        GovtBankFlowRow(
            market="TW", ts=later_day, bank_name="bank1",
            buy_amount=999_000, sell_amount=0, source="twse",
        ),
    ])
    # Market-wide institutional — same pattern.
    await upsert_market_institutional_daily(db_session, [
        MarketInstitutionalRow(
            market="TW", ts=backtest_day,
            foreign_buy=1_000, foreign_sell=500,
            sitc_buy=0, sitc_sell=0, dealer_buy=0, dealer_sell=0,
            source="twse",
        ),
        MarketInstitutionalRow(
            market="TW", ts=later_day,
            foreign_buy=999_000, foreign_sell=0,
            sitc_buy=0, sitc_sell=0, dealer_buy=0, dealer_sell=0,
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
            db_session, market="TW", as_of=backtest_day,
        )

    # foreign_buyers must only contain 2330 (backtest_day row);
    # 2454's later_day row must NOT bleed through.
    syms = [row["symbol"] for row in ctx["top_foreign_buyers"]]
    assert "2330" in syms
    assert "2454" not in syms

    # Margin trend reflects the 2025-01-15 row, not 2025-04-30.
    assert ctx["margin_balance_trend"]["total_margin_balance"] == 11_111
    assert ctx["margin_balance_trend"]["total_short_balance"] == 2_222

    # Govt bank flow's only entry is the backtest_day row.
    flow = ctx["govt_bank_flow_5d"]
    assert len(flow) == 1
    assert flow[0]["date"] == backtest_day.isoformat()
    assert flow[0]["net"] == 5_000

    # Market-wide institutional reflects the 2025-01-15 numbers.
    inst = ctx["market_institutional_5d"]
    assert len(inst) == 1
    assert inst[0]["date"] == backtest_day.isoformat()
    assert inst[0]["foreign_net"] == 500


@pytest.mark.asyncio
async def test_gather_market_context_backtest_passes_as_of_to_market_services(
    db_session: AsyncSession,
):
    """Backtest mode (PR #226): the screener / index / macro fetchers
    receive `as_of` as a kwarg so they can route to the historical
    data path instead of returning live data."""
    from datetime import date as _date
    import services.tw_market_service as _tw   # noqa: F401

    backtest_day = _date(2025, 1, 15)

    screener_mock = AsyncMock(return_value=[])
    index_mock = AsyncMock(return_value={})
    macro_mock = AsyncMock(return_value={})
    focus_mock = AsyncMock(return_value=[])

    with patch.object(_tw, "get_screener", new=screener_mock), \
         patch.object(_tw, "get_index", new=index_mock), \
         patch(
            "services.discussion_service._assemble_macro_block",
            new=macro_mock,
         ), \
         patch(
            "services.discussion_service._assemble_focus_briefs",
            new=focus_mock,
         ):
        await discussion_service.gather_market_context(
            db_session, market="TW", as_of=backtest_day,
        )

    assert screener_mock.call_args.kwargs.get("as_of") == backtest_day
    assert index_mock.call_args.kwargs.get("as_of") == backtest_day
    assert macro_mock.call_args.kwargs.get("as_of") == backtest_day


@pytest.mark.asyncio
async def test_gather_market_context_live_keeps_empty_news_block(
    db_session: AsyncSession,
):
    """Live mode (no `as_of`): even when no headlines are scored,
    `news_sentiment` stays as the empty-counts dict. Operators read
    the zero counts as 'cron hasn't run yet' — distinct from the
    backtest case above where dropping the block tells the LLM 'we
    have no data here'."""
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
        ctx = await discussion_service.gather_market_context(db_session)

    assert ctx["backtest"] is False
    assert ctx["as_of"] is None
    assert ctx["news_sentiment"] is not None
    assert ctx["news_sentiment"]["bullish"] == 0


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


# ── PR #267: post-mortem fallout fixes ────────────────────────────


def test_safe_conclusion_repairs_truncated_json_before_close_brace():
    """Reasoning model under post-mortem load: long chain-of-thought
    eats most of `max_tokens`, the JSON starts streaming but gets
    cut off before the close brace. Repair adds the missing `}`
    so the operator sees the partial conclusion instead of the
    parse-error placeholder."""
    truncated = (
        '{"recommended_symbols": ["2330", "0050"], '
        '"reasoning": "事後檢討後仍維持原推薦", '
        '"risks": ["匯率"], '
        '"time_horizon": "short_term", '
        '"consensus_score": 0.6'
        # Note: missing closing `}` — runs out of tokens here.
    )
    out = discussion_service._safe_conclusion(truncated)
    assert "_parse_error" not in out, (
        f"truncated JSON should be repaired, not flagged as parse error: {out}"
    )
    assert out["recommended_symbols"] == ["2330", "0050"]
    assert out["consensus_score"] == 0.6


def test_safe_conclusion_repairs_json_truncated_inside_string():
    """Even worse: truncation lands INSIDE a string literal — the
    repair must close the string before closing the object."""
    truncated = (
        '{"recommended_symbols": ["2330"], '
        '"reasoning": "我認為事後檢討的最終共識仍然是維持原推'
        # ← string never closed, no commas / braces follow
    )
    out = discussion_service._safe_conclusion(truncated)
    assert "_parse_error" not in out
    assert out["recommended_symbols"] == ["2330"]
    assert "事後檢討" in out["reasoning"]


def test_safe_conclusion_repairs_json_truncated_after_key_colon():
    """Most pathological case: truncation lands right after a key's
    colon (no value yet). Repair drops the orphan key/value pair
    rather than producing `"foo":}` which is invalid."""
    truncated = (
        '{"recommended_symbols": ["2330"], '
        '"reasoning": "ok", '
        '"risks": '
        # ← cuts off right at `:` of risks — no value
    )
    out = discussion_service._safe_conclusion(truncated)
    assert "_parse_error" not in out
    assert out["recommended_symbols"] == ["2330"]


def test_safe_conclusion_still_flags_unparseable_garbage():
    """Repair shouldn't accidentally rescue non-JSON output. A pure-
    prose response with no `{` should still surface as parse_error
    so the operator knows to retry."""
    raw = "I'm sorry, I cannot answer this question."
    out = discussion_service._safe_conclusion(raw)
    assert out["_parse_error"] is True
    assert out["recommendations"] == []


# ── PR-C0: per-symbol confidence in conclusion ─────────────────────


def test_safe_conclusion_parses_recommendations_with_confidence():
    raw = (
        '{"recommendations": ['
        '{"symbol": "2330", "confidence": 0.75}, '
        '{"symbol": "0050", "confidence": 0.55}'
        '], "reasoning": "ok", "risks": [], '
        '"time_horizon": "short_term", "consensus_score": 0.7}'
    )
    out = discussion_service._safe_conclusion(raw)
    assert "_parse_error" not in out
    assert out["recommendations"] == [
        {"symbol": "2330", "confidence": 0.75},
        {"symbol": "0050", "confidence": 0.55},
    ]
    # recommended_symbols stays in sync — derived from recommendations
    # so existing consumers keep working.
    assert out["recommended_symbols"] == ["2330", "0050"]


def test_safe_conclusion_falls_back_when_recommendations_missing():
    """Old discussions / pre-C0 LLM outputs only emit
    `recommended_symbols`. Parser fills in a neutral 0.5 confidence so
    downstream Brier scoring (PR-C1) can always assume the structured
    shape exists."""
    raw = (
        '{"recommended_symbols": ["2330", "2454"], '
        '"reasoning": "ok", "risks": [], '
        '"time_horizon": "short_term", "consensus_score": 0.5}'
    )
    out = discussion_service._safe_conclusion(raw)
    assert "_parse_error" not in out
    assert out["recommended_symbols"] == ["2330", "2454"]
    assert out["recommendations"] == [
        {"symbol": "2330", "confidence": 0.5},
        {"symbol": "2454", "confidence": 0.5},
    ]


def test_safe_conclusion_clamps_invalid_confidence():
    """LLMs occasionally emit confidence > 1, < 0, NaN, or strings.
    Clamp to [0, 1] and treat unparseable as the neutral default."""
    raw = (
        '{"recommendations": ['
        '{"symbol": "A", "confidence": 1.5}, '
        '{"symbol": "B", "confidence": -0.3}, '
        '{"symbol": "C", "confidence": "high"}, '
        '{"symbol": "D", "confidence": null}'
        '], "reasoning": "x", "risks": [], '
        '"time_horizon": "short_term", "consensus_score": 0.5}'
    )
    out = discussion_service._safe_conclusion(raw)
    confs = {r["symbol"]: r["confidence"] for r in out["recommendations"]}
    assert confs["A"] == 1.0
    assert confs["B"] == 0.0
    assert confs["C"] == 0.5   # unparseable string → neutral
    assert confs["D"] == 0.5   # null → neutral


def test_safe_conclusion_dedupes_recommendations_keeping_first():
    raw = (
        '{"recommendations": ['
        '{"symbol": "2330", "confidence": 0.8}, '
        '{"symbol": "2330", "confidence": 0.4}, '
        '{"symbol": "0050", "confidence": 0.6}'
        '], "reasoning": "ok", "risks": [], '
        '"time_horizon": "short_term", "consensus_score": 0.5}'
    )
    out = discussion_service._safe_conclusion(raw)
    assert out["recommended_symbols"] == ["2330", "0050"]
    confs = {r["symbol"]: r["confidence"] for r in out["recommendations"]}
    assert confs["2330"] == 0.8   # first occurrence wins


def test_safe_conclusion_caps_recommendations_at_five():
    entries = ", ".join(
        f'{{"symbol": "S{i}", "confidence": 0.5}}' for i in range(8)
    )
    raw = (
        f'{{"recommendations": [{entries}], '
        '"reasoning": "ok", "risks": [], '
        '"time_horizon": "short_term", "consensus_score": 0.5}'
    )
    out = discussion_service._safe_conclusion(raw)
    assert len(out["recommendations"]) == 5
    assert len(out["recommended_symbols"]) == 5


def test_safe_conclusion_prefers_recommendations_over_legacy_symbols():
    """When LLM emits both fields and they conflict, recommendations
    is the source of truth — recommended_symbols gets reconstructed
    from it."""
    raw = (
        '{"recommendations": ['
        '{"symbol": "2454", "confidence": 0.7}'
        '], "recommended_symbols": ["WRONG", "STILL_WRONG"], '
        '"reasoning": "ok", "risks": [], '
        '"time_horizon": "short_term", "consensus_score": 0.5}'
    )
    out = discussion_service._safe_conclusion(raw)
    assert out["recommended_symbols"] == ["2454"]
    assert out["recommendations"] == [{"symbol": "2454", "confidence": 0.7}]


def test_safe_conclusion_skips_recommendation_entries_without_symbol():
    raw = (
        '{"recommendations": ['
        '{"symbol": "", "confidence": 0.7}, '
        '{"confidence": 0.6}, '
        '{"symbol": "2330", "confidence": 0.65}'
        '], "reasoning": "ok", "risks": [], '
        '"time_horizon": "short_term", "consensus_score": 0.5}'
    )
    out = discussion_service._safe_conclusion(raw)
    assert out["recommended_symbols"] == ["2330"]
    assert len(out["recommendations"]) == 1


def test_safe_conclusion_parse_error_returns_empty_recommendations():
    out = discussion_service._safe_conclusion(
        "I'm sorry, I cannot answer."
    )
    assert out["_parse_error"] is True
    assert out["recommended_symbols"] == []
    assert out["recommendations"] == []


# ── PR-C2 follow-up: calibration apply in synthesize_conclusion ──


@pytest.mark.asyncio
async def test_apply_calibration_overlays_calibrated_confidence(
    db_session: AsyncSession, owner: User,
):
    """When the strategy template has a calibration curve, every
    recommendation gains a `calibrated_confidence` field looking
    up the raw value through the isotonic curve."""
    from datetime import date as _date

    from models.backtest_sweep import BacktestSweep
    from models.discussion_strategy_template import (
        DiscussionStrategyTemplate,
    )

    tmpl = DiscussionStrategyTemplate(
        id=uuid.uuid4(),
        owner_id=owner.id,
        name="t", topic="t", rules="r", market="TW",
        persona_ids=["a"],
        default_rounds=1, default_concurrency=1,
        default_auto_post_mortem=False,
        persona_weights={},
        # Synthesizer is over-confident at 0.9 → curve maps to 0.4.
        # Lower buckets are well-calibrated.
        calibration_curve=[
            {"raw": 0.5, "calibrated": 0.5},
            {"raw": 0.9, "calibrated": 0.4},
        ],
    )
    db_session.add(tmpl)
    await db_session.commit()

    sweep = BacktestSweep(
        id=uuid.uuid4(),
        owner_id=owner.id,
        topic="t", rules="r", market="TW",
        persona_ids=["a"],
        anchor_date=_date(2026, 5, 1),
        trading_days_count=5, rounds_per_discussion=1,
        concurrency=1, auto_post_mortem=False,
        strategy_id=tmpl.id,
    )
    db_session.add(sweep)
    await db_session.commit()
    await db_session.refresh(sweep)

    discussion = Discussion(
        id=uuid.uuid4(), owner_id=owner.id,
        topic="t", rules="r", persona_ids=["a"],
        market="TW", status="running", current_round=1,
        sweep_id=sweep.id,
    )
    conclusion = {
        "recommended_symbols": ["A", "B"],
        "recommendations": [
            {"symbol": "A", "confidence": 0.9},   # high → calibrate to 0.4
            {"symbol": "B", "confidence": 0.5},   # mid → calibrate to 0.5
        ],
        "reasoning": "x", "risks": [],
        "time_horizon": "short_term", "consensus_score": 0.5,
    }

    await discussion_service._apply_calibration_to_conclusion(
        db_session, discussion, conclusion,
    )

    confs = {r["symbol"]: r for r in conclusion["recommendations"]}
    # Raw values preserved so future re-fits stay grounded in
    # what the synthesizer originally emitted.
    assert confs["A"]["confidence"] == 0.9
    assert confs["B"]["confidence"] == 0.5
    # Calibrated values applied per the curve.
    assert confs["A"]["calibrated_confidence"] == 0.4
    assert confs["B"]["calibrated_confidence"] == 0.5


@pytest.mark.asyncio
async def test_apply_calibration_noop_when_no_sweep(
    db_session: AsyncSession, owner: User,
):
    """Live discussions (no sweep_id) skip calibration entirely —
    nothing is added to the recommendations."""
    discussion = Discussion(
        id=uuid.uuid4(), owner_id=owner.id,
        topic="t", rules="r", persona_ids=["a"],
        market="TW", status="running", current_round=1,
        sweep_id=None,
    )
    conclusion = {
        "recommended_symbols": ["A"],
        "recommendations": [{"symbol": "A", "confidence": 0.9}],
        "reasoning": "x", "risks": [],
        "time_horizon": "short_term", "consensus_score": 0.5,
    }
    await discussion_service._apply_calibration_to_conclusion(
        db_session, discussion, conclusion,
    )
    assert "calibrated_confidence" not in conclusion["recommendations"][0]


@pytest.mark.asyncio
async def test_apply_calibration_noop_when_curve_empty(
    db_session: AsyncSession, owner: User,
):
    """Strategy hasn't accumulated enough samples to fit a curve
    yet — calibration_curve is None / empty. The function must
    leave the recommendations unchanged."""
    from datetime import date as _date

    from models.backtest_sweep import BacktestSweep
    from models.discussion_strategy_template import (
        DiscussionStrategyTemplate,
    )

    tmpl = DiscussionStrategyTemplate(
        id=uuid.uuid4(),
        owner_id=owner.id,
        name="t", topic="t", rules="r", market="TW",
        persona_ids=["a"],
        default_rounds=1, default_concurrency=1,
        default_auto_post_mortem=False,
        persona_weights={},
        calibration_curve=None,
    )
    db_session.add(tmpl)
    await db_session.commit()

    sweep = BacktestSweep(
        id=uuid.uuid4(), owner_id=owner.id,
        topic="t", rules="r", market="TW",
        persona_ids=["a"],
        anchor_date=_date(2026, 5, 1),
        trading_days_count=5, rounds_per_discussion=1,
        concurrency=1, auto_post_mortem=False,
        strategy_id=tmpl.id,
    )
    db_session.add(sweep)
    await db_session.commit()
    await db_session.refresh(sweep)

    discussion = Discussion(
        id=uuid.uuid4(), owner_id=owner.id,
        topic="t", rules="r", persona_ids=["a"],
        market="TW", status="running", current_round=1,
        sweep_id=sweep.id,
    )
    conclusion = {
        "recommended_symbols": ["A"],
        "recommendations": [{"symbol": "A", "confidence": 0.9}],
        "reasoning": "x", "risks": [],
        "time_horizon": "short_term", "consensus_score": 0.5,
    }
    await discussion_service._apply_calibration_to_conclusion(
        db_session, discussion, conclusion,
    )
    assert "calibrated_confidence" not in conclusion["recommendations"][0]


@pytest.mark.asyncio
async def test_apply_calibration_neutralizes_nan_input(
    db_session: AsyncSession, owner: User,
):
    """Audit follow-up #3: a synthesizer that somehow emitted NaN
    or +inf as raw confidence (LLM returning the literal string
    "NaN" / "Infinity") gets neutralized to 0.5 instead of
    leaking through as a corner-clamped 1.0 — that latter would
    pollute the brier comparison with a confidently-wrong
    value."""
    from datetime import date as _date

    from models.backtest_sweep import BacktestSweep
    from models.discussion_strategy_template import (
        DiscussionStrategyTemplate,
    )

    tmpl = DiscussionStrategyTemplate(
        id=uuid.uuid4(),
        owner_id=owner.id,
        name="t", topic="t", rules="r", market="TW",
        persona_ids=["a"],
        default_rounds=1, default_concurrency=1,
        default_auto_post_mortem=False,
        persona_weights={},
        # well-calibrated curve at 0.5 → 0.5
        calibration_curve=[
            {"raw": 0.5, "calibrated": 0.5},
            {"raw": 1.0, "calibrated": 0.7},
        ],
    )
    db_session.add(tmpl)
    await db_session.commit()

    sweep = BacktestSweep(
        id=uuid.uuid4(), owner_id=owner.id,
        topic="t", rules="r", market="TW",
        persona_ids=["a"],
        anchor_date=_date(2026, 5, 1),
        trading_days_count=5, rounds_per_discussion=1,
        concurrency=1, auto_post_mortem=False,
        strategy_id=tmpl.id,
    )
    db_session.add(sweep)
    await db_session.commit()
    await db_session.refresh(sweep)

    discussion = Discussion(
        id=uuid.uuid4(), owner_id=owner.id,
        topic="t", rules="r", persona_ids=["a"],
        market="TW", status="running", current_round=1,
        sweep_id=sweep.id,
    )
    conclusion = {
        "recommended_symbols": ["A", "B", "C"],
        "recommendations": [
            {"symbol": "A", "confidence": float("nan")},
            {"symbol": "B", "confidence": float("inf")},
            {"symbol": "C", "confidence": float("-inf")},
        ],
        "reasoning": "x", "risks": [],
        "time_horizon": "short_term", "consensus_score": 0.5,
    }

    await discussion_service._apply_calibration_to_conclusion(
        db_session, discussion, conclusion,
    )

    # All three pathological inputs collapse to the neutral 0.5
    # → curve maps to 0.5 calibrated. None of them leak through
    # as NaN / inf / corner-clamped 0.0 / 1.0.
    for rec in conclusion["recommendations"]:
        assert rec["calibrated_confidence"] == 0.5


@pytest.mark.asyncio
async def test_apply_calibration_skips_parse_error_conclusion(
    db_session: AsyncSession, owner: User,
):
    """A parse-error placeholder conclusion has empty recommendations.
    The function must not crash trying to map an empty list."""
    discussion = Discussion(
        id=uuid.uuid4(), owner_id=owner.id,
        topic="t", rules="r", persona_ids=["a"],
        market="TW", status="running", current_round=1,
        sweep_id=uuid.uuid4(),
    )
    conclusion = {
        "recommended_symbols": [],
        "recommendations": [],
        "reasoning": "parse failed", "risks": [],
        "time_horizon": "short_term", "consensus_score": 0.0,
        "_parse_error": True,
    }
    # Should not raise even though the sweep_id won't resolve.
    await discussion_service._apply_calibration_to_conclusion(
        db_session, discussion, conclusion,
    )
    assert conclusion["recommendations"] == []


def test_format_transcript_renders_user_input_as_directive():
    """The post-mortem injection lands as a `_user/user_input` turn.
    The synthesizer's `_format_transcript` must render it as a
    discussion-owner directive rather than just another `_user`-named
    persona, so the LLM doesn't try to *answer* the embedded
    questions in its `reasoning` field (the failure mode that caused
    `_parse_error: True` after PR #266)."""
    from datetime import UTC as _UTC, datetime as _dt

    from models.discussion import DiscussionTurn

    persona_turn = DiscussionTurn(
        round=1, turn_index=0, persona_id="market_analyst",
        stance="agree", content="RSI 偏高",
        created_at=_dt.now(_UTC),
    )
    user_turn = DiscussionTurn(
        round=1, turn_index=1,
        persona_id=discussion_service.USER_PERSONA_ID,
        stance=discussion_service.USER_INJECTION_STANCE,
        content="【事後檢討】請反思",
        created_at=_dt.now(_UTC),
    )
    out = discussion_service._format_transcript([persona_turn, user_turn])
    assert "market_analyst" in out
    # User-input rendered with the directive label; the raw `_user`
    # token must NOT appear in the persona-prefix style.
    assert "討論發起人指示" in out
    assert "/_user/user_input]" not in out


def test_format_transcript_handles_empty_user_input():
    """Edge: an empty user_input turn (shouldn't happen given
    `_validate_text`, but defensive) shouldn't crash the formatter."""
    from datetime import UTC as _UTC, datetime as _dt

    from models.discussion import DiscussionTurn

    user_turn = DiscussionTurn(
        round=1, turn_index=0,
        persona_id=discussion_service.USER_PERSONA_ID,
        stance=discussion_service.USER_INJECTION_STANCE,
        content="",
        created_at=_dt.now(_UTC),
    )
    out = discussion_service._format_transcript([user_turn])
    assert "討論發起人指示" in out


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
        market="TW", symbols=[],
    )
    assert out == []


# ── _build_persona_tool_kwargs ────────────────────────────────────


def test_build_persona_tool_kwargs_viewer_gets_tools_without_user_data(monkeypatch):
    """Viewers (free tier) gain access to all 13 public market-data
    tools but NOT `query_user_data` — the SQL toolset would surface
    portfolio / watchlist / alert rows to the LLM provider, which we
    don't want for unverified accounts.

    Stub the openai_compat builder so the test doesn't need
    claude_agent_sdk installed."""
    import sys
    import types

    captured: dict = {}

    def fake_build(user_id, *, as_of_date=None, include_user_data=True):
        captured["include_user_data"] = include_user_data
        # Mimic the real builder's filter so the test sees the same
        # surface area the persona would.
        schemas = [
            {"type": "function", "function": {"name": "get_quote"}},
            {"type": "function", "function": {"name": "query_user_data"}},
        ]
        dispatch = {"get_quote": object(), "query_user_data": object()}
        if not include_user_data:
            schemas = [
                s for s in schemas if s["function"]["name"] != "query_user_data"
            ]
            dispatch.pop("query_user_data", None)
        return (schemas, dispatch)

    fake_mod = types.ModuleType("ai.tools.openai_compat")
    fake_mod.build_openai_compat_toolset = fake_build
    monkeypatch.setitem(sys.modules, "ai.tools.openai_compat", fake_mod)

    out = discussion_service._build_persona_tool_kwargs(
        provider="groq",
        user_role="viewer",
        user_id="abc",
    )
    assert captured["include_user_data"] is False
    assert "openai_tool_dispatch" in out
    assert "query_user_data" not in out["openai_tool_dispatch"]
    assert "get_quote" in out["openai_tool_dispatch"]


def test_build_persona_tool_kwargs_analyst_gets_user_data(monkeypatch):
    """Analyst / admin retain access to query_user_data so personas
    can read the caller's holdings + watchlist when relevant."""
    import sys
    import types

    captured: dict = {}

    def fake_build(user_id, *, as_of_date=None, include_user_data=True):
        captured["include_user_data"] = include_user_data
        return (
            [{"type": "function", "function": {"name": "query_user_data"}}],
            {"query_user_data": object()},
        )

    fake_mod = types.ModuleType("ai.tools.openai_compat")
    fake_mod.build_openai_compat_toolset = fake_build
    monkeypatch.setitem(sys.modules, "ai.tools.openai_compat", fake_mod)

    out = discussion_service._build_persona_tool_kwargs(
        provider="groq",
        user_role="analyst",
        user_id="abc",
    )
    assert captured["include_user_data"] is True
    assert "query_user_data" in out["openai_tool_dispatch"]


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

    def fake_build(_user_id, *, as_of_date=None, include_user_data=True):
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


def test_build_persona_tool_kwargs_forwards_as_of_to_openai_compat(monkeypatch):
    """Backtest mode: discussion's `as_of_date` must reach the toolset
    builder so chip-flow tools anchor at the historical date instead
    of `today`. Without this plumbing the persona's tool calls would
    silently fetch live data and contaminate the backtest."""
    import sys
    import types
    from datetime import date

    captured: dict = {}

    def fake_build(user_id, *, as_of_date=None, include_user_data=True):
        captured["as_of_date"] = as_of_date
        return ([{"type": "function", "name": "get_quote"}],
                {"get_quote": object()})

    fake_mod = types.ModuleType("ai.tools.openai_compat")
    fake_mod.build_openai_compat_toolset = fake_build
    monkeypatch.setitem(sys.modules, "ai.tools.openai_compat", fake_mod)

    discussion_service._build_persona_tool_kwargs(
        provider="groq",
        user_role="analyst",
        user_id="u1",
        as_of_date=date(2026, 4, 15),
    )
    assert captured["as_of_date"] == date(2026, 4, 15)


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
    async def fake_brief(sym: str, *, as_of=None) -> dict:
        if sym == "BAD":
            raise RuntimeError("upstream blew up")
        return {"symbol": sym, "quote": {"price": 100.0}}

    with patch(
        "services.discussion.focus_briefs._build_tw_focus_brief",
        new=fake_brief,
    ):
        out = await discussion_service._assemble_focus_briefs(
            market="TW", symbols=["2330", "BAD", "2454"],
        )
    syms = [b["symbol"] for b in out]
    assert "2330" in syms and "2454" in syms and "BAD" not in syms
