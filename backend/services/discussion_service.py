"""Expert-discussion orchestrator.

A `Discussion` is a round-table session where N AI personas take turns
arguing a user-supplied topic under user-supplied rules. Each round walks
the persona roster in order; each persona reads the prior turns and
returns a structured `{stance, content}` reply where `stance ∈
{agree, dissent, supplement}`. After enough rounds (or on demand) a
synthesizer persona reads every turn and produces a structured
conclusion (recommended_symbols / reasoning / risks / time_horizon).

Public surface:

  - `create_discussion(...)` — persist a fresh draft session.
  - `list_discussions(...)` / `get_discussion(...)` — read APIs for the
    session list and detail pages.
  - `gather_market_context(...)` — pull a snapshot of TW market data
    (top movers, fundamentals, market-wide news with sentiment, TAIEX)
    once per round so every persona sees identical evidence.
  - `run_round(...)` — async generator yielding one event per turn so the
    HTTP layer can stream them as SSE. Persists each turn to the DB
    transactionally — partial rounds (e.g. user disconnect mid-stream)
    leave clean state.
  - `synthesize_conclusion(...)` — runs the synthesizer persona over the
    full transcript and stores the structured JSON result.
  - `delete_discussion(...)` — owner-only cascade delete.

Design choices:

  - Per-turn LLM output is parsed as JSON. If the persona drifts off
    format (returns prose) we fall back to `stance="supplement"` with the
    raw text as `content` rather than blowing up the round.
  - Token budget per turn is capped (`_MAX_TURN_TOKENS`) so a runaway
    persona can't drain quota.
  - Market context is built once per round and re-used across personas
    via in-memory cache (keyed on `(discussion_id, round)`) — avoids
    fanning out N × the same database queries.
  - Author auth is enforced at the router; this module assumes the
    caller has already verified ownership.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai.agents import all_persona_ids, get_agent_resolved
from ai.llm_router import stream_chat
from models.discussion import Discussion, DiscussionTurn

log = logging.getLogger(__name__)

# ── tuning knobs ────────────────────────────────────────────────────

_MAX_PERSONAS = 8           # safety cap so one discussion can't fan out 19 LLM calls/round
_MIN_PERSONAS = 2
_MAX_TURN_TOKENS = 600      # plenty for 2-3 paragraphs in zh-TW; cuts runaway costs
_MAX_TOPIC_CHARS = 500
_MAX_RULES_CHARS = 2000
_MAX_HISTORY_TURNS = 30     # how many prior turns to feed the next persona
_DEFAULT_TOP_MOVERS = 8

VALID_STANCES = ("agree", "dissent", "supplement")
DEFAULT_STANCE = "supplement"

# Status machine: draft → running → done. The "running" state lets the UI
# show a busy indicator and lets future code reject parallel rounds on the
# same discussion if we ever want to.
STATUS_DRAFT = "draft"
STATUS_RUNNING = "running"
STATUS_DONE = "done"


@dataclass(frozen=True)
class TurnEvent:
    """Single event emitted by `run_round` for SSE serialization."""
    type: str
    payload: dict[str, Any]


# ── validation ──────────────────────────────────────────────────────


def _normalize_persona_ids(ids: list[str]) -> list[str]:
    valid = set(all_persona_ids())
    cleaned: list[str] = []
    seen: set[str] = set()
    for pid in ids:
        if not isinstance(pid, str):
            continue
        pid = pid.strip()
        if pid in seen or pid not in valid:
            continue
        seen.add(pid)
        cleaned.append(pid)
    if len(cleaned) < _MIN_PERSONAS:
        raise ValueError(
            f"At least {_MIN_PERSONAS} valid persona IDs required; "
            f"got {len(cleaned)}",
        )
    if len(cleaned) > _MAX_PERSONAS:
        cleaned = cleaned[:_MAX_PERSONAS]
    return cleaned


def _validate_text(value: str, *, field: str, max_chars: int) -> str:
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{field} must not be empty")
    if len(text) > max_chars:
        raise ValueError(f"{field} must be ≤ {max_chars} chars (got {len(text)})")
    return text


# ── CRUD ────────────────────────────────────────────────────────────


async def create_discussion(
    db: AsyncSession,
    *,
    owner_id: uuid.UUID,
    topic: str,
    rules: str,
    persona_ids: list[str],
) -> Discussion:
    topic = _validate_text(topic, field="topic", max_chars=_MAX_TOPIC_CHARS)
    rules = _validate_text(rules, field="rules", max_chars=_MAX_RULES_CHARS)
    pids = _normalize_persona_ids(persona_ids)

    row = Discussion(
        owner_id=owner_id,
        topic=topic,
        rules=rules,
        persona_ids=pids,
        status=STATUS_DRAFT,
        current_round=0,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def list_discussions(
    db: AsyncSession, *, owner_id: uuid.UUID, limit: int = 50,
) -> list[Discussion]:
    stmt = (
        select(Discussion)
        .where(Discussion.owner_id == owner_id)
        .order_by(Discussion.created_at.desc())
        .limit(limit)
    )
    return list((await db.scalars(stmt)).all())


async def get_discussion(
    db: AsyncSession, *, discussion_id: uuid.UUID, owner_id: uuid.UUID,
) -> Discussion | None:
    stmt = select(Discussion).where(
        Discussion.id == discussion_id, Discussion.owner_id == owner_id,
    )
    return await db.scalar(stmt)


async def get_turns(
    db: AsyncSession, *, discussion_id: uuid.UUID,
) -> list[DiscussionTurn]:
    stmt = (
        select(DiscussionTurn)
        .where(DiscussionTurn.discussion_id == discussion_id)
        .order_by(
            DiscussionTurn.round.asc(), DiscussionTurn.turn_index.asc(),
        )
    )
    return list((await db.scalars(stmt)).all())


async def update_discussion(
    db: AsyncSession,
    discussion: Discussion,
    *,
    topic: str | None = None,
    rules: str | None = None,
    persona_ids: list[str] | None = None,
) -> Discussion:
    """Only allowed while status == draft. Once a round has run the
    persona roster + rules are frozen so prior turns stay coherent."""
    if discussion.status != STATUS_DRAFT:
        raise ValueError("Cannot edit a discussion that has already started")
    if topic is not None:
        discussion.topic = _validate_text(topic, field="topic", max_chars=_MAX_TOPIC_CHARS)
    if rules is not None:
        discussion.rules = _validate_text(rules, field="rules", max_chars=_MAX_RULES_CHARS)
    if persona_ids is not None:
        discussion.persona_ids = _normalize_persona_ids(persona_ids)
    discussion.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(discussion)
    return discussion


async def delete_discussion(
    db: AsyncSession, *, discussion_id: uuid.UUID, owner_id: uuid.UUID,
) -> bool:
    """Returns True if a row was deleted, False if not found / not owned.

    Explicitly removes child turn rows before the parent because SQLite
    (used in the test suite) ignores `ondelete="CASCADE"` unless
    `PRAGMA foreign_keys = ON` is set per-connection. Postgres with the
    cascading FK would also handle this, but doing it manually keeps
    the contract identical across both dialects.
    """
    parent = await db.scalar(
        select(Discussion).where(
            Discussion.id == discussion_id, Discussion.owner_id == owner_id,
        )
    )
    if parent is None:
        return False
    await db.execute(
        delete(DiscussionTurn).where(DiscussionTurn.discussion_id == discussion_id)
    )
    await db.execute(delete(Discussion).where(Discussion.id == discussion_id))
    await db.commit()
    return True


# ── market context ──────────────────────────────────────────────────


async def gather_market_context(
    db: AsyncSession, *, market: str = "TW", top_n: int = _DEFAULT_TOP_MOVERS,
) -> dict[str, Any]:
    """Build a structured snapshot of the market state for the personas.

    Each block degrades gracefully — if any data source is unavailable we
    return an empty list / None for that block instead of raising, so a
    transient outage doesn't block the discussion entirely.
    """
    ctx: dict[str, Any] = {
        "market": market,
        "captured_at": datetime.now(UTC).isoformat(),
        "top_gainers": [],
        "top_losers": [],
        "index": None,
        "news_sentiment": None,
    }

    # Top movers via the screener (TW-only for now; US fits later when we
    # add the same shape to us_market_service).
    if market == "TW":
        try:
            from services import tw_market_service
            rows = await tw_market_service.get_screener(limit=200, min_volume=1_000_000)
            scored = [r for r in rows if isinstance(r.get("change_pct"), (int, float))]
            scored.sort(key=lambda r: r["change_pct"], reverse=True)
            ctx["top_gainers"] = [_compact_screener_row(r) for r in scored[:top_n]]
            ctx["top_losers"] = [_compact_screener_row(r) for r in scored[-top_n:][::-1]]
        except Exception as exc:
            log.warning("discussion.context.screener_failed", extra={"error": str(exc)})

        try:
            from services import tw_market_service
            ctx["index"] = await tw_market_service.get_index()
        except Exception as exc:
            log.warning("discussion.context.index_failed", extra={"error": str(exc)})

    try:
        from services.news_sentiment_service import read_recent_market_sentiment
        ctx["news_sentiment"] = await read_recent_market_sentiment(
            db, market=market, limit=20, max_age_hours=48,
        )
    except Exception as exc:
        log.warning("discussion.context.news_sentiment_failed", extra={"error": str(exc)})

    return ctx


def _compact_screener_row(r: dict[str, Any]) -> dict[str, Any]:
    """Strip the screener row to just the fields a persona needs, so the
    LLM prompt stays compact (300 rows × 12 fields fills the context fast)."""
    return {
        "symbol": r.get("symbol"),
        "name": r.get("name_zh") or r.get("name"),
        "price": r.get("price"),
        "change_pct": r.get("change_pct"),
        "volume": r.get("volume"),
        "pe": r.get("pe_ratio"),
        "yield": r.get("dividend_yield"),
    }


# ── turn loop ───────────────────────────────────────────────────────


_TURN_PROMPT_TEMPLATE = (
    "你正在參加一場專家圓桌討論。你的角色身份請依系統提示扮演。\n\n"
    "## 主題\n{topic}\n\n"
    "## 共同規則\n{rules}\n\n"
    "## 市場現況\n```json\n{context}\n```\n\n"
    "## 先前發言\n{history}\n\n"
    "## 你現在的任務\n"
    "依照你扮演的角色立場，閱讀上述資料與先前發言後，"
    "輸出**合法 JSON**（不要包 markdown code fence）：\n"
    '{{"stance": "agree|dissent|supplement", "content": "你的發言"}}\n\n'
    "stance 規則：\n"
    "  - agree：完全同意先前共識，無新內容可補充。content 可留空或一句話致意。\n"
    "  - dissent：對某位專家的觀點有具體反對，必須點名是反對誰、為什麼。\n"
    "  - supplement：補充新資訊、新角度、新數據。\n\n"
    "content 必須遵守共同規則中的字數限制與引用要求。"
)


def _format_history(prior_turns: list[DiscussionTurn]) -> str:
    if not prior_turns:
        return "（你是本場第一位發言者）"
    lines = []
    for t in prior_turns[-_MAX_HISTORY_TURNS:]:
        body = t.content.strip() or "（同意，無補充）"
        lines.append(f"- 第{t.round}輪 · {t.persona_id} · {t.stance}：{body}")
    return "\n".join(lines)


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*\n(.*?)\n```\s*$", text, re.DOTALL)
    return fence.group(1).strip() if fence else text


def _parse_turn_response(raw: str) -> tuple[str, str]:
    """Return (stance, content). Falls back to (DEFAULT_STANCE, raw) when
    the model drifts off JSON format — better to record the prose than
    to lose the turn entirely."""
    cleaned = _strip_code_fence(raw)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return DEFAULT_STANCE, raw.strip()
    if not isinstance(data, dict):
        return DEFAULT_STANCE, raw.strip()
    stance = str(data.get("stance", "")).strip().lower()
    if stance not in VALID_STANCES:
        stance = DEFAULT_STANCE
    content = str(data.get("content", "")).strip()
    return stance, content


async def _ask_persona(
    db: AsyncSession,
    *,
    persona_id: str,
    topic: str,
    rules: str,
    context: dict[str, Any],
    prior_turns: list[DiscussionTurn],
    user_id: str | None,
) -> AsyncGenerator[dict, None]:
    """Yield raw stream events from one persona's turn. Caller assembles
    the deltas + parses the final JSON.
    """
    spec = await get_agent_resolved(db, persona_id)
    user_prompt = _TURN_PROMPT_TEMPLATE.format(
        topic=topic,
        rules=rules,
        context=json.dumps(context, ensure_ascii=False, indent=2),
        history=_format_history(prior_turns),
    )
    messages = [
        {"role": "system", "content": spec.system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    async for event in stream_chat(
        messages=messages,
        provider=spec.default_provider,
        model=spec.default_model,
        max_tokens=_MAX_TURN_TOKENS,
        temperature=0.4,
        db=db,
        user_id=user_id,
    ):
        yield event


async def run_round(
    db: AsyncSession,
    discussion: Discussion,
    *,
    user_id: str | None = None,
) -> AsyncGenerator[TurnEvent, None]:
    """Run one full round of discussion. Each persona is queried in order;
    each persona's response is persisted as a `DiscussionTurn` row.

    Emits the following event types:
      - round_start  {round}
      - context      {market_context}
      - turn_start   {round, turn_index, persona_id, persona_name}
      - delta        {round, turn_index, persona_id, text}
      - turn_end     {round, turn_index, persona_id, stance, content}
      - round_end    {round, turn_count}
      - error        {message}            (terminal)
    """
    round_number = discussion.current_round + 1
    discussion.status = STATUS_RUNNING
    discussion.current_round = round_number
    discussion.updated_at = datetime.now(UTC)
    await db.commit()

    yield TurnEvent("round_start", {"round": round_number})

    context = await gather_market_context(db)
    yield TurnEvent("context", {"context": context})

    prior_turns = await get_turns(db, discussion_id=discussion.id)

    for idx, persona_id in enumerate(discussion.persona_ids):
        spec = await get_agent_resolved(db, persona_id)
        yield TurnEvent("turn_start", {
            "round": round_number,
            "turn_index": idx,
            "persona_id": persona_id,
            "persona_name": spec.name,
        })

        assembled = ""
        try:
            async for event in _ask_persona(
                db,
                persona_id=persona_id,
                topic=discussion.topic,
                rules=discussion.rules,
                context=context,
                prior_turns=prior_turns,
                user_id=user_id,
            ):
                etype = event.get("type")
                if etype == "delta":
                    chunk = event.get("text", "")
                    assembled += chunk
                    yield TurnEvent("delta", {
                        "round": round_number,
                        "turn_index": idx,
                        "persona_id": persona_id,
                        "text": chunk,
                    })
                elif etype == "error":
                    yield TurnEvent("error", {
                        "message": event.get("message", "LLM error"),
                        "persona_id": persona_id,
                    })
                    assembled = assembled or "（此輪因 LLM 錯誤未取得回覆）"
                    break
        except Exception as exc:
            log.exception("discussion.turn.failed",
                          extra={"persona_id": persona_id, "round": round_number})
            yield TurnEvent("error", {
                "message": str(exc),
                "persona_id": persona_id,
            })
            assembled = assembled or "（此輪因例外中止）"

        stance, content = _parse_turn_response(assembled)
        turn_row = DiscussionTurn(
            discussion_id=discussion.id,
            round=round_number,
            turn_index=idx,
            persona_id=persona_id,
            stance=stance,
            content=content,
            citations=None,
        )
        db.add(turn_row)
        await db.commit()
        prior_turns.append(turn_row)

        yield TurnEvent("turn_end", {
            "round": round_number,
            "turn_index": idx,
            "persona_id": persona_id,
            "persona_name": spec.name,
            "stance": stance,
            "content": content,
        })

    discussion.status = STATUS_DRAFT  # ready for the next round
    discussion.updated_at = datetime.now(UTC)
    await db.commit()

    yield TurnEvent("round_end", {
        "round": round_number,
        "turn_count": len(discussion.persona_ids),
    })


# ── conclusion synthesizer ──────────────────────────────────────────


_SYNTHESIZER_SYSTEM = (
    "你是一位資深投資組合經理，主持本場圓桌討論。"
    "你的任務是閱讀全部專家發言，整理出可執行的結論。"
    "你不偏袒任何一位專家，而是從他們的共識與分歧中抓出最高勝率的觀點。"
)

_SYNTHESIZER_USER_TEMPLATE = (
    "## 討論主題\n{topic}\n\n"
    "## 討論規則\n{rules}\n\n"
    "## 市場現況\n```json\n{context}\n```\n\n"
    "## 全部發言（依序）\n{transcript}\n\n"
    "## 任務\n"
    "輸出**合法 JSON**（不要包 markdown code fence）：\n"
    "{{\n"
    '  "recommended_symbols": ["代號1", "代號2", ...],   // 最多5檔，要有市場共識且風險可控\n'
    '  "reasoning": "結論摘要（≤200字，引用至少2位專家）",\n'
    '  "risks": ["風險1", "風險2", ...],\n'
    '  "time_horizon": "short_term | medium_term | long_term",\n'
    '  "consensus_score": 0.0~1.0   // 0=完全分歧 1=完全共識\n'
    "}}\n"
)


def _format_transcript(turns: list[DiscussionTurn]) -> str:
    if not turns:
        return "（無發言）"
    lines = []
    for t in turns:
        body = t.content.strip() or "（同意，無補充）"
        lines.append(f"[第{t.round}輪/{t.persona_id}/{t.stance}] {body}")
    return "\n".join(lines)


def _safe_conclusion(raw: str) -> dict[str, Any]:
    cleaned = _strip_code_fence(raw)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return {
            "recommended_symbols": [],
            "reasoning": raw.strip()[:500] or "無法解析結論",
            "risks": [],
            "time_horizon": "short_term",
            "consensus_score": 0.0,
            "_parse_error": True,
        }
    if not isinstance(data, dict):
        return {
            "recommended_symbols": [],
            "reasoning": "無法解析結論",
            "risks": [],
            "time_horizon": "short_term",
            "consensus_score": 0.0,
            "_parse_error": True,
        }
    symbols = data.get("recommended_symbols") or []
    if not isinstance(symbols, list):
        symbols = []
    risks = data.get("risks") or []
    if not isinstance(risks, list):
        risks = []
    horizon = str(data.get("time_horizon", "short_term"))
    if horizon not in ("short_term", "medium_term", "long_term"):
        horizon = "short_term"
    try:
        consensus = float(data.get("consensus_score", 0.0))
    except (TypeError, ValueError):
        consensus = 0.0
    consensus = max(0.0, min(1.0, consensus))
    return {
        "recommended_symbols": [str(s).strip() for s in symbols if str(s).strip()][:5],
        "reasoning": str(data.get("reasoning", ""))[:1000],
        "risks": [str(r).strip() for r in risks if str(r).strip()][:10],
        "time_horizon": horizon,
        "consensus_score": round(consensus, 3),
    }


async def synthesize_conclusion(
    db: AsyncSession,
    discussion: Discussion,
    *,
    user_id: str | None = None,
    provider: str = "anthropic",
    model: str = "claude-haiku-4-5-20251001",
) -> dict[str, Any]:
    """Read every turn and produce a structured conclusion JSON. Stores
    the result on the Discussion row and flips status → done.

    The synthesizer is intentionally a fixed model rather than one of
    the persona LLMs — we want a neutral arbiter, not a re-skin of
    Buffett or Soros. Provider is overridable so deployments without an
    Anthropic key can swap to OpenAI.
    """
    turns = await get_turns(db, discussion_id=discussion.id)
    context = await gather_market_context(db)

    user_prompt = _SYNTHESIZER_USER_TEMPLATE.format(
        topic=discussion.topic,
        rules=discussion.rules,
        context=json.dumps(context, ensure_ascii=False, indent=2),
        transcript=_format_transcript(turns),
    )
    messages = [
        {"role": "system", "content": _SYNTHESIZER_SYSTEM},
        {"role": "user", "content": user_prompt},
    ]

    assembled = ""
    try:
        async for event in stream_chat(
            messages=messages,
            provider=provider,
            model=model,
            max_tokens=1024,
            temperature=0.2,
            db=db,
            user_id=user_id,
        ):
            if event.get("type") == "delta":
                assembled += event.get("text", "")
            elif event.get("type") == "error":
                log.warning(
                    "discussion.synthesize.llm_error",
                    extra={"message": event.get("message")},
                )
                break
    except Exception as exc:
        log.exception("discussion.synthesize.failed", extra={"id": str(discussion.id)})
        assembled = json.dumps({"reasoning": f"合成失敗：{exc}"})

    conclusion = _safe_conclusion(assembled)
    discussion.conclusion = conclusion
    discussion.status = STATUS_DONE
    discussion.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(discussion)
    return conclusion
