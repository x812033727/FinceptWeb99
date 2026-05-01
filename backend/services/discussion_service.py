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

import asyncio
import json
import logging
import re
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ai.agents import all_persona_ids
from ai.llm_router import stream_chat
from config import settings
from models.discussion import Discussion, DiscussionTurn
from models.discussion_round_context import DiscussionRoundContext

if TYPE_CHECKING:
    from ai.agents import AgentSpec

log = logging.getLogger(__name__)

# Recognises a 4-digit TW stock code as a standalone token. Used by
# `gather_market_context` to optionally pull per-symbol news sentiment for
# anything mentioned in the topic — discussion personas then see "this is
# what 2330 specifically is being said about" instead of just market-wide
# sentiment.
_TW_SYMBOL_RE = re.compile(r"\b(\d{4,6})\b")
_MAX_FOCUS_SYMBOLS = 5

# ── tuning knobs ────────────────────────────────────────────────────

_MAX_PERSONAS = 8           # safety cap so one discussion can't fan out 19 LLM calls/round
_MIN_PERSONAS = 2
# 2048 gives Chinese-output personas (BPE → ~3 tokens/char) ~600-700 chars of
# analysis after a reasoning preamble. With 1024 we still saw truncation mid-
# sentence on long-winded personas (Lynch / Buffett); the salvage path in
# `_parse_turn_response` recovers the partial content but raising the cap
# means it kicks in less often.
_MAX_TURN_TOKENS = 2048
_MAX_TOPIC_CHARS = 500
_MAX_RULES_CHARS = 2000
_MAX_HISTORY_TURNS = 30     # how many prior turns to feed the next persona

# Reasoning models surface their chain-of-thought wrapped in <think>...</think>
# blocks. `strip_think_blocks` removes them post-hoc; `_ThinkBlockFilter`
# below does the same as a streaming filter for SSE so the thinking never
# flashes across the chat UI in the first place.
# Both `strip_think_blocks` and the JSON-parse helpers live in
# `services.llm_parsing_utils` so other LLM-fed pipelines (news sentiment
# scorer, future tasks) reuse the same tolerant parser.
from services.llm_parsing_utils import (  # noqa: E402
    extract_json_object as _extract_json_object,
    loads_lenient as _loads_lenient,
    strip_code_fence as _strip_code_fence,
    strip_think_blocks,
)


class _ThinkBlockFilter:
    """Stateful streaming filter that drops text inside `<think>...</think>`
    tags as deltas arrive. Keeps the raw model chunks out of the user-
    visible SSE stream, so reasoning models don't flash hundreds of lines
    of internal monologue across the chat UI before settling on the final
    JSON.

    Buffers up to 16 chars of trailing text in case the next chunk
    completes a `<think>` opening tag straddling the boundary.
    """

    _OPEN = "<think>"
    _CLOSE = "</think>"
    _MAX_OPEN_LEN = len(_OPEN)
    _MAX_CLOSE_LEN = len(_CLOSE)

    def __init__(self) -> None:
        self._in_think = False
        self._buf = ""

    def feed(self, chunk: str) -> str:
        """Consume `chunk`; return the filtered text safe to forward."""
        self._buf += chunk
        out_parts: list[str] = []
        while self._buf:
            if self._in_think:
                end = self._buf.find(self._CLOSE)
                if end < 0:
                    # Still inside thinking; hold last few chars in case
                    # `</think>` straddles the next chunk boundary.
                    keep = min(len(self._buf), self._MAX_CLOSE_LEN - 1)
                    self._buf = self._buf[-keep:] if keep else ""
                    break
                self._in_think = False
                self._buf = self._buf[end + self._MAX_CLOSE_LEN:]
            else:
                start = self._buf.find(self._OPEN)
                if start < 0:
                    # No `<think>` opener found. Emit everything except a
                    # tail that is a *prefix* of `<think>` (e.g. `<thi`)
                    # which might complete on the next chunk. A bare `<`
                    # followed by unrelated text is fine to release.
                    hold = 0
                    max_check = min(len(self._buf), self._MAX_OPEN_LEN - 1)
                    for i in range(max_check, 0, -1):
                        if self._OPEN.startswith(self._buf[-i:]):
                            hold = i
                            break
                    if hold:
                        out_parts.append(self._buf[:-hold])
                        self._buf = self._buf[-hold:]
                    else:
                        out_parts.append(self._buf)
                        self._buf = ""
                    break
                out_parts.append(self._buf[:start])
                self._in_think = True
                self._buf = self._buf[start + self._MAX_OPEN_LEN:]
        return "".join(out_parts)

    def flush(self) -> str:
        """Emit any held-over text once the stream is done. If we're still
        inside a `<think>` block (model never closed it), drop the tail."""
        if self._in_think:
            self._buf = ""
            return ""
        out, self._buf = self._buf, ""
        return out
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


async def get_round_contexts(
    db: AsyncSession, *, discussion_id: uuid.UUID,
) -> list[DiscussionRoundContext]:
    """Per-round context snapshots for `discussion_id`, ascending."""
    stmt = (
        select(DiscussionRoundContext)
        .where(DiscussionRoundContext.discussion_id == discussion_id)
        .order_by(DiscussionRoundContext.round.asc())
    )
    return list((await db.scalars(stmt)).all())


async def _upsert_round_context(
    db: AsyncSession,
    *,
    discussion_id: uuid.UUID,
    round_number: int,
    context: dict[str, Any],
) -> None:
    """Persist the assembled context for one round. Idempotent on
    (discussion_id, round) so a defensive re-write (e.g. a stuck
    RUNNING row force-reset and re-run on the same round number)
    overwrites instead of failing the whole round commit. Caller
    is the only writer — `run_round` calls this once per round
    after `gather_market_context` returns."""
    payload = {
        "discussion_id": discussion_id,
        "round":         round_number,
        "context":       context,
    }
    dialect = db.bind.dialect.name if db.bind is not None else "postgresql"
    if dialect == "sqlite":
        stmt = sqlite_insert(DiscussionRoundContext).values(payload)
        stmt = stmt.on_conflict_do_update(
            index_elements=["discussion_id", "round"],
            set_={"context": stmt.excluded.context},
        )
    else:
        stmt = pg_insert(DiscussionRoundContext).values(payload)
        stmt = stmt.on_conflict_do_update(
            index_elements=["discussion_id", "round"],
            set_={"context": stmt.excluded.context},
        )
    await db.execute(stmt)
    await db.commit()


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


async def force_reset_status(
    db: AsyncSession, discussion: Discussion,
) -> None:
    """Atomically reset a discussion's status to DRAFT.

    Used by the round route to recover a discussion stuck in RUNNING
    (e.g. previous attempt died before its finally-block reset, or the
    reset commit silently failed). Atomic SQL UPDATE so it can't be
    affected by stale in-memory state. We manually mirror the new
    values onto the in-memory entity instead of `db.refresh()` —
    SQLAlchemy 2.0's ORM-level UPDATE with the default
    `synchronize_session='auto'` may expunge the entity from the
    session under PostgreSQL, after which `refresh()` raises
    "Instance is not persistent within this Session".
    """
    now = datetime.now(UTC)
    await db.execute(
        update(Discussion)
        .where(Discussion.id == discussion.id)
        .values(status=STATUS_DRAFT, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    discussion.status = STATUS_DRAFT
    discussion.updated_at = now


# ── market context ──────────────────────────────────────────────────


def extract_focus_symbols(text: str) -> list[str]:
    """Pull TW stock codes (4-6 digit) out of free text. Deduped, capped
    at `_MAX_FOCUS_SYMBOLS`. Used to enrich discussion context with
    per-symbol news sentiment when the topic names specific stocks
    ("找出 2330 / 2454 短線買點…")."""
    seen: list[str] = []
    for code in _TW_SYMBOL_RE.findall(text or ""):
        if code not in seen:
            seen.append(code)
        if len(seen) >= _MAX_FOCUS_SYMBOLS:
            break
    return seen


async def gather_market_context(
    db: AsyncSession,
    *,
    market: str = "TW",
    top_n: int = _DEFAULT_TOP_MOVERS,
    focus_symbols: list[str] | None = None,
) -> dict[str, Any]:
    """Build a structured snapshot of the market state for the personas.

    Each block degrades gracefully — if any data source is unavailable we
    return an empty list / None for that block instead of raising, so a
    transient outage doesn't block the discussion entirely.

    `focus_symbols` (typically extracted from the discussion topic via
    `extract_focus_symbols`) makes the context include per-symbol news
    sentiment alongside the market-wide aggregate. Empty list / None
    skips the per-symbol block.
    """
    ctx: dict[str, Any] = {
        "market": market,
        "captured_at": datetime.now(UTC).isoformat(),
        "top_gainers": [],
        "top_losers": [],
        "index": None,
        "news_sentiment": None,
        "per_symbol_news_sentiment": {},
        # International / cross-market news (Fed, FOMC, US markets,
        # global macro) translated into Chinese — populated by the
        # `ingest_news_international` cron writing rows under
        # `market='GLOBAL'`. Distinct from `news_sentiment` which is
        # the per-discussion-market aggregate. Lets a TW persona say
        # "FOMC 鷹派預期 → 對台股科技股不利" with actual data backing.
        "international_sentiment": None,
        # TW-only chip-metric blocks (PR #131). `top_foreign_buyers`
        # is the ranked net foreign buy over the last 5 trading days
        # (positive = accumulation, negative = distribution). `margin
        # _balance_trend` is the latest market-wide margin + short
        # balance, used as a leverage / retail-activity proxy. Both
        # are populated only when `market='TW'`; other markets keep
        # them None so the discussion prompt template doesn't have
        # to reason about empty per-market shapes.
        "top_foreign_buyers": [],
        "margin_balance_trend": None,
        # `top_revenue_growers` is the ranked YoY revenue growth in
        # the latest reported month (PR #133). Populated by
        # `tasks/ingest_revenue_tw`; empty until the first ingest
        # cycle finishes on a fresh deploy. TW-only.
        "top_revenue_growers": [],
        # `active_buybacks` (PR #189) — companies whose declared
        # buyback execution window covers today. Strong management
        # signal ("we'll spend cash on our own equity"); often
        # precedes / supports a price-floor narrative. TW-only.
        "active_buybacks": [],
        # `govt_bank_flow_5d` (PR #190) — eight-government-bank
        # net buy/sell summed across the last 5 trading days.
        # Quasi-public-sector flow signal. TW-only.
        "govt_bank_flow_5d": [],
        # `risk_warnings` (PR #192) — disposition / suspended /
        # high-day-trading-ratio summaries. Personas use these as
        # negative filters: don't recommend a 處置股 even if
        # screening criteria say buy. TW-only.
        "risk_warnings": {
            "active_dispositions": [],
            "recent_suspensions": [],
            "high_day_trading_ratio": [],
        },
        # `market_institutional_5d` (PR #193) — full-market three-
        # major-investor net flow summed by date. Personas read this
        # as "外資對台股整體買超 +250 億" — the headline-level
        # narrative complement to the per-symbol `top_foreign_buyers`
        # block. TW-only.
        "market_institutional_5d": [],
        # Each connector failure appends `{"source": "...", "error": "..."}`
        # so the personas (and the synthesizer) can mention "context was
        # incomplete" instead of confidently citing missing data. Logged
        # at ERROR level too so ops actually see broken connectors.
        "errors": [],
    }

    def _record_error(source: str, exc: Exception) -> None:
        log.error(
            "discussion.context.connector_failed",
            extra={"source": source, "error": str(exc)},
        )
        ctx["errors"].append({"source": source, "error": str(exc)})

    # Top movers via the screener (TW-only for now; US fits later when we
    # add the same shape to us_market_service).
    if market == "TW":
        try:
            from services import tw_market_service
            rows = await tw_market_service.get_screener(limit=200, min_volume=1_000_000)
            scored = [
                r for r in rows
                if isinstance(r.get("change_pct"), (int, float))
                and not _is_speculative_etf(r.get("symbol"))
            ]
            scored.sort(key=lambda r: r["change_pct"], reverse=True)
            ctx["top_gainers"] = [_compact_screener_row(r) for r in scored[:top_n]]
            ctx["top_losers"] = [_compact_screener_row(r) for r in scored[-top_n:][::-1]]
        except Exception as exc:
            _record_error("screener", exc)

        try:
            from services import tw_market_service
            # 30-day TAIEX history alongside the current quote. Lets
            # personas reference 大盤型態 ("TAIEX 連跌 5 日 -5%")
            # without burning an LLM tool call. Backed by the
            # `ingest_taiex_history` cron writing to ohlcv_daily under
            # symbol='_TAIEX'; empty `history` on fresh deploys is
            # fine — `get_index` returns DB-only for history so a
            # missing archive doesn't fall through to a TWSE call.
            ctx["index"] = await tw_market_service.get_index(history_days=30)
        except Exception as exc:
            _record_error("index", exc)

        # Chip metrics — both blocks are best-effort. Empty result
        # (cron hasn't run yet, fresh deploy) leaves the default and
        # personas just won't reference foreign flow / margin. We
        # don't push this through `tw_market_service` because there's
        # no per-symbol cache key to populate — these are aggregates.
        try:
            from services.ingest.repository import read_top_foreign_buyers
            ctx["top_foreign_buyers"] = _tag_industry(
                await read_top_foreign_buyers(
                    db, market="TW", days=5, limit=10,
                )
            )
        except Exception as exc:
            _record_error("top_foreign_buyers", exc)

        try:
            from services.ingest.repository import (
                read_market_margin_balance_trend,
            )
            ctx["margin_balance_trend"] = await read_market_margin_balance_trend(
                db, market="TW", days=5,
            )
        except Exception as exc:
            _record_error("margin_balance_trend", exc)

        try:
            from services.ingest.repository import read_top_revenue_growers
            ctx["top_revenue_growers"] = _tag_industry(
                await read_top_revenue_growers(db, market="TW", limit=10)
            )
        except Exception as exc:
            _record_error("top_revenue_growers", exc)

        # Active 庫藏股 buybacks (PR #189). Surfaced as a bullish-signal
        # block so personas can cite "公司自家正在買回" alongside
        # foreign-flow data. Cap at 10 — most days the active list is
        # 5-15 entries, and beyond that the prompt context bloats.
        try:
            from services.ingest.repository import read_active_buybacks
            ctx["active_buybacks"] = _tag_industry(
                await read_active_buybacks(db, market="TW", limit=10)
            )
        except Exception as exc:
            _record_error("active_buybacks", exc)

        # 八大行庫 5-day net flow (PR #190). Personas read this as
        # "國家隊昨天進場 +12 億 / 已連 3 日買超" alongside foreign
        # flow. Empty when ingest hasn't populated yet.
        try:
            from services.ingest.repository import read_recent_govt_bank_flow
            ctx["govt_bank_flow_5d"] = await read_recent_govt_bank_flow(
                db, market="TW", days=5,
            )
        except Exception as exc:
            _record_error("govt_bank_flow_5d", exc)

        # Risk warnings (PR #192). Three independent reads — wrap
        # each individually so one ingest hiccup doesn't blank the
        # other two. Each block is capped at 10-20 entries; personas
        # use these as negative filters so per-row detail isn't
        # required, just the symbol set.
        try:
            from services.ingest.repository import (
                read_active_dispositions,
                read_high_day_trading_ratio,
                read_recent_suspensions,
            )
            ctx["risk_warnings"] = {
                "active_dispositions": await read_active_dispositions(
                    db, market="TW", limit=20,
                ),
                "recent_suspensions": await read_recent_suspensions(
                    db, market="TW", days=7, limit=10,
                ),
                "high_day_trading_ratio": await read_high_day_trading_ratio(
                    db, market="TW", days=1, threshold=0.6, limit=20,
                ),
            }
        except Exception as exc:
            _record_error("risk_warnings", exc)

        # 全市場三大法人 5-day net flow (PR #193). Aggregated across
        # foreign / SITC / dealer; lets personas reference the
        # index-level narrative ("外資已連 3 日買超台股") alongside
        # the per-symbol `top_foreign_buyers`.
        try:
            from services.ingest.repository import read_recent_market_institutional
            ctx["market_institutional_5d"] = await read_recent_market_institutional(
                db, market="TW", days=5,
            )
        except Exception as exc:
            _record_error("market_institutional_5d", exc)

    try:
        from services.news_sentiment_service import read_recent_market_sentiment
        ctx["news_sentiment"] = await read_recent_market_sentiment(
            db, market=market, limit=20, max_age_hours=48,
        )
    except Exception as exc:
        _record_error("news_sentiment", exc)

    # International macro context — same reader, different market code.
    # Always pulled regardless of `market` arg because Fed / global
    # macro is relevant to TW personas just as much as US ones. Empty
    # block (zeros + empty headlines list) when ingest hasn't run yet,
    # which the personas already know to interpret as "no signal".
    try:
        from services.news_sentiment_service import read_recent_market_sentiment
        ctx["international_sentiment"] = await read_recent_market_sentiment(
            db, market="GLOBAL", limit=20, max_age_hours=48,
        )
    except Exception as exc:
        _record_error("international_sentiment", exc)

    if focus_symbols:
        try:
            from services.news_sentiment_service import read_symbol_sentiment
            for sym in focus_symbols[:_MAX_FOCUS_SYMBOLS]:
                rows = await read_symbol_sentiment(
                    db, market=market, symbol=sym, limit=10, max_age_hours=72,
                )
                if rows:
                    ctx["per_symbol_news_sentiment"][sym] = rows
        except Exception as exc:
            _record_error("per_symbol_sentiment", exc)

    return ctx


# TW listed leveraged / inverse / futures-tracking ETFs encode the
# kind in the trailing letter of the 5-digit code:
#   L = 2x leveraged (`00715L` 期街口布蘭特正 2)
#   U = futures-tracking (`00642U` 期元大 S&P 石油)
#   R = inverse (`00632R` 元大台灣 50 反 1)
# These products mean-revert hard the day after a spike, so them
# topping `top_gainers` consistently mis-leads personas into
# recommending tomorrow's reversal candidate. Plain index / dividend
# ETFs (`0050` `0056` `00878`) and ordinary stocks (`2330`) keep the
# trailing-digit-only shape and pass the filter.
_TW_SPECULATIVE_ETF_RE = re.compile(r"^\d{4,5}[LUR]$")


def _is_speculative_etf(symbol: Any) -> bool:
    if not isinstance(symbol, str):
        return False
    return bool(_TW_SPECULATIVE_ETF_RE.match(symbol))


def _compact_screener_row(r: dict[str, Any]) -> dict[str, Any]:
    """Strip the screener row to just the fields a persona needs, so the
    LLM prompt stays compact (300 rows × 12 fields fills the context fast)."""
    from services import tw_market_service
    sym = r.get("symbol")
    return {
        "symbol": sym,
        "name": r.get("name_zh") or r.get("name") or (
            tw_market_service.get_company_name(sym) if sym else None
        ),
        "industry": tw_market_service.get_industry(sym) if sym else None,
        "price": r.get("price"),
        "change_pct": r.get("change_pct"),
        "volume": r.get("volume"),
        "pe": r.get("pe_ratio"),
        "yield": r.get("dividend_yield"),
    }


def _tag_industry(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Enrich each row with `industry` + `name_zh` from the in-memory
    company-info maps. Rows already carrying these keys are passed
    through unchanged so callers that pre-tagged don't get clobbered.

    Used for the chip-metric and revenue-grower aggregator outputs
    so personas can see "外資買超 2330 (半導體業)" instead of just
    "2330" — the industry tag turns a raw list of codes into
    sector-flow analysis without an extra LLM tool call.
    """
    from services import tw_market_service
    out: list[dict[str, Any]] = []
    for r in rows:
        sym = r.get("symbol")
        enriched = dict(r)
        if sym:
            if "industry" not in enriched or not enriched["industry"]:
                ind = tw_market_service.get_industry(sym)
                if ind:
                    enriched["industry"] = ind
            if "name_zh" not in enriched or not enriched["name_zh"]:
                nm = tw_market_service.get_company_name(sym)
                if nm:
                    enriched["name_zh"] = nm
        out.append(enriched)
    return out


# ── turn loop ───────────────────────────────────────────────────────

# Schema annotation prepended to the JSON dump so weaker / smaller models
# (Haiku, GPT-4o-mini, Llama-3.3) actually use each ctx block. Without
# this they tend to fixate on `top_gainers` and ignore risk filters /
# institutional flow / buyback signals — defeating the cost of all the
# ingest crons feeding the context.
#
# Keep one line per block. Emphasise the negative-filter semantics for
# `risk_warnings` because that's where weak models most often go wrong
# (recommending a 處置股 because price action looks bullish).
_CONTEXT_SCHEMA_ANNOTATION = (
    "## 市場現況解讀提示\n"
    "下方 `## 市場現況` 的 JSON 包含多個訊號區塊，請依語意整合判讀，"
    "不要只挑 `top_gainers` 看：\n"
    "- top_gainers / top_losers：當日漲跌幅前 10（動能 + 籌碼面）。\n"
    "- index：大盤 (TAIEX) 即時報價 + 30 日歷史，用以判斷市場 regime。\n"
    "- news_sentiment：所屬市場整體新聞情緒（bullish/bearish/neutral 計數）。\n"
    "- per_symbol_news_sentiment：主題提及之個股新聞情緒。\n"
    "- international_sentiment：Fed / FOMC / 國際宏觀新聞情緒，影響台股風險偏好。\n"
    "- top_foreign_buyers：近 5 日外資累計淨買超前 10 名（已含產業別）。\n"
    "- margin_balance_trend：全市場融資 / 融券餘額趨勢（散戶槓桿與看空代理）。\n"
    "- top_revenue_growers：最新月份營收年增率前 10（基本面）。\n"
    "- active_buybacks：今日仍在執行庫藏股的公司，**強烈管理層信心訊號**。\n"
    "- govt_bank_flow_5d：八大行庫近 5 日累計買賣超（國家隊方向）。\n"
    "- risk_warnings：**負向過濾**——`active_dispositions`（處置股）、"
    "`recent_suspensions`（近期暫停交易）、`high_day_trading_ratio`"
    "（當沖比 >60%，投機過熱）。**禁止推薦中招的標的，即使其他訊號看多。**\n"
    "- market_institutional_5d：全市場三大法人近 5 日淨買賣超（大盤方向）。\n"
    "- errors：本次抓取的連接器錯誤清單；非空時務必聲明資料不完整。"
)


_TURN_PROMPT_TEMPLATE = (
    "你正在參加一場專家圓桌討論。你的角色身份請依系統提示扮演。\n\n"
    "## 語言規範（最重要）\n"
    "整段 content **必須用繁體中文（台灣用語）**。\n"
    "  - 用「漲停」不用「涨停」、用「資金」不用「资金」、用「電子」不用「电子」。\n"
    "  - 金融術語照台灣慣用：殖利率 / 本益比 / 三大法人 / 月營收年增。\n"
    "  - 不要混入簡體字，即使你的訓練資料偏向簡體也要轉繁。\n\n"
    "## 主題\n{topic}\n\n"
    "## 共同規則\n{rules}\n\n"
    + _CONTEXT_SCHEMA_ANNOTATION + "\n\n"
    "## 市場現況\n```json\n{context}\n```\n\n"
    "## 先前發言\n{history}\n\n"
    "## 你現在的任務\n"
    "依照你扮演的角色立場，閱讀上述資料與先前發言後，"
    "**直接輸出合法 JSON**（不要包 markdown code fence、不要在 JSON 之前或之後加任何解釋文字）：\n"
    '{{"stance": "agree|dissent|supplement", "content": "你的發言"}}\n\n'
    "stance 規則：\n"
    "  - agree：完全同意先前共識，無新內容可補充。content 可留空或一句話致意。\n"
    "  - dissent：對某位專家的觀點有具體反對，必須點名是反對誰、為什麼。\n"
    "  - supplement：補充新資訊、新角度、新數據。\n\n"
    "## 排版規範\n"
    "為了讓使用者快速抓重點，content 內請用 markdown 強調語法：\n"
    "  - 關鍵結論、重要數字、目標價、停損點、股票代號 → 用 **粗體**\n"
    "    例如：**台積電 (2330)**、**目標價 650 元**、**停損 565 元**、**+5.2%**\n"
    "  - 段落之間用空行分開，不要寫成一整塊牆。\n"
    "  - 條列重點時用 `- ` 開頭，每點獨立一行。\n"
    "  - **不要**用 markdown 標題 (`#`、`##`),也不要用程式碼區塊。\n\n"
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


# Matches the opening of a `"content": "` field. Used by the truncation
# salvage path — the persona's content always lives behind this key in
# our prompt template, so finding it gives us a reliable extraction
# anchor when the JSON wrapper got cut off mid-string.
_CONTENT_OPEN_RE = re.compile(r'"content"\s*:\s*"')
_STANCE_RE = re.compile(r'"stance"\s*:\s*"([^"]*)"')

# Standard JSON single-char escape map. `\u####` is handled inline
# because it consumes a variable number of input characters.
_JSON_ESCAPE_MAP = {
    "n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f",
    '"': '"', "\\": "\\", "/": "/",
}


def _decode_partial_json_string(s: str) -> str:
    """Decode JSON escape sequences inside a string fragment that has
    no closing quote (because the LLM hit max_tokens mid-content).
    Stops at the first unescaped `"` (legitimate end) or end of input.
    Drops a trailing partial escape (lone `\\` or incomplete `\\u####`)
    so the rendered text doesn't carry a dangling backslash."""
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            if i + 1 >= n:
                break
            esc = s[i + 1]
            mapped = _JSON_ESCAPE_MAP.get(esc)
            if mapped is not None:
                out.append(mapped)
                i += 2
                continue
            if esc == "u":
                if i + 6 > n:
                    break
                try:
                    out.append(chr(int(s[i + 2:i + 6], 16)))
                    i += 6
                except ValueError:
                    out.append(s[i:i + 2])
                    i += 2
                continue
            out.append(esc)
            i += 2
        elif c == '"':
            break
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _salvage_truncated_json(text: str) -> tuple[str, str] | None:
    """Recover (stance, content) from a JSON wrapper that got truncated
    when the LLM hit max_tokens mid-string.

    Triggered after `_extract_json_object` fails to find a balanced
    `{...}` — the closing `}` never appeared because the model ran out
    of budget while writing the content. The wrapper looks like
    `{"stance":"supplement","content":"...partial text`. We pull stance
    out by regex (it's a short word that almost always finishes before
    truncation hits) and take everything after `"content":"` as the
    content body, JSON-decoding the standard escapes so embedded `\\n`
    becomes a real newline.

    Returns None when neither a `"content":"` opener nor any sign of the
    JSON wrapper is present — the caller falls back to raw-text mode.
    """
    content_match = _CONTENT_OPEN_RE.search(text)
    if content_match is None:
        return None
    stance_match = _STANCE_RE.search(text[:content_match.start()])
    stance = stance_match.group(1) if stance_match else DEFAULT_STANCE
    content = _decode_partial_json_string(text[content_match.end():])
    return stance, content


def _parse_turn_response(raw: str) -> tuple[str, str]:
    """Return (stance, content). Falls back to (DEFAULT_STANCE, cleaned_raw)
    when the model drifts off JSON format — better to record the prose
    than to lose the turn entirely.

    Parsing is layered to survive the most common LLM shape drifts:
      1. strip `<think>...</think>` reasoning blocks
      2. strip surrounding markdown code fence
      3. parse with `strict=False` so embedded newlines / tabs in
         Chinese content don't blow up json
      4. if that fails, salvage the first balanced `{...}` object from
         surrounding prose (handles "Here is my analysis: {...}" cases)
      5. if no balanced object exists (LLM hit max_tokens mid-string so
         the closing `"}` never arrived), regex-extract the partial
         `content` field — keeps most of the persona's analysis instead
         of surfacing the raw `{"stance":"...","content":"...` wrapper
         to the user.
    """
    no_thinking = strip_think_blocks(raw)
    cleaned = _strip_code_fence(no_thinking)
    data: Any | None = None
    try:
        data = _loads_lenient(cleaned)
    except json.JSONDecodeError:
        salvaged = _extract_json_object(cleaned)
        if salvaged is not None:
            try:
                data = _loads_lenient(salvaged)
            except json.JSONDecodeError:
                data = None
    if isinstance(data, dict):
        stance = str(data.get("stance", "")).strip().lower()
        if stance not in VALID_STANCES:
            stance = DEFAULT_STANCE
        content = str(data.get("content", "")).strip()
        return stance, content

    truncated = _salvage_truncated_json(cleaned)
    if truncated is not None:
        stance, content = truncated
        stance = stance.strip().lower()
        if stance not in VALID_STANCES:
            stance = DEFAULT_STANCE
        content = content.strip()
        if content:
            return stance, content
    return DEFAULT_STANCE, no_thinking.strip()


async def _resolve_persona_specs(
    db: AsyncSession, persona_ids: list[str],
) -> dict[str, "AgentSpec"]:
    """Batch-load admin overrides for the entire roster in one DB round-trip,
    then merge with compiled defaults. Replaces N per-persona
    `get_agent_resolved` calls during a round.
    """
    from ai.agents import AgentSpec, get_agent
    from models.persona_override import PersonaOverride

    if not persona_ids:
        return {}
    rows = (await db.execute(
        select(PersonaOverride).where(PersonaOverride.persona_id.in_(persona_ids))
    )).scalars().all()
    overrides = {r.persona_id: r for r in rows}

    out: dict[str, AgentSpec] = {}
    for pid in persona_ids:
        try:
            base = get_agent(pid)
        except ValueError:
            log.warning("discussion.persona.unknown", extra={"persona_id": pid})
            continue
        ov = overrides.get(pid)
        if ov is None:
            out[pid] = base
        else:
            out[pid] = AgentSpec(
                name=base.name,
                description=base.description,
                system_prompt=base.system_prompt,
                default_provider=ov.provider,
                default_model=ov.model,
            )
    return out


async def _ask_persona(
    db: AsyncSession,
    *,
    spec: "AgentSpec",
    topic: str,
    rules: str,
    context: dict[str, Any],
    prior_turns: list[DiscussionTurn],
    user_id: str | None,
) -> AsyncGenerator[dict, None]:
    """Yield raw stream events from one persona's turn. Caller assembles
    the deltas + parses the final JSON.

    Takes a pre-resolved `AgentSpec` so callers can batch-load the
    persona roster's overrides up-front (avoiding an N+1 round trip
    inside the per-persona loop).
    """
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
    provider_override: str | None = None,
    model_override: str | None = None,
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
    # Atomic SQL increment so the round counter can't drift from what's
    # in the DB. The previous in-memory `discussion.current_round =
    # round_number` + commit relied on SQLAlchemy attribute-tracking +
    # session attachment + autoflush behaviour, all of which can fail
    # silently (e.g. detached entity from a stale request, autoflush=False
    # interaction). Doing it as `UPDATE ... SET current_round =
    # current_round + 1 RETURNING current_round` is bulletproof: one
    # round-trip, atomic, returns the new value directly. We mirror the
    # new values onto the in-memory entity manually — `db.refresh()`
    # would raise "Instance is not persistent" after an ORM update that
    # SQLAlchemy 2.0 chose to synchronize via expunge under PostgreSQL.
    now = datetime.now(UTC)
    result = await db.execute(
        update(Discussion)
        .where(Discussion.id == discussion.id)
        .values(
            current_round=Discussion.current_round + 1,
            status=STATUS_RUNNING,
            updated_at=now,
        )
        .returning(Discussion.current_round)
        .execution_options(synchronize_session=False)
    )
    round_number = result.scalar_one()
    await db.commit()
    discussion.current_round = round_number
    discussion.status = STATUS_RUNNING
    discussion.updated_at = now
    log.info(
        "discussion.round.started",
        extra={"discussion_id": str(discussion.id), "round": round_number},
    )

    yield TurnEvent("round_start", {"round": round_number})

    # Try-finally guarantees status returns to DRAFT even if an
    # unexpected exception fires below (e.g. a transient DB commit failure
    # while persisting a turn). Without this the discussion would be
    # permanently stuck in RUNNING and the router would reject every
    # subsequent /round call.
    try:
        focus = extract_focus_symbols(discussion.topic)
        context = await gather_market_context(db, focus_symbols=focus)
        # Snapshot the assembled context so re-opening the discussion
        # later can show "what data the personas saw at the time".
        # Failure to persist is non-fatal — we still want the round
        # to proceed and the personas to reply; the missing snapshot
        # just means this round won't be replayable from the archive.
        try:
            await _upsert_round_context(
                db,
                discussion_id=discussion.id,
                round_number=round_number,
                context=context,
            )
        except Exception as exc:
            log.warning(
                "discussion.round.context_snapshot_failed",
                extra={
                    "discussion_id": str(discussion.id),
                    "round": round_number,
                    "error": str(exc),
                },
            )
        yield TurnEvent("context", {"context": context})

        prior_turns = await get_turns(db, discussion_id=discussion.id)
        # Resolve persona timeout via the runtime config service so admins
        # can retune it from the UI without redeploying. Falls back to
        # the compiled-in setting on any resolver failure.
        try:
            from services.runtime_config_service import get_int as _get_int
            persona_timeout = await _get_int(db, "DISCUSSION_PERSONA_TIMEOUT_SECONDS")
        except Exception:
            persona_timeout = settings.DISCUSSION_PERSONA_TIMEOUT_SECONDS

        # Batch-load persona overrides up front so the per-persona loop
        # doesn't make N round-trips to the persona_overrides table.
        specs_by_id = await _resolve_persona_specs(db, list(discussion.persona_ids))
        # System-task-level LLM override: when the auto-run scheduler
        # passes (provider, model), every persona in this round is
        # rebuilt to point at that one LLM. Lets admins set a single
        # cheap model for the daily auto-run via SystemTasksCard
        # without having to override each persona individually.
        # PersonasCard overrides on individual personas are ignored
        # for this run only.
        if provider_override and model_override:
            from ai.agents import AgentSpec
            specs_by_id = {
                pid: AgentSpec(
                    name=spec.name,
                    description=spec.description,
                    system_prompt=spec.system_prompt,
                    default_provider=provider_override,
                    default_model=model_override,
                )
                for pid, spec in specs_by_id.items()
            }

        for idx, persona_id in enumerate(discussion.persona_ids):
            spec = specs_by_id.get(persona_id)
            if spec is None:
                # persona_id no longer recognised by ai.agents (shouldn't
                # happen since personas are compiled-in, but defensive)
                yield TurnEvent("error", {
                    "message": f"unknown persona: {persona_id}",
                    "persona_id": persona_id,
                })
                continue

            yield TurnEvent("turn_start", {
                "round": round_number,
                "turn_index": idx,
                "persona_id": persona_id,
                "persona_name": spec.name,
            })

            assembled = ""
            usage_seen: dict[str, int] | None = None
            # Wrap the persona's turn in asyncio.timeout so a single stuck
            # provider can't hang the whole round indefinitely. On timeout
            # we emit an error event, persist a placeholder turn, and
            # proceed to the next persona — same pattern as an LLM error.
            # Filter out `<think>...</think>` content as it streams so
            # reasoning models (deepseek-r1, gpt-o1, qwen-3) don't flash
            # internal monologue across the chat UI. The full unfiltered
            # text is still kept in `assembled` so JSON parsing still
            # sees what the model sent (and `_parse_turn_response` strips
            # think blocks again for safety / persistence).
            think_filter = _ThinkBlockFilter()
            try:
                async with asyncio.timeout(persona_timeout):
                    async for event in _ask_persona(
                        db,
                        spec=spec,
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
                            visible = think_filter.feed(chunk)
                            if visible:
                                yield TurnEvent("delta", {
                                    "round": round_number,
                                    "turn_index": idx,
                                    "persona_id": persona_id,
                                    "text": visible,
                                })
                        elif etype == "usage":
                            # Capture the provider's reported token counts
                            # so we can write a per-persona LLMUsageEvent
                            # row after the turn settles. Without this,
                            # the bulk of discussion cost (N personas ×
                            # rounds) was invisible in UsageCard.
                            usage_seen = {
                                "prompt_tokens": int(event.get("prompt_tokens", 0)),
                                "completion_tokens": int(event.get("completion_tokens", 0)),
                            }
                        elif etype == "error":
                            yield TurnEvent("error", {
                                "message": event.get("message", "LLM error"),
                                "persona_id": persona_id,
                            })
                            assembled = assembled or "（此輪因 LLM 錯誤未取得回覆）"
                            break
            except TimeoutError:
                log.warning(
                    "discussion.turn.timeout",
                    extra={"persona_id": persona_id, "round": round_number,
                           "timeout_s": persona_timeout},
                )
                yield TurnEvent("error", {
                    "message": f"persona timeout after {persona_timeout}s",
                    "persona_id": persona_id,
                })
                assembled = assembled or f"（此輪因 LLM {persona_timeout}s 內未回覆而中止）"
            except Exception as exc:
                log.exception("discussion.turn.failed",
                              extra={"persona_id": persona_id, "round": round_number})
                yield TurnEvent("error", {
                    "message": str(exc),
                    "persona_id": persona_id,
                })
                assembled = assembled or "（此輪因例外中止）"

            # Whether the stream finished cleanly, errored, or timed out,
            # flush any text the think-filter is still buffering. If the
            # tail is inside an open <think> block it gets dropped (the
            # model never closed the tag, so we never want to show it).
            tail = think_filter.flush()
            if tail:
                yield TurnEvent("delta", {
                    "round": round_number,
                    "turn_index": idx,
                    "persona_id": persona_id,
                    "text": tail,
                })

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

            # Record the persona's LLM usage. Tagged with the actual
            # persona_id (not "_system:..." like sentiment/synthesizer)
            # so the admin UsageCard breakdown can attribute cost per
            # persona/round. Skipped if the provider didn't emit a
            # usage event (some openai-compat backends don't).
            if usage_seen is not None:
                from services.llm_usage_service import record_usage
                await record_usage(
                    db,
                    user_id=user_id,
                    provider=spec.default_provider,
                    model=spec.default_model,
                    persona_id=persona_id,
                    prompt_tokens=usage_seen["prompt_tokens"],
                    completion_tokens=usage_seen["completion_tokens"],
                )

            yield TurnEvent("turn_end", {
                "round": round_number,
                "turn_index": idx,
                "persona_id": persona_id,
                "persona_name": spec.name,
                "stance": stance,
                "content": content,
            })

        yield TurnEvent("round_end", {
            "round": round_number,
            "turn_count": len(discussion.persona_ids),
        })
    finally:
        # Always reset to DRAFT so the next round attempt isn't blocked
        # by the router's "round in progress" guard. Use the same atomic
        # SQL UPDATE pattern as round-start so the reset can't fail
        # silently the way an in-memory mutation can. Wrap in its own
        # try so a reset failure doesn't mask the body exception.
        try:
            now = datetime.now(UTC)
            await db.execute(
                update(Discussion)
                .where(Discussion.id == discussion.id)
                .values(status=STATUS_DRAFT, updated_at=now)
                .execution_options(synchronize_session=False)
            )
            await db.commit()
            discussion.status = STATUS_DRAFT
            discussion.updated_at = now
            log.info(
                "discussion.round.ended",
                extra={
                    "discussion_id": str(discussion.id),
                    "current_round": discussion.current_round,
                },
            )
        except Exception:
            log.exception(
                "discussion.run_round.status_reset_failed",
                extra={"discussion_id": str(discussion.id)},
            )


# ── conclusion synthesizer ──────────────────────────────────────────


_SYNTHESIZER_SYSTEM = (
    "你是一位資深投資組合經理，主持本場圓桌討論。"
    "你的任務是閱讀全部專家發言，整理出可執行的結論。"
    "你不偏袒任何一位專家，而是從他們的共識與分歧中抓出最高勝率的觀點。\n"
    "**所有輸出必須使用繁體中文（台灣用語）**，禁用簡體字。"
)

_SYNTHESIZER_USER_TEMPLATE = (
    "## 討論主題\n{topic}\n\n"
    "## 討論規則\n{rules}\n\n"
    + _CONTEXT_SCHEMA_ANNOTATION + "\n\n"
    "## 市場現況\n```json\n{context}\n```\n\n"
    "## 全部發言（依序）\n{transcript}\n\n"
    "## 任務\n"
    "**直接輸出合法 JSON**（不要包 markdown code fence、不要在 JSON 之前或之後加任何解釋、"
    "不要寫 // 或 /* */ 註解）：\n"
    "{{\n"
    '  "recommended_symbols": ["2330", "0050"],\n'
    '  "reasoning": "結論摘要，≤200字，繁體中文，引用至少2位專家",\n'
    '  "risks": ["風險1", "風險2"],\n'
    '  "time_horizon": "short_term",\n'
    '  "consensus_score": 0.7\n'
    "}}\n\n"
    "欄位規則：\n"
    "- recommended_symbols：最多 5 檔，要有市場共識且風險可控\n"
    "- time_horizon：只能是 short_term / medium_term / long_term 三選一\n"
    "- consensus_score：0.0 到 1.0 之間的數字，0 代表完全分歧，1 代表完全共識\n"
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
    cleaned = _strip_code_fence(strip_think_blocks(raw))
    data: Any | None = None
    try:
        data = _loads_lenient(cleaned)
    except json.JSONDecodeError:
        salvaged = _extract_json_object(cleaned)
        if salvaged is not None:
            try:
                data = _loads_lenient(salvaged)
            except json.JSONDecodeError:
                data = None
    if not isinstance(data, dict):
        return {
            "recommended_symbols": [],
            "reasoning": raw.strip()[:500] or "無法解析結論",
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
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Read every turn and produce a structured conclusion JSON. Stores
    the result on the Discussion row and flips status → done.

    The synthesizer is intentionally a fixed task (not one of the persona
    LLMs) — we want a neutral arbiter, not a re-skin of Buffett or Soros.
    Provider/model default to whatever the admin selected for the
    `discussion_synthesizer` system task; pass explicit values in tests
    or one-off calls to bypass the resolver.
    """
    if provider is None or model is None:
        from services.system_task_config_service import resolve as _resolve_task
        r_provider, r_model = await _resolve_task(db, "discussion_synthesizer")
        provider = provider or r_provider
        model = model or r_model
    turns = await get_turns(db, discussion_id=discussion.id)
    focus = extract_focus_symbols(discussion.topic)
    context = await gather_market_context(db, focus_symbols=focus)

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
    usage_seen: dict[str, int] | None = None
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
            etype = event.get("type")
            if etype == "delta":
                assembled += event.get("text", "")
            elif etype == "usage":
                usage_seen = {
                    "prompt_tokens": int(event.get("prompt_tokens", 0)),
                    "completion_tokens": int(event.get("completion_tokens", 0)),
                }
            elif etype == "error":
                log.warning(
                    "discussion.synthesize.llm_error",
                    extra={"message": event.get("message")},
                )
                break
    except Exception as exc:
        log.exception("discussion.synthesize.failed", extra={"id": str(discussion.id)})
        assembled = json.dumps({"reasoning": f"合成失敗：{exc}"})

    if usage_seen is not None:
        from services.llm_usage_service import record_usage
        await record_usage(
            db,
            user_id=user_id,
            provider=provider,
            model=model,
            persona_id="_system:discussion_synthesizer",
            prompt_tokens=usage_seen["prompt_tokens"],
            completion_tokens=usage_seen["completion_tokens"],
        )

    conclusion = _safe_conclusion(assembled)
    discussion.conclusion = conclusion
    discussion.status = STATUS_DONE
    discussion.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(discussion)
    return conclusion
