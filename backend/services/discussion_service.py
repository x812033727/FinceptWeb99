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
  - Token budget per turn is capped (`DISCUSSION_TURN_MAX_TOKENS`) so a runaway
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
from datetime import UTC, date, datetime, timedelta
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

# Symbol-extraction patterns for `extract_focus_symbols`. Each market
# uses a different shape:
#
#   - TW: 4-6 digit numeric codes. Years like "2026" land in the same
#     space as 2330 / 0050 / 00878, so the year filter below is the
#     guard that keeps `read_symbol_sentiment` queries from polluting
#     a US/global topic with TW lookups.
#   - US: cashtag `$AAPL` always matches. Bare uppercase tickers
#     (`AAPL`, `MSFT`) are only honoured when the discussion's market
#     is US — otherwise common English words like "AND" / "FOR" /
#     "USD" would be mistaken for tickers in TW topics.
#   - GLOBAL / crypto: matched against the curated Top-20 universe in
#     `data/crypto/symbols.py` so `BTC` triggers but `ETH-USD` doesn't.
_TW_SYMBOL_RE = re.compile(r"(?<![\w])(\d{4,6})(?![\w])")
_CASHTAG_RE = re.compile(r"\$([A-Z]{1,5})\b")
_BARE_US_TICKER_RE = re.compile(r"\b([A-Z]{1,5})\b")
# Year-like 4-digit numbers — keep generous; TW codes never overlap.
_YEAR_MIN = 1900
_YEAR_MAX = 2099
# Common 1-5 letter uppercase tokens that look like US tickers but
# aren't. Prevents `discussion_service` from sentimening "USD news".
# Not exhaustive — the topic field is short, false positives are
# cheap (worst case: an empty per-symbol news block), and adding new
# entries here is a 1-line patch.
_US_TICKER_STOPWORDS = frozenset({
    "A", "AI", "AN", "ARE", "AS", "AT", "BE", "BY", "CAN", "CEO",
    "CFO", "CTO", "DCF", "DXY", "EPS", "ETF", "EU", "FED", "FOMC",
    "FOR", "FX", "GDP", "GET", "I", "IF", "IN", "IPO", "IS", "IT",
    "M2", "NEW", "NO", "NOT", "OF", "ON", "OR", "PE", "ROE",
    "SEC", "SP", "SPX", "TBD", "THE", "TO", "UK", "US", "USA", "USD",
    "VAR", "VIX", "WTI", "YOU",
})

_VALID_MARKETS = ("TW", "US", "GLOBAL")
_DEFAULT_MARKET = "TW"
_MAX_FOCUS_SYMBOLS = 5

# ── tuning knobs ────────────────────────────────────────────────────

_MAX_PERSONAS = 8           # safety cap so one discussion can't fan out 19 LLM calls/round
_MIN_PERSONAS = 2
# Per-persona-turn `max_tokens` is now admin-tunable via the
# RuntimeTunablesCard (`DISCUSSION_TURN_MAX_TOKENS`, default 8192). The
# default gives reasoning models (MiniMax-M2.7, DeepSeek-R1) enough
# headroom for chain-of-thought (~3-5K tokens) before the visible
# Chinese content (~600-700 chars × 3 BPE = ~2K tokens) is emitted. With
# 2048 the budget was exhausted entirely on reasoning and
# `finish_reason="length"` arrived with zero content (see PR #225 for the
# silent-empty-response diagnosis). Non-thinking personas still emit only
# what they need; the cap is purely an output-side ceiling.
_MAX_TOPIC_CHARS = 500
_MAX_RULES_CHARS = 2000
_MAX_HISTORY_TURNS = 30     # how many prior turns to feed the next persona

# When the transcript gets long, only the most recent
# `_FULL_HISTORY_TURNS` turns are passed verbatim. Older turns are
# rendered as a single-line summary ("第1輪/buffett/supplement: 看好 2330,
# 目標價...") capped at `_HISTORY_SUMMARY_CHARS` chars so the prompt
# budget doesn't balloon at round 5 with 8 personas (40 turns × ~300
# Chinese chars × 3 BPE tokens ≈ 36K input tokens just for history).
_FULL_HISTORY_TURNS = 8
_HISTORY_SUMMARY_CHARS = 120

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


def _normalize_market(value: str | None) -> str:
    """Coerce + validate a market string. Falls back to `_DEFAULT_MARKET`
    when None / empty so legacy clients that never sent the field keep
    working. Unknown values raise ValueError."""
    if value is None or not str(value).strip():
        return _DEFAULT_MARKET
    market = str(value).strip().upper()
    if market not in _VALID_MARKETS:
        raise ValueError(
            f"market must be one of {_VALID_MARKETS}; got {market!r}",
        )
    return market


# ── CRUD ────────────────────────────────────────────────────────────


async def create_discussion(
    db: AsyncSession,
    *,
    owner_id: uuid.UUID,
    topic: str,
    rules: str,
    persona_ids: list[str],
    market: str | None = None,
    as_of_date: date | None = None,
    sweep_id: uuid.UUID | None = None,
) -> Discussion:
    topic = _validate_text(topic, field="topic", max_chars=_MAX_TOPIC_CHARS)
    rules = _validate_text(rules, field="rules", max_chars=_MAX_RULES_CHARS)
    pids = _normalize_persona_ids(persona_ids)
    market = _normalize_market(market)
    if as_of_date is not None:
        # Reject future dates: there's no historical data to filter
        # to, and the verifier's "next 5 trading days" window won't
        # exist yet. Today is allowed (degenerate live mode).
        from datetime import date as _date_today
        if as_of_date > _date_today.today():
            raise ValueError(
                "as_of_date cannot be in the future "
                f"(got {as_of_date.isoformat()})",
            )

    row = Discussion(
        owner_id=owner_id,
        topic=topic,
        rules=rules,
        persona_ids=pids,
        market=market,
        status=STATUS_DRAFT,
        current_round=0,
        as_of_date=as_of_date,
        sweep_id=sweep_id,
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
    market: str | None = None,
) -> Discussion:
    """Only allowed while status == draft. Once a round has run the
    persona roster + rules + market are frozen so prior turns stay
    coherent (a TW 籌碼 round followed by a US fundamentals round in
    the same discussion would be incoherent)."""
    if discussion.status != STATUS_DRAFT:
        raise ValueError("Cannot edit a discussion that has already started")
    if topic is not None:
        discussion.topic = _validate_text(topic, field="topic", max_chars=_MAX_TOPIC_CHARS)
    if rules is not None:
        discussion.rules = _validate_text(rules, field="rules", max_chars=_MAX_RULES_CHARS)
    if persona_ids is not None:
        discussion.persona_ids = _normalize_persona_ids(persona_ids)
    if market is not None:
        discussion.market = _normalize_market(market)
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


# ── user injection ─────────────────────────────────────────────────
#
# Between rounds the discussion owner can drop a "user_input" turn
# into the transcript so the next round's personas have to react to
# it. Use case: after round 1 the user wants to refocus the debate
# ("把目光放在 2330 而不是大盤"), or feed in extra context the
# personas missed ("剛剛 Q2 EPS 公布 +35% YoY"). This is an
# alternative to editing the discussion's `topic` mid-stream — that
# would silently rewrite history and confuse later rounds.
#
# Stored as a normal `DiscussionTurn` with `persona_id="_user"` and
# `stance="user_input"` so `_format_history` picks it up naturally;
# the only special-case is `_format_history` rendering it without
# the "stance" suffix (it's a directive, not an analyst opinion).

USER_PERSONA_ID = "_user"
USER_INJECTION_STANCE = "user_input"
_MAX_USER_INJECTION_CHARS = 2000


async def inject_user_message(
    db: AsyncSession, discussion: Discussion, *, content: str,
) -> DiscussionTurn:
    """Append a user-input turn to the discussion's current round.

    Constraints:
      - status must NOT be `running` — mid-round injection would
        race the active persona stream and leave the new turn out
        of order. `draft` (round-completed, ready for next round)
        and `done` (concluded, but extensible — the post-mortem
        flow injects against a concluded discussion to seed the
        next round of self-critique) are both fine.
      - `current_round` must be ≥ 1 (no point injecting before the
        first round has run — the user can just edit `topic` /
        `rules` while the discussion is still untouched).
      - content non-empty + capped at `_MAX_USER_INJECTION_CHARS`.

    The injected turn lands at `round=current_round` with
    `turn_index = max_existing_index + 1` so it sits AFTER the
    last persona's reply for that round. Personas in the next round
    will see it via `prior_turns` ordering.
    """
    text = _validate_text(
        content, field="content", max_chars=_MAX_USER_INJECTION_CHARS,
    )
    if discussion.status == STATUS_RUNNING:
        raise ValueError(
            "Cannot inject a message while a round is in progress",
        )
    if (discussion.current_round or 0) < 1:
        raise ValueError(
            "Cannot inject a message before the first round has run",
        )

    # Find the highest turn_index already used in this round so the
    # injection lands after the last persona reply (and after any
    # previous injection on the same round).
    #
    # Race window (PR #218): two concurrent injects can both read
    # `max=N` and try to insert `turn_index=N+1`. The PR-#218 unique
    # constraint on (discussion_id, round, turn_index) surfaces the
    # second insert as IntegrityError; we retry by re-reading max +
    # bumping. Bounded retry count protects against pathological
    # contention storms (typical real traffic = 1-2 attempts max).
    from sqlalchemy.exc import IntegrityError

    last_exc: Exception | None = None
    for _attempt in range(5):
        max_idx = await db.scalar(
            select(DiscussionTurn.turn_index)
            .where(
                DiscussionTurn.discussion_id == discussion.id,
                DiscussionTurn.round == discussion.current_round,
            )
            .order_by(DiscussionTurn.turn_index.desc())
            .limit(1)
        )
        next_idx = (int(max_idx) + 1) if max_idx is not None else 0

        row = DiscussionTurn(
            discussion_id=discussion.id,
            round=discussion.current_round,
            turn_index=next_idx,
            persona_id=USER_PERSONA_ID,
            stance=USER_INJECTION_STANCE,
            content=text,
            citations=None,
        )
        db.add(row)
        discussion.updated_at = datetime.now(UTC)
        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            last_exc = exc
            continue
        await db.refresh(row)
        return row
    raise RuntimeError(
        f"inject_user_message: turn_index collision retried 5x without success "
        f"(last error: {last_exc})"
    )


async def prune_old_round_contexts(
    db: AsyncSession, *, older_than_days: int,
) -> int:
    """Delete `discussion_round_contexts` rows whose `captured_at`
    is older than `older_than_days`. Returns deleted row count.

    Snapshots persist a full `gather_market_context` JSON per round
    (~30-50 KB each, occasionally 100 KB+ when focus_briefs has 5
    symbols). A user with 100 discussions × 5 rounds each fills
    25-50 MB of snapshot JSON; the table grows unbounded today.

    The 90-day default matches `_PRIOR_DISCUSSIONS_LOOKBACK_DAYS` so
    the cross-session memory window and the replay archive age out
    together — anything older than 90 days is already considered
    stale by the discussion subsystem itself. Discussions and their
    `discussion_turns` are NOT touched (they're tiny, retain
    forever); only the heavy JSON snapshots get GC'd.

    Multi-pod safe via the standard `delete ... where ts < cutoff`
    pattern; concurrent runs just both no-op after the first
    finishes (delete is idempotent).
    """
    cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
    stmt = delete(DiscussionRoundContext).where(
        DiscussionRoundContext.captured_at < cutoff,
    )
    result = await db.execute(stmt)
    await db.commit()
    return result.rowcount or 0


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


def _is_year_like(code: str) -> bool:
    """4-digit numeric tokens in the year range — `2026 Q1 法說` would
    otherwise be tagged as a TW stock code and pollute the per-symbol
    sentiment lookup."""
    if len(code) != 4 or not code.isdigit():
        return False
    return _YEAR_MIN <= int(code) <= _YEAR_MAX


def _crypto_universe() -> list[str]:
    """Top-20 crypto base assets, normalised to uppercase. Imported
    lazily so a unit test that monkeypatches `data.crypto.symbols`
    sees the patched value, and so the discussion service stays
    decoupled from the crypto module loading at import time."""
    try:
        from data.crypto.symbols import TOP20
    except Exception:
        return []
    return [str(s).upper() for s in TOP20 if s]


def extract_focus_symbols(text: str, *, market: str = _DEFAULT_MARKET) -> list[str]:
    """Pull stock / crypto codes out of free text. Deduped, capped at
    `_MAX_FOCUS_SYMBOLS`, returned in encounter order.

    Behaviour by market:
      - TW: 4-6 digit numeric codes; 4-digit year-like values
        (1900-2099) are filtered to avoid mis-tagging dates.
      - US: cashtag `$AAPL` always honoured; bare uppercase 1-5 letter
        tokens honoured if they aren't in `_US_TICKER_STOPWORDS`.
      - GLOBAL: cashtags + crypto base assets from the curated Top-20
        universe (BTC / ETH / SOL …). Year-filtered TW codes are also
        honoured because the international news bucket sometimes
        carries cross-listed TW ADRs (TSM / UMC).

    Cashtag matches are honoured for every market — `$AAPL` in a TW
    discussion still pulls AAPL into the per-symbol news bucket,
    because the user's intent is explicit.
    """
    raw = text or ""
    seen: list[str] = []

    def _push(code: str) -> bool:
        if code not in seen:
            seen.append(code)
        return len(seen) >= _MAX_FOCUS_SYMBOLS

    for tag in _CASHTAG_RE.findall(raw):
        if _push(tag):
            return seen

    market = (market or _DEFAULT_MARKET).upper()
    if market == "TW":
        for code in _TW_SYMBOL_RE.findall(raw):
            if _is_year_like(code):
                continue
            if _push(code):
                break
    elif market == "US":
        for tok in _BARE_US_TICKER_RE.findall(raw):
            if tok in _US_TICKER_STOPWORDS:
                continue
            if _push(tok):
                break
    else:  # GLOBAL — accept TW digits + crypto base assets
        universe = set(_crypto_universe())
        for tok in _BARE_US_TICKER_RE.findall(raw):
            if tok not in universe:
                continue
            if _push(tok):
                return seen
        for code in _TW_SYMBOL_RE.findall(raw):
            if _is_year_like(code):
                continue
            if _push(code):
                break

    # Name-based fallback for TW + GLOBAL markets (PR #221). Topics
    # written with the company short name ("討論台積電 / 鴻海 短線
    # 走勢") miss the digit-only regex. Lookup against the in-memory
    # `_name_map` populated by the daily symbol-refresh cron picks
    # those up so `prior_discussions` / `per_symbol_news_sentiment` /
    # `focus_briefs` actually find them. Skipped for US — different
    # name conventions, and the bare-ticker regex already covers
    # the common case there.
    if market in ("TW", "GLOBAL") and len(seen) < _MAX_FOCUS_SYMBOLS:
        try:
            from services.tw_market_service import (
                find_symbols_by_names_in_text,
            )
            remaining = _MAX_FOCUS_SYMBOLS - len(seen)
            for sym in find_symbols_by_names_in_text(raw, limit=remaining):
                if _push(sym):
                    break
        except Exception:
            # Fresh deploy where symbol map hasn't loaded, or any
            # other defensive failure — fall through with whatever
            # the regex-only pass found.
            pass
    return seen


# ── focus_briefs (per-symbol mini analyst report) ──────────────────
#
# `gather_market_context` already pulls per-symbol news sentiment for
# anything the topic names; that's enough for "what is the market
# saying about 2330" but useless for "should we buy 2330 at this
# price". An analyst joining a meeting would have at minimum: latest
# quote, technical context (MAs, 52w range, RSI, recent perf),
# fundamentals (PE / PB / yield / EPS), revenue trend, foreign /
# margin flow, and a few peers. `_assemble_focus_briefs` builds that
# bundle for each focus_symbol so the personas have actual evidence
# to reason over instead of guessing from headlines.
#
# Each block is best-effort — a missing fundamentals snapshot or a
# stooq history outage doesn't kill the brief, the failed block is
# just None / [] and the persona reasons with what's available.


_FOCUS_BRIEF_HISTORY_MONTHS = 12         # enough for 52w high/low
_FOCUS_BRIEF_REVENUE_MONTHS = 6
_FOCUS_BRIEF_CHIP_DAYS = 5
_FOCUS_BRIEF_PEER_COUNT = 3


def _bar_close(bar: dict[str, Any]) -> float | None:
    c = bar.get("close")
    try:
        return float(c) if c is not None else None
    except (TypeError, ValueError):
        return None


def _ma(closes: list[float], window: int) -> float | None:
    if len(closes) < window:
        return None
    return round(sum(closes[-window:]) / window, 4)


def _rsi(closes: list[float], window: int = 14) -> float | None:
    """Wilder's RSI on the last `window` returns. Falls through to None
    when there's not enough data — fresh-listed names that landed in
    the topic get a None instead of a misleading 50."""
    if len(closes) <= window:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas[-window:]]
    losses = [max(-d, 0.0) for d in deltas[-window:]]
    avg_gain = sum(gains) / window
    avg_loss = sum(losses) / window
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def _pct_change(start: float | None, end: float | None) -> float | None:
    if not start or not end or start == 0:
        return None
    return round((end - start) / start * 100.0, 2)


def _compute_technicals(bars: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Compute summary technicals from a daily-OHLCV history list.

    Returns None when fewer than 20 bars available — the moving
    averages would be too thin to carry signal and the persona is
    better off seeing "技術指標不足" than misleading numbers.
    """
    closes = [c for c in (_bar_close(b) for b in bars) if c is not None]
    if len(closes) < 20:
        return None
    last = closes[-1]
    high_52w = max(closes[-min(252, len(closes)):])
    low_52w = min(closes[-min(252, len(closes)):])
    return {
        "last_close":      round(last, 4),
        "ma20":            _ma(closes, 20),
        "ma60":            _ma(closes, 60),
        "ma120":           _ma(closes, 120),
        "high_52w":        round(high_52w, 4),
        "low_52w":         round(low_52w, 4),
        "dist_high_52w_pct": _pct_change(high_52w, last),
        "dist_low_52w_pct":  _pct_change(low_52w, last),
        "perf_5d_pct":     _pct_change(
            closes[-6] if len(closes) >= 6 else None, last,
        ),
        "perf_20d_pct":    _pct_change(
            closes[-21] if len(closes) >= 21 else None, last,
        ),
        "perf_60d_pct":    _pct_change(
            closes[-61] if len(closes) >= 61 else None, last,
        ),
        "rsi14":           _rsi(closes, 14),
    }


def _summarize_revenue(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Take the latest `_FOCUS_BRIEF_REVENUE_MONTHS` rows from a
    `tw_market_service.get_revenue` response, drop noise fields."""
    if not rows:
        return []
    tail = rows[-_FOCUS_BRIEF_REVENUE_MONTHS:]
    out: list[dict[str, Any]] = []
    for r in tail:
        out.append({
            "month":       (r.get("date") or "")[:7],
            "revenue_yoy": r.get("revenue_yoy"),
            "revenue_mom": r.get("revenue_mom"),
        })
    return out


def _summarize_institutional(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Sum 5-day net foreign / SITC / dealer over the rows. Returns
    None when nothing came back — caller drops the block."""
    if not rows:
        return None
    fini_net = sitc_net = dealer_net = 0
    days = 0
    for r in rows[-_FOCUS_BRIEF_CHIP_DAYS:]:
        fini_net += int(r.get("fini_buy") or 0) - int(r.get("fini_sell") or 0)
        sitc_net += int(r.get("sitc_buy") or 0) - int(r.get("sitc_sell") or 0)
        dealer_net += int(r.get("dealer_buy") or 0) - int(r.get("dealer_sell") or 0)
        days += 1
    if days == 0:
        return None
    return {
        "fini_net_5d":   fini_net,
        "sitc_net_5d":   sitc_net,
        "dealer_net_5d": dealer_net,
        "days":          days,
    }


def _summarize_margin(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    latest = rows[-1]
    return {
        "as_of":           latest.get("date"),
        "margin_balance":  latest.get("margin_balance"),
        "short_balance":   latest.get("short_balance"),
    }


async def _get_tw_peers(
    *, symbol: str, industry: str | None, limit: int = _FOCUS_BRIEF_PEER_COUNT,
) -> list[dict[str, Any]]:
    """Same-industry comparable set drawn from the cached screener.

    Picks the most-traded peers (proxy for liquidity / market cap)
    and returns a compact `{symbol, name, price, change_pct, pe}`
    record per peer. Empty list when the industry is unknown or the
    screener cache is cold / failing.
    """
    if not industry:
        return []
    try:
        from services import tw_market_service
        rows = await tw_market_service.get_screener(
            limit=400, min_volume=500_000,
        )
    except Exception:
        return []
    candidates = []
    for r in rows:
        sym = r.get("symbol")
        if not sym or sym == symbol:
            continue
        if _is_speculative_etf(sym):
            continue
        if tw_market_service.get_industry(sym) != industry:
            continue
        candidates.append(r)
    candidates.sort(
        key=lambda r: (r.get("volume") or 0), reverse=True,
    )
    out: list[dict[str, Any]] = []
    for r in candidates[:limit]:
        sym = r.get("symbol")
        out.append({
            "symbol":     sym,
            "name":       r.get("name_zh") or tw_market_service.get_company_name(sym),
            "price":      r.get("price"),
            "change_pct": r.get("change_pct"),
            "pe":         r.get("pe_ratio"),
        })
    return out


async def _build_tw_focus_brief(
    symbol: str, *, as_of: date | None = None,
) -> dict[str, Any]:
    """Per-TW-symbol mini analyst report. Each sub-call is wrapped in
    its own try so a single connector outage doesn't blank the whole
    brief — the persona just sees "fundamentals: null" and reasons
    with what remained.

    Doesn't take a `db` session: every sub-call goes through the
    `tw_market_service` autosession variants which open + close
    their own connections. The `db` parameter used to be threaded
    through (and was unused) — dropped in PR #220 so the
    concurrency contract is unambiguous: this builder is safe to
    fan out alongside the parallel `gather_market_context` tasks
    that DO touch the shared `db`.

    `as_of` (PR #224): backtest mode. When set, routes to
    `_build_tw_focus_brief_backtest` which reads only from
    `ohlcv_daily` with `ts <= as_of`. Live-only blocks
    (fundamentals / revenue / chip / peers) are skipped in v1.
    """
    if as_of is not None:
        return await _build_tw_focus_brief_backtest(symbol, as_of=as_of)
    from services import tw_market_service

    brief: dict[str, Any] = {
        "symbol":         symbol,
        "name_zh":        tw_market_service.get_company_name(symbol),
        "industry":       tw_market_service.get_industry(symbol),
        "quote":          None,
        "technicals":     None,
        "fundamentals":   None,
        "revenue_trend":  [],
        "chip_5d":        None,
        "margin_latest":  None,
        "peers":          [],
    }

    # Quote — cached behind Redis 15s, safe to call even mid-round.
    try:
        q = await tw_market_service.get_quote(symbol)
        brief["quote"] = {
            "price":      q.get("price"),
            "change_pct": q.get("change_pct"),
            "volume":     q.get("volume"),
            "prev_close": q.get("prev_close"),
        }
    except Exception as exc:
        log.warning("focus_brief.quote.failed",
                    extra={"symbol": symbol, "error": str(exc)})

    # History → technicals. 12 months is enough for 52w stats + 60d MA.
    try:
        bars = await tw_market_service.get_history(
            symbol, months=_FOCUS_BRIEF_HISTORY_MONTHS,
        )
        brief["technicals"] = _compute_technicals(bars or [])
    except Exception as exc:
        log.warning("focus_brief.history.failed",
                    extra={"symbol": symbol, "error": str(exc)})

    # Fundamentals.
    try:
        f = await tw_market_service.get_fundamentals(symbol)
        if isinstance(f, dict):
            brief["fundamentals"] = {
                "pe":             f.get("pe_ratio"),
                "pb":             f.get("pb_ratio"),
                "dividend_yield": f.get("dividend_yield"),
                "eps":            f.get("eps"),
            }
    except Exception as exc:
        log.warning("focus_brief.fundamentals.failed",
                    extra={"symbol": symbol, "error": str(exc)})

    # Revenue trend (TW-only data — Taiwan listed companies file monthly).
    try:
        rev = await tw_market_service.get_revenue(
            symbol, months=_FOCUS_BRIEF_REVENUE_MONTHS,
        )
        brief["revenue_trend"] = _summarize_revenue(rev or [])
    except Exception as exc:
        log.warning("focus_brief.revenue.failed",
                    extra={"symbol": symbol, "error": str(exc)})

    # Chip metrics (法人 + 融資融券).
    try:
        inst = await tw_market_service.get_institutional(
            symbol, days=_FOCUS_BRIEF_CHIP_DAYS,
        )
        brief["chip_5d"] = _summarize_institutional(inst or [])
    except Exception as exc:
        log.warning("focus_brief.institutional.failed",
                    extra={"symbol": symbol, "error": str(exc)})

    try:
        margin = await tw_market_service.get_margin(
            symbol, days=_FOCUS_BRIEF_CHIP_DAYS,
        )
        brief["margin_latest"] = _summarize_margin(margin or [])
    except Exception as exc:
        log.warning("focus_brief.margin.failed",
                    extra={"symbol": symbol, "error": str(exc)})

    # Peer set — best-effort, off the cached screener.
    try:
        brief["peers"] = await _get_tw_peers(
            symbol=symbol, industry=brief["industry"],
        )
    except Exception as exc:
        log.warning("focus_brief.peers.failed",
                    extra={"symbol": symbol, "error": str(exc)})

    return brief


async def _build_tw_focus_brief_backtest(
    symbol: str, *, as_of: date,
) -> dict[str, Any]:
    """Backtest variant of `_build_tw_focus_brief` (PR #224).

    Reads ONLY from `ohlcv_daily` with `ts <= as_of` so no live data
    leaks back from the future. The synthetic `quote` block carries
    the close on `as_of` (or the most recent bar before it) plus the
    1-day change vs the prior bar. Technicals (MA / 52w / RSI / perf
    %) are computed from the as_of-truncated history.

    Skipped in v1: fundamentals, revenue trend, chip metrics, peers.
    Those readers either don't have an `as_of`-aware path yet or
    require live screener data that can't be reconstructed from the
    DB. They show as null in the brief; personas read them as "data
    not available in backtest mode" — better than fabricating values
    that never existed on `as_of`.
    """
    from services import tw_market_service
    from services.ingest.repository import read_ohlcv_range_autosession

    brief: dict[str, Any] = {
        "symbol":         symbol,
        "name_zh":        tw_market_service.get_company_name(symbol),
        "industry":       tw_market_service.get_industry(symbol),
        "quote":          None,
        "technicals":     None,
        "fundamentals":   None,
        "revenue_trend":  [],
        "chip_5d":        None,
        "margin_latest":  None,
        "peers":          [],
        "_backtest":      True,   # marker the prompt template can show
        "_as_of":         as_of.isoformat(),
    }

    try:
        # ~12 months of bars ending at as_of so 52w / MA120 have
        # enough lookback. Slight overshoot fine — `_compute_technicals`
        # is a pure function over closes.
        start = as_of - timedelta(days=400)
        bars = await read_ohlcv_range_autosession("TW", symbol, start, as_of)
    except Exception as exc:
        log.warning("focus_brief.backtest.history.failed",
                    extra={"symbol": symbol, "as_of": as_of.isoformat(), "error": str(exc)})
        bars = []

    if bars:
        brief["technicals"] = _compute_technicals(bars)
        # Synthetic quote: last close as price; change_pct vs prior bar.
        last = bars[-1]
        prev = bars[-2] if len(bars) >= 2 else None
        last_close = _bar_close(last)
        prev_close = _bar_close(prev) if prev else None
        change_pct = (
            ((last_close - prev_close) / prev_close * 100.0)
            if last_close is not None and prev_close not in (None, 0)
            else None
        )
        brief["quote"] = {
            "price":      last_close,
            "change_pct": round(change_pct, 2) if change_pct is not None else None,
            "volume":     int(last.get("volume") or 0),
            "prev_close": prev_close,
        }
    return brief


async def _build_us_focus_brief(symbol: str) -> dict[str, Any]:
    """US-side equivalent — quote + technicals + fundamentals only.
    No revenue / chip / peers because the underlying data tier
    doesn't have parity with TW (no monthly revenue feed, no
    foreign-investor ledger, no industry-tagged screener)."""
    from services import us_market_service

    brief: dict[str, Any] = {
        "symbol":       symbol,
        "name":         None,
        "industry":     None,
        "quote":        None,
        "technicals":   None,
        "fundamentals": None,
    }
    try:
        q = await us_market_service.get_quote(symbol)
        brief["quote"] = {
            "price":      q.get("price"),
            "change_pct": q.get("change_pct"),
            "volume":     q.get("volume"),
            "prev_close": q.get("prev_close"),
        }
    except Exception as exc:
        log.warning("focus_brief.us_quote.failed",
                    extra={"symbol": symbol, "error": str(exc)})

    try:
        bars = await us_market_service.get_history(symbol, period="1y", interval="1d")
        brief["technicals"] = _compute_technicals(bars or [])
    except Exception as exc:
        log.warning("focus_brief.us_history.failed",
                    extra={"symbol": symbol, "error": str(exc)})

    try:
        f = await us_market_service.get_fundamentals(symbol)
        if isinstance(f, dict):
            brief["name"] = f.get("name")
            brief["industry"] = f.get("industry") or f.get("sector")
            brief["fundamentals"] = {
                "pe":             f.get("pe_ratio"),
                "pb":             f.get("pb_ratio"),
                "dividend_yield": f.get("dividend_yield"),
                "eps":            f.get("eps"),
                "market_cap":     f.get("market_cap"),
            }
    except Exception as exc:
        log.warning("focus_brief.us_fundamentals.failed",
                    extra={"symbol": symbol, "error": str(exc)})

    return brief


async def _assemble_focus_briefs(
    *, market: str, symbols: list[str], as_of: date | None = None,
) -> list[dict[str, Any]]:
    """Fan out per-symbol brief assembly concurrently. Cap at
    `_MAX_FOCUS_SYMBOLS` for token-budget protection.

    No `db` param: both `_build_tw_focus_brief` and
    `_build_us_focus_brief` use their respective service modules'
    autosession helpers, so this fan-out is safe to run alongside
    the shared-`db` reads in `gather_market_context` (PR #222
    cleanup; the dead param was removed for a clearer concurrency
    contract).

    `as_of` (PR #224): backtest mode. TW route uses the historical
    `_build_tw_focus_brief_backtest` variant. US route currently
    has no backtest variant — backtests on US discussions return
    empty briefs in v1.
    """
    if not symbols:
        return []
    syms = symbols[:_MAX_FOCUS_SYMBOLS]
    if market == "TW":
        coros = [_build_tw_focus_brief(s, as_of=as_of) for s in syms]
    elif market == "US":
        # No US backtest variant in v1 — degrade to empty briefs.
        if as_of is not None:
            return []
        coros = [_build_us_focus_brief(s) for s in syms]
    else:
        # GLOBAL — fall back to US shape for ASCII-letter symbols, TW
        # shape for digit-only. Crypto symbols (BTC/ETH/...) would
        # also land in the US branch but their fundamentals path
        # doesn't apply; the personas already see them via the
        # crypto news block, so we skip them here to avoid faking
        # equity-style PE/PB.
        coros = []
        for s in syms:
            if s.isdigit():
                coros.append(_build_tw_focus_brief(s, as_of=as_of))
            elif s in _crypto_universe():
                continue
            elif as_of is None:
                # US backtest currently unavailable — skip in v1.
                coros.append(_build_us_focus_brief(s))
    if not coros:
        return []
    results = await asyncio.gather(*coros, return_exceptions=True)
    out: list[dict[str, Any]] = []
    for r in results:
        if isinstance(r, Exception):
            log.warning("focus_brief.fan_out.failed", extra={"error": str(r)})
            continue
        out.append(r)
    return out


# ── macro block (FRED-backed) ──────────────────────────────────────


_MACRO_SERIES = (
    ("fed_funds_rate", "Fed Funds Rate (%)"),
    ("10y_yield",      "US 10Y Treasury (%)"),
    ("10y_minus_2y",   "10Y-2Y Spread (%)"),
    ("usd_index",      "USD Index (DXY)"),
    ("twd_usd",        "TWD/USD"),
)


def _macro_summary_from_series(
    series: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Reduce a FRED `[{date, value}]` time series to:
        {latest_date, latest_value, change_1y, change_3m}.
    Returns None if the series came back empty."""
    if not series:
        return None
    points: list[tuple[str, float]] = []
    for p in series:
        try:
            v = float(p["value"])
        except (TypeError, ValueError, KeyError):
            continue
        d = p.get("date")
        if not d:
            continue
        points.append((d, v))
    if not points:
        return None
    points.sort(key=lambda p: p[0])
    latest_date, latest_value = points[-1]

    def _earlier_than(target_days: int) -> float | None:
        from datetime import date as _date
        from datetime import timedelta as _td
        try:
            ld = _date.fromisoformat(latest_date)
        except ValueError:
            return None
        target = ld - _td(days=target_days)
        # Find the closest point on or before `target`.
        best: float | None = None
        for d, v in points:
            try:
                dd = _date.fromisoformat(d)
            except ValueError:
                continue
            if dd <= target:
                best = v
        return best

    # Series-level change deltas — for rates we report absolute change
    # (basis points), for everything else relative %. Personas read
    # these to anchor "Fed cut 75bp YoY" vs "DXY +6% YoY".
    one_y_ago = _earlier_than(365)
    three_m_ago = _earlier_than(90)
    return {
        "latest_date":  latest_date,
        "latest_value": round(latest_value, 4),
        "change_1y":    None if one_y_ago is None
                        else round(latest_value - one_y_ago, 4),
        "change_3m":    None if three_m_ago is None
                        else round(latest_value - three_m_ago, 4),
    }


async def _assemble_macro_block(
    *, as_of: date | None = None,
) -> dict[str, Any]:
    """Pull a small set of FRED macro series concurrently and reduce
    each to its latest value plus 1y / 3m delta. Empty / failing
    series degrade to None so the personas can mention "macro data
    incomplete" instead of confidently citing a missing rate.

    `as_of` (PR #226): backtest mode. Forwards to FRED's
    `observation_end` so each series excludes data points after the
    backtest anchor."""
    from services import us_market_service

    async def _pull(name: str) -> tuple[str, list[dict[str, Any]]]:
        try:
            return name, await us_market_service.get_macro_indicator(
                name, as_of=as_of,
            )
        except Exception as exc:
            log.warning("macro.fetch.failed",
                        extra={"name": name, "error": str(exc)})
            return name, []

    results = await asyncio.gather(*[_pull(n) for n, _ in _MACRO_SERIES])
    by_name = dict(results)
    block: dict[str, Any] = {}
    for name, label in _MACRO_SERIES:
        block[name] = {
            "label":   label,
            "summary": _macro_summary_from_series(by_name.get(name) or []),
        }
    return block


# ── user_context (owner's portfolio + watchlist) ───────────────────
#
# Personas like portfolio_advisor / risk_manager need to know what
# the user actually holds before recommending action — "should I add
# 2330" is unanswerable without knowing whether they already own
# 30% in 2330. Other personas can ignore the block.
#
# Read directly off the ORM with no live-quote enrichment: the
# discussion isn't about today's exact P&L, it's about portfolio
# fit, sector concentration, and overlap with the topic. Cap each
# list (holdings, watchlist_symbols) so the prompt budget stays
# bounded even for power users with many portfolios.
#
# Privacy: round_context snapshots persist this block, but
# `discussion_round_contexts` is owner-scoped via the discussion's
# FK — only the owner can read their own snapshots through the API.

_USER_CONTEXT_HOLDING_CAP = 20
_USER_CONTEXT_WATCHLIST_CAP = 30


async def _assemble_user_context(
    db: AsyncSession,
    *,
    owner_id: uuid.UUID,
    focus_symbols: list[str] | None = None,
) -> dict[str, Any]:
    """Compact summary of the discussion owner's portfolio + watchlist.

    Cheap (no live quote enrichment) — suitable to fire on every
    round. Each sub-block degrades to an empty list on query failure
    so a transient portfolio-table outage doesn't kill the round.
    """
    from models.portfolio import Holding, Portfolio
    from models.watchlist import Watchlist, WatchlistItem

    out: dict[str, Any] = {
        "portfolios":        [],
        "holdings":          [],
        "watchlist_symbols": [],
        "focus_overlap":     {"held": [], "watching": []},
    }
    focus_set = {s for s in (focus_symbols or []) if s}

    try:
        pf_stmt = (
            select(Portfolio)
            .where(Portfolio.user_id == owner_id)
            .order_by(Portfolio.created_at)
        )
        portfolios = list((await db.scalars(pf_stmt)).all())
    except Exception as exc:
        log.warning("user_context.portfolios.failed", extra={"error": str(exc)})
        portfolios = []

    holdings_rows: list[dict[str, Any]] = []
    for p in portfolios:
        try:
            h_stmt = select(Holding).where(Holding.portfolio_id == p.id)
            hs = list((await db.scalars(h_stmt)).all())
        except Exception as exc:
            log.warning("user_context.holdings.failed",
                        extra={"portfolio_id": str(p.id), "error": str(exc)})
            hs = []
        out["portfolios"].append({
            "name":          p.name,
            "currency":      p.currency,
            "holding_count": len(hs),
        })
        for h in hs:
            holdings_rows.append({
                "portfolio":     p.name,
                "symbol":        h.symbol,
                "market":        h.market.value,
                "quantity":      float(h.quantity),
                "avg_cost":      float(h.avg_cost),
                "cost_currency": h.cost_currency,
            })

    # Largest-position-first so the cap prefers the meaningful holdings.
    holdings_rows.sort(
        key=lambda r: float(r["quantity"]) * float(r["avg_cost"]),
        reverse=True,
    )
    out["holdings"] = holdings_rows[:_USER_CONTEXT_HOLDING_CAP]

    try:
        wl_stmt = (
            select(WatchlistItem)
            .join(Watchlist, WatchlistItem.watchlist_id == Watchlist.id)
            .where(Watchlist.user_id == owner_id)
        )
        wl_items = list((await db.scalars(wl_stmt)).all())
    except Exception as exc:
        log.warning("user_context.watchlist.failed", extra={"error": str(exc)})
        wl_items = []

    seen_wl: set[tuple[str, str]] = set()
    watchlist_summary: list[dict[str, str]] = []
    for it in wl_items:
        key = (it.market.value, it.symbol)
        if key in seen_wl:
            continue
        seen_wl.add(key)
        watchlist_summary.append({"symbol": it.symbol, "market": it.market.value})
    out["watchlist_symbols"] = watchlist_summary[:_USER_CONTEXT_WATCHLIST_CAP]

    if focus_set:
        held_syms = {r["symbol"] for r in holdings_rows}
        wl_syms = {it["symbol"] for it in watchlist_summary}
        out["focus_overlap"] = {
            "held":     sorted(focus_set & held_syms),
            "watching": sorted(focus_set & wl_syms),
        }

    return out


# ── prior_discussions (cross-discussion memory) ───────────────────
#
# Personas have no recall across sessions: each new discussion sees
# market data + the current transcript, but never "what did this
# user / panel conclude on 2330 last week?". That makes consistency
# impossible — round 1 of a new discussion can recommend Buy on the
# same name where the last concluded discussion said Hold.
#
# `_assemble_prior_discussions` queries the owner's past completed
# discussions whose topic OR `conclusion.recommended_symbols`
# overlap with the current focus_symbols. Returns a compact list
# the synthesiser-style prompt can render as "上次 4/15 對 2330
# 結論 Hold (時間軸 short_term, 共識 0.7)" so personas can stay
# coherent across sessions.
#
# Owner-scoped (the FK + WHERE clause both hard-gate to the user);
# the current discussion's own id is excluded so a re-run doesn't
# reference itself.

_PRIOR_DISCUSSIONS_CAP = 5
_PRIOR_DISCUSSIONS_LOOKBACK_DAYS = 90


async def _assemble_prior_discussions(
    db: AsyncSession,
    *,
    owner_id: uuid.UUID,
    focus_symbols: list[str] | None,
    exclude_id: uuid.UUID | None = None,
    as_of: datetime | None = None,
) -> list[dict[str, Any]]:
    """Most-recent-first list of the owner's concluded discussions
    that overlap any of `focus_symbols` (matched against the topic
    string or against `conclusion.recommended_symbols`).

    Capped at `_PRIOR_DISCUSSIONS_CAP` and limited to the last
    `_PRIOR_DISCUSSIONS_LOOKBACK_DAYS` days so a 2-year-old call is
    not dragged into a fresh discussion's prompt.

    `as_of` (PR #224) clamps the lookup window to discussions
    concluded BEFORE that timestamp — backtest mode prevents
    "future leakage" where a 2026-04-22 discussion would otherwise
    surface in a 2026-01-15 backtest's prior list.

    The block is intentionally compact (no full conclusion reasoning,
    no risks list) — personas only need the headline so they can
    stay consistent. They can refer the user back to the prior
    discussion id for full detail.
    """
    if not focus_symbols:
        return []

    anchor = as_of or datetime.now(UTC)
    cutoff = anchor - timedelta(days=_PRIOR_DISCUSSIONS_LOOKBACK_DAYS)
    stmt = (
        select(Discussion)
        .where(
            Discussion.owner_id == owner_id,
            Discussion.status == STATUS_DONE,
            Discussion.conclusion.isnot(None),
            Discussion.created_at >= cutoff,
            Discussion.created_at < anchor,
        )
        .order_by(Discussion.created_at.desc())
        .limit(50)  # generous cap for the in-Python filter
    )
    if exclude_id is not None:
        stmt = stmt.where(Discussion.id != exclude_id)
    rows = list((await db.scalars(stmt)).all())
    if not rows:
        return []

    focus_set = {str(s) for s in focus_symbols}
    matches: list[dict[str, Any]] = []
    for row in rows:
        topic = row.topic or ""
        conclusion = row.conclusion if isinstance(row.conclusion, dict) else {}
        recommended = [
            str(s).strip()
            for s in (conclusion.get("recommended_symbols") or [])
            if str(s).strip()
        ]
        # Match either: focus symbol literally appears in the topic
        # string (catches the `2330` / `$AAPL` case the user typed),
        # or appears in the prior conclusion's recommended_symbols.
        matched = sorted({
            sym for sym in focus_set
            if sym in topic or sym in recommended
        })
        if not matched:
            continue
        matches.append({
            "id":                  str(row.id),
            "created_at":          row.created_at.isoformat(),
            # PR #278: surface the historical anchor for backtest
            # discussions so the persona prompt + frontend summary
            # show "the day this prior discussion was analysing",
            # not the day it was created. NULL for live discussions
            # — caller (frontend / `_format_history`) falls back to
            # `created_at` when missing.
            "as_of_date":          (
                row.as_of_date.isoformat() if row.as_of_date else None
            ),
            "topic":               topic[:120],
            "recommended_symbols": recommended[:5],
            "time_horizon":        conclusion.get("time_horizon"),
            "consensus_score":     conclusion.get("consensus_score"),
            "verdict":             row.verdict,
            "matched_symbols":     matched,
        })
        if len(matches) >= _PRIOR_DISCUSSIONS_CAP:
            break
    return matches


async def gather_market_context(
    db: AsyncSession,
    *,
    market: str = "TW",
    top_n: int = _DEFAULT_TOP_MOVERS,
    focus_symbols: list[str] | None = None,
    owner_id: uuid.UUID | None = None,
    exclude_discussion_id: uuid.UUID | None = None,
    as_of: date | None = None,
    progress_cb: Any = None,
) -> dict[str, Any]:
    """Build a structured snapshot of the market state for the personas.

    Thin wrapper around `services.discussion.context.build_market_context`
    — the actual orchestration + per-block fetchers live there. Kept
    here as the public name so existing callers (and the long-standing
    `patch("services.discussion_service.gather_market_context", ...)`
    pattern in tests) keep working unchanged.

    Each block degrades gracefully — if any data source is unavailable
    we return an empty list / None for that block instead of raising,
    so a transient outage doesn't block the discussion entirely.

    `focus_symbols` (typically extracted from the discussion topic via
    `extract_focus_symbols`) makes the context include per-symbol news
    sentiment alongside the market-wide aggregate. Empty list / None
    skips the per-symbol block.

    `owner_id` (the discussion's owner) opts the context into a
    `user_context` block carrying the owner's portfolio + watchlist
    summary plus overlap with `focus_symbols`. Personas that don't
    care about portfolio fit can ignore it.
    """
    from services.discussion.context import build_market_context
    return await build_market_context(
        db,
        market=market,
        top_n=top_n,
        focus_symbols=focus_symbols,
        owner_id=owner_id,
        exclude_discussion_id=exclude_discussion_id,
        as_of=as_of,
        max_focus_symbols=_MAX_FOCUS_SYMBOLS,
        progress_cb=progress_cb,
    )


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


def _compact_us_screener_row(r: dict[str, Any]) -> dict[str, Any]:
    """US-side compact form (PR #215). Mirrors `_compact_screener_row`
    but pulls from US screener output: industry / sector come from
    the row directly (no global map like TW's `_industry_map`); PE /
    yield often missing on Polygon snapshot tier so they're omitted
    rather than passed through as 0."""
    sym = r.get("symbol")
    return {
        "symbol":     sym,
        "name":       r.get("name"),
        "sector":     r.get("sector"),
        "price":      r.get("price"),
        "change_pct": r.get("change_pct"),
        "volume":     r.get("volume"),
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

# Per-block schema annotation. Keys match top-level ctx field names so
# `_persona_schema_annotation()` can project the right subset for each
# persona. Without this annotation, weak models (Haiku, GPT-4o-mini,
# Llama-3.3) fixate on `top_gainers` and ignore the rest — defeating
# the cost of all the ingest crons feeding the context.
#
# Bullets are intentionally one-per-block. The `top_gainers` /
# `top_losers` pair shares one entry because they're rendered together;
# `risk_warnings` keeps its emphasis on the negative-filter semantics
# (that's where weak models most often go wrong).
_BLOCK_ANNOTATIONS: dict[str, str] = {
    "top_gainers":               "- top_gainers / top_losers：當日漲跌幅前 10（動能 + 籌碼面）。",
    "index":                     "- index：大盤 (TAIEX) 即時報價 + 30 日歷史，用以判斷市場 regime。",
    "news_sentiment":            "- news_sentiment：所屬市場整體新聞情緒（bullish/bearish/neutral 計數，全視窗統計而非 headlines 抽樣）。",
    "per_symbol_news_sentiment": "- per_symbol_news_sentiment：主題提及之個股新聞情緒。",
    "short_term_signals": (
        "- short_term_signals：focus_symbols 的 Tier-1 短線技術訊號 "
        "(1-5 日視窗) — `volume_ratio`（量能 / 20 日均量倍數，>2 = 突破型）、"
        "`return_5d` / `return_20d`（短中期報酬 %）、`rsi_14`（< 30 超賣 / > 70 超買）、"
        "`gap_pct`（今日開盤跳空 %）、`kd_k` / `kd_d`（KD 9-3-3，K 從下方穿越 D"
        "且 < 20 = 偏多反轉，從上方穿越 D 且 > 80 = 偏空反轉）、"
        "`macd` / `macd_signal` / `macd_hist`（MACD 12-26-9，histogram 由負轉正 "
        "= 偏多動能起步、由正轉負 = 偏空動能起步）、"
        "`bollinger_pct_b` / `bollinger_width`（布林通道 20-2σ，%B > 1 = 突破上軌、"
        "< 0 = 跌破下軌；width 縮小 = 波動收斂常為突破前兆）、"
        "`obv_5d_change_pct`（OBV 5 日累計變化，正值且漲 = 籌碼增持確認、"
        "負值但漲 = 量價背離警示）、"
        "`holdings_concentration_trend`（個股大戶持股集中度 4 週趨勢："
        "`latest_top_holders_pct` 千張大戶 + 集中持有者累計持股比、"
        "`change_pp` 4 週百分點變化、`trend` rising/stable/falling — "
        "rising = 機構長線加碼，是與短線量能訊號獨立的籌碼確認）、"
        "`industry_rs`（同產業 RS：`symbol_return_5d` vs `industry_median_5d`，"
        "正值 = 領先類股，負值 = 落後）、"
        "`day_trading_trend`（個股當沖比 5 日趨勢：`latest_ratio` 最新當沖佔成交比、"
        "`mean_ratio` 5 日均值、`trend` rising/stable/falling — `latest_ratio > 0.5` "
        "且 `trend=rising` = 散戶亢奮過熱，短線見頂風險升）、"
        "`securities_lending_trend`（個股借券餘額 5 日趨勢：`latest_balance` 借券餘額、"
        "`balance_change_5d` 5 日變化（正值 = 餘額上升）、`trend` rising/stable/falling"
        " — `trend=rising` 代表機構透過借券建立隱性空頭部位，是 explicit 短賣以外的"
        "看空 leading flow，逆勢做多需特別留意）、"
        "`upcoming_event`（未來 14 日法說 / 除息行事曆：`next_event` earnings|ex_dividend、"
        "`next_event_in_days` 距事件日數 — `next_event_in_days <= 3` 屬事件窗，個股價格"
        "易出現 asymmetric move，**短線方向訊號失效**，建議 personas 切換為「停看聽」"
        "策略而非追多殺多）。配合 news_sentiment 同向時短線勝率較高。"
    ),
    "international_sentiment":   "- international_sentiment：Fed / FOMC / 國際宏觀新聞情緒，影響台股風險偏好。",
    "top_foreign_buyers":        "- top_foreign_buyers：近 5 日外資累計淨買超前 10 名（已含產業別）。",
    "taifex_positioning": (
        "- taifex_positioning：**外資台指期未平倉** (smart-money 方向訊號)。"
        "`fini.net_oi`（外資多單 - 空單，正值 = 淨多）、"
        "`fini.change_5d`（5 日變化，> +1000 口偏多 / < -1000 口偏空）、"
        "`trend` 已自動分類 bullish/bearish/neutral。**外資台指期方向往往領先大盤 1-2 日**，"
        "若與 news_sentiment 同向則短線方向確立度高；逆向則優先信任此訊號。"
    ),
    "single_stock_futures_oi": (
        "- single_stock_futures_oi：**個股期貨外資未平倉變化前 N 名** "
        "(per-stock smart-money 方向訊號，PR #282)。每筆含 `symbol` / "
        "`contract_id` / `fini_net_oi`（最新淨倉，正 = 淨多）/ "
        "`fini_change`（5 日淨倉變化，正 = 外資多單建倉，負 = 空單建倉）/ "
        "`industry` / `name_zh`。**個股期貨外資方向通常領先個股現貨 1-2 日**，"
        "與 top_foreign_buyers（現貨）同向 = 短線確立度極高，逆向 = 優先信任期貨方。"
        "引用時請帶具體口數（例：「外資個股期 2330 連 5 日多單 +1500 口」）。"
    ),
    "taiwan_vix": (
        "- taiwan_vix：**臺指選擇權波動率指數**（VIX_TW，PR #283）。"
        "`value` = 最新收盤、`change_pct` = 5 日變化 %。"
        "與 overseas_indicators 的 `^VIX`（美 VIX）並列：兩者方向 / 強度差距"
        "反映台美避險溢價 spread，VIX_TW 跳升而 ^VIX 平靜 = 台股 idiosyncratic "
        "風險偏高（地緣 / 央行 / 個股突發）；同步跳升 = 全球 risk-off。"
        "**> 25 視為高度恐慌**（歷史中位數約 16-18），結論時應提及避險建議。"
        "引用時請帶具體值（例：「台 VIX 22.3 + 5d +18%，避險溢價擴大」）。"
    ),
    "upcoming_events_calendar": (
        "- upcoming_events_calendar：**未來 30 日法說會 / 除權息行事曆**"
        "（PR #284，市場層級，限本次討論已關注的標的）。每筆含 `symbol` / "
        "`next_event`（earnings / ex_dividend）/ `next_event_in_days`（距今天數）/ "
        "`next_event_date`。**事件前後幾日波動放大、方向難測**，分析師應主動避開"
        "或為事件風險預留調整空間（停損 / 部位縮減 / 套保）。"
        "引用時請點名具體標的 + 距今天數（例：「2330 法說 3 日後，建議事件前減倉」）。"
    ),
    "broker_concentration": (
        "- broker_concentration：**主力分點 5 日累計買賣超**"
        "（PR #285，每筆即一個 focus_symbol）。每筆含 `symbol` / "
        "`top_buyers`[broker, broker_id, net_buy_shares] / `top_sellers`[…] / "
        "`session_count`。**branch-dot pattern 是公司派 / 主力 / 大戶在做什麼的"
        "最直接訊號** — 同一家分點連續多日大買大賣，往往領先個股 3-5 日大行情。"
        "若 top_buyers / top_sellers 集中度高（前 3 大佔總額 > 50%），代表"
        "走勢有 single-actor driver，分析時應將該分點當作主軸；分散度高則"
        "表示資金無共識，技術分析權重應降低。引用時請點名具體分點 + 張數"
        "（例：「凱基台北 連 5 日淨買 +50K 張」），不要只說「主力大買」。"
    ),
    "margin_balance_trend":      "- margin_balance_trend：全市場融資 / 融券餘額趨勢（散戶槓桿與看空代理）。",
    "top_revenue_growers":       "- top_revenue_growers：最新月份營收年增率前 10（基本面）。",
    "active_buybacks":           "- active_buybacks：今日仍在執行庫藏股的公司，**強烈管理層信心訊號**。",
    "govt_bank_flow_5d":         "- govt_bank_flow_5d：八大行庫近 5 日累計買賣超（國家隊方向）。",
    "risk_warnings": (
        "- risk_warnings：**負向過濾**——`active_dispositions`（處置股）、"
        "`recent_suspensions`（近期暫停交易）、`high_day_trading_ratio`"
        "（當沖比 >60%，投機過熱）。**禁止推薦中招的標的，即使其他訊號看多。**"
    ),
    "market_institutional_5d":   "- market_institutional_5d：全市場三大法人近 5 日淨買賣超（大盤方向）。",
    "focus_briefs": (
        "- focus_briefs：**主題提及之個股小型分析師簡報**——`quote` 即時報價、"
        "`technicals`（MA20/60/120、52w 高低與距離、5/20/60 日漲跌幅、RSI14）、"
        "`fundamentals`（PE/PB/殖利率/EPS）、`revenue_trend`（近 6 月營收年/月增）、"
        "`chip_5d`（外資 / 投信 / 自營近 5 日淨買賣）、`margin_latest`（最新融資餘額）、"
        "`peers`（同產業 3 檔可比標的）。**有此區塊就要引用具體數據**，"
        "不要只憑 headlines 推論。"
    ),
    "macro": (
        "- macro：宏觀利率與匯率快照（Fed Funds / US 10Y / 10Y-2Y 殖利率價差 / "
        "DXY / TWD/USD），各帶 `latest_value` + `change_1y` + `change_3m`。"
        "影響全球風險偏好與外資流向，建議在結論中至少提及一次相關方向。"
    ),
    "overseas_indicators": (
        "- overseas_indicators：**隔夜美股 / 全球指數快照**（PR #269）——"
        "`indices` 內含 ^SOX (費半) / ^IXIC (NASDAQ) / ^GSPC (S&P 500) / "
        "^DJI (道瓊) / ^VIX (波動率指數)，各帶 `close` + `prev_close` + "
        "`change_pct`。**台股開盤前必看** — `^SOX` 對台積電等晶圓代工股有 1-2 日領先性，"
        "`^VIX` 跳升代表全球避險情緒升溫應降低部位風險。引用時請帶具體 % 變化"
        "（例如「SOX -2.3% 拖累半導體」），勿只提名稱。"
    ),
    "user_context": (
        "- user_context：**討論發起人本人的部位**——`portfolios`（組合清單）、"
        "`holdings`（前 20 大持股，含股數 / 平均成本 / 計價幣別）、"
        "`watchlist_symbols`（自選股，前 30）、`focus_overlap.held` "
        "（主題提及的標的中已持有者）、`focus_overlap.watching`（自選股中相關者）。"
        "**只在你的角色與部位配置 / 風險管理相關時引用**；其他情境忽略。"
        "**禁止在結論中揭露具體股數或成本價**——僅用於決策邏輯。"
    ),
    "prior_discussions": (
        "- prior_discussions：**本人過去 90 天對主題提及之標的所做的結論**，含 "
        "`created_at`（建立時間）/ `as_of_date`（回測日期，回測討論才有；"
        "live 為 null）/ topic 摘要 / `recommended_symbols` / `time_horizon` / "
        "`consensus_score` / `verdict`（win / loss / unverifiable / null）。"
        "**用以保持跨討論一致性**——若上次對 2330 結論 Hold 而本次卻要 Buy，"
        "必須在 content 中明確說明改變理由（例如「上週起殖利率下行 +30bp，重新評估」），"
        "不可默默翻盤。引用過去討論時請優先以 as_of_date（若有）為錨，更貼近「在那一天的判斷」。"
    ),
    "recent_lessons": (
        "- recent_lessons：**過去事後檢討學到的教訓**——`market` 為全市場通用教訓"
        "（time-decay 排序），`per_symbol` 為主題提及之標的的歷史教訓"
        "（同 symbol 加權）。每條含 `as_of_date` / `category` "
        "(missed_sector | wrong_signal_weight | missing_data | "
        "over_confidence | other) / `lesson_text` (≤ 80 字反思) / "
        "`related_symbols` / `missed_winners`。**這是「上次踩坑的具體紀錄」**——"
        "回應前先掃過，避免重蹈覆轍；若這次的論點正好在 lesson_text 警示的"
        "範圍內，必須明確處理（要麼提出新證據反駁，要麼承認並調整）。"
        "若 lessons 為空表示尚未累積足夠歷史教訓，正常進行即可。"
    ),
    "errors":                    "- errors：本次抓取的連接器錯誤清單；非空時務必聲明資料不完整。",
}

_SCHEMA_HEADER = (
    "## 市場現況解讀提示\n"
    "下方 `## 市場現況` 的 JSON 包含多個訊號區塊，請依語意整合判讀：\n"
)


def _persona_schema_annotation(ctx: dict[str, Any]) -> str:
    """Build a schema annotation listing only the blocks present in
    `ctx`. Avoids the failure mode where the prompt advertises
    `top_gainers` / `risk_warnings` to a `macro_analyst` whose
    filtered ctx didn't carry them — the LLM would either invent
    values or apologise about missing data, both equally bad.

    Block ordering follows `_BLOCK_ANNOTATIONS` insertion order so
    the prompt structure stays stable across personas. A block is
    "present" if its key is in `ctx` AND the value isn't None / [] /
    {} (matches how `gather_market_context` initialises empty
    blocks). `errors` is always included so personas know what
    `errors: []` means even on a clean run.
    """
    bullets: list[str] = []
    for block, line in _BLOCK_ANNOTATIONS.items():
        if block == "errors":
            bullets.append(line)
            continue
        val = ctx.get(block)
        if val is None or val == [] or val == {}:
            continue
        bullets.append(line)
    return _SCHEMA_HEADER + "\n".join(bullets)


# Full annotation kept for `synthesize_conclusion` (which sees the
# unfiltered context snapshot, so the full block list is always
# accurate). Built once at import time.
_CONTEXT_SCHEMA_ANNOTATION = _SCHEMA_HEADER + "\n".join(
    _BLOCK_ANNOTATIONS.values()
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
    "{annotation}\n\n"
    "## 市場現況\n```json\n{context}\n```\n\n"
    "## 訊號引用準則（高優先）\n"
    "為避免「資料看了沒用、結論自由心證」，content 中引用 ## 市場現況 的訊號時"
    "必須遵守以下三條：\n"
    "  1. **引用具體數值**，不要只用一般性形容詞。\n"
    "     ✗ 「RSI 偏高」、「外資動向轉弱」\n"
    "     ✓ 「RSI 67.3 已逼近超買區」、「外資台指期淨空 5 日 +3500 口」\n"
    "  2. **每個與本主題相關的訊號區塊都要至少點名一次**（即使結論是「該訊號"
    "與本案無顯著關聯」也要明寫，不要默默跳過）。判斷相關性的標準：annotation"
    "中該區塊的描述若觸及主題提到的個股 / 產業 / 時間視窗，即屬相關。\n"
    "  3. **訊號之間互相印證或衝突要明寫**。例：「news_sentiment 偏多但"
    "taifex_positioning trend=bearish — 兩者相左，優先信任後者，因外資期貨"
    "部位通常領先大盤 1-2 日」。單一訊號可被忽略，多訊號互證或互斥不可。\n\n"
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


def _summarize_turn_content(content: str) -> str:
    """Compress a turn's full content down to one line for the older-
    history block. Strips markdown emphasis + collapses whitespace +
    truncates at `_HISTORY_SUMMARY_CHARS`. The persona doesn't need
    the full text from 4 rounds ago — only the gist of the speaker's
    previous position so they can spot drift / contradictions."""
    body = (content or "").strip()
    if not body:
        return "（同意，無補充）"
    # Drop markdown bold / italic markers + bullet hyphens.
    body = body.replace("**", "").replace("__", "")
    # Collapse whitespace + newlines.
    body = " ".join(body.split())
    if len(body) > _HISTORY_SUMMARY_CHARS:
        body = body[:_HISTORY_SUMMARY_CHARS] + "…"
    return body


def _format_history(prior_turns: list[DiscussionTurn]) -> str:
    """Build the `## 先前發言` block for a persona prompt.

    Two-tier compression keeps the prompt budget bounded:
      - The N most recent turns (`_FULL_HISTORY_TURNS`) appear in
        full — these are the live debate the persona is reacting to.
      - Older turns up to `_MAX_HISTORY_TURNS` appear as a single-
        line summary so the persona retains continuity ("buffett 第
        1 輪看好 2330, 我此輪也補強") without paying for verbatim
        text from rounds ago.

    The full window comes after the summary block so the LLM's
    recency bias works in our favour — the most recent turn is the
    last thing in the prompt before its own "你現在的任務" line.
    """
    if not prior_turns:
        return "（你是本場第一位發言者）"
    window = prior_turns[-_MAX_HISTORY_TURNS:]
    if len(window) <= _FULL_HISTORY_TURNS:
        recent = window
        older: list[DiscussionTurn] = []
    else:
        split = len(window) - _FULL_HISTORY_TURNS
        older = window[:split]
        recent = window[split:]

    def _render(t: DiscussionTurn, body: str) -> str:
        # User injections aren't analyst opinions — render them as a
        # directive from the discussion's owner so personas know the
        # next round must respond to it. Keeps the same `第N輪` prefix
        # for ordering / recency cues.
        if t.persona_id == USER_PERSONA_ID:
            return f"- 第{t.round}輪 · 【討論發起人插話】：{body}"
        return f"- 第{t.round}輪 · {t.persona_id} · {t.stance}：{body}"

    sections: list[str] = []
    if older:
        sections.append("（較早輪次摘要）")
        for t in older:
            sections.append(_render(t, _summarize_turn_content(t.content)))
    if older and recent:
        sections.append("")
        sections.append("（最近發言全文）")
    for t in recent:
        body = t.content.strip() or "（同意，無補充）"
        sections.append(_render(t, body))
    return "\n".join(sections)


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


# Provider names whose `_openai_compat_tool_loop` carries the same
# OpenAI-style tools=[...] schema. Mirrors `_OPENAI_COMPAT_PROVIDERS`
# in `api/ai_agents/router.py`; kept narrowly here so the discussion
# service doesn't import the chat-router module (which pulls FastAPI
# into the test path for free).
_OPENAI_COMPAT_TOOL_PROVIDERS = ("minimax", "groq", "deepseek", "openrouter")


def _build_persona_tool_kwargs(
    *, provider: str, user_role: str | None, user_id: str | None,
) -> dict[str, Any]:
    """Return the tool-related kwargs to forward to `stream_chat` for
    a single persona turn.

    Mirrors the eligibility rules at `/api/ai/chat`:
      - `claude_agent` provider: when the SDK is importable + the
        owner has analyst / admin role, build the MCP toolset and
        cap turns at `CLAUDE_AGENT_MAX_TURNS`.
      - OpenAI-compat providers: when the owner has analyst / admin
        role, build the OpenAI-compat toolset; viewers fall back to
        plain chat (the `query_user_data` tool reads the caller's
        own data, so handing it to a viewer would let an account
        with low quota silently exfiltrate via tool calls).
      - Any other provider, or any role/SDK gate failing: returns
        an empty dict so the caller falls through to today's plain
        streaming.

    Errors building the toolset (SDK import failure, key fetcher
    blowing up) are swallowed + logged so a single tool-config
    issue never breaks the discussion round — the persona just
    gets a tool-less turn and the rest of the round continues.
    """
    if not user_id:
        return {}
    role = (user_role or "").lower()
    if role not in ("analyst", "admin"):
        return {}

    prov = (provider or "").lower()
    if prov == "claude_agent":
        if not settings.claude_agent_effective_enabled:
            return {}
        try:
            from ai.tools import build_toolset, tool_names
            return {
                "mcp_server":    build_toolset(user_id),
                "allowed_tools": tool_names(),
                "max_turns":     settings.CLAUDE_AGENT_MAX_TURNS,
            }
        except Exception as exc:
            log.warning(
                "discussion.tools.claude_agent.build_failed",
                extra={"user_id": user_id, "error": str(exc)},
            )
            return {}

    if prov in _OPENAI_COMPAT_TOOL_PROVIDERS:
        try:
            from ai.tools.openai_compat import build_openai_compat_toolset
            schemas, dispatch = build_openai_compat_toolset(user_id)
        except Exception as exc:
            log.warning(
                "discussion.tools.openai_compat.build_failed",
                extra={"user_id": user_id, "provider": prov, "error": str(exc)},
            )
            return {}
        max_turns_attr = {
            "minimax":    "MINIMAX_MAX_TURNS",
            "groq":       "GROQ_MAX_TURNS",
            "deepseek":   "DEEPSEEK_MAX_TURNS",
            "openrouter": "OPENROUTER_MAX_TURNS",
        }[prov]
        return {
            "openai_tool_schemas":  schemas,
            "openai_tool_dispatch": dispatch,
            "max_turns":            getattr(settings, max_turns_attr, 5),
        }

    return {}


# ── per-persona context filtering ──────────────────────────────────
#
# Sending the full `gather_market_context` payload (~12-15 blocks) to
# every persona costs tokens that don't help: a `macro_analyst` doesn't
# benefit from the `risk_warnings` 處置股 list, and `dalio` doesn't
# care about per-symbol `top_revenue_growers`. Worse, more text =
# more attention dilution — weak models start mixing chip-flow data
# into a macro thesis it shouldn't be in.
#
# `_PERSONA_CONTEXT_PROFILES` enumerates the blocks each persona
# actually wants. Personas absent from the registry fall through to
# `_ALL_PERSONA_BLOCKS` (current behaviour — full context). Admin
# overrides on persona provider/model don't affect filtering — the
# filter is keyed on the canonical persona_id so reskinning Buffett's
# LLM doesn't change what data he cares about.
#
# Always-included keys (top of every persona's view) are the metadata
# the prompt template references unconditionally: `market`,
# `captured_at`, and `errors` (so personas can mention "data was
# incomplete" without us having to enumerate per-archetype).

_ALWAYS_INCLUDED_BLOCKS: frozenset[str] = frozenset({
    "market", "captured_at", "errors",
    # `prior_discussions` is meta-context — past consensus on the
    # same symbols matters to every archetype (a value persona
    # should know we said Hold last week; a quant persona should
    # see when their last momentum call was wrong). Cheap because
    # it's per-symbol-overlap-only.
    "prior_discussions",
    # `recent_lessons` carries past post-mortem takeaways for the
    # same market + per focus_symbol — every archetype benefits
    # from "what we got wrong last time" so it's universal. The
    # block is bounded by the runtime LESSONS_PER_*_LIMIT
    # tunables so token cost stays predictable.
    "recent_lessons",
})

# Full block set — used as the fall-through profile and the union the
# filter compares against. Kept in sync manually with the keys
# `gather_market_context` populates.
_ALL_PERSONA_BLOCKS: frozenset[str] = frozenset({
    "top_gainers", "top_losers", "index",
    "news_sentiment", "per_symbol_news_sentiment", "international_sentiment",
    "short_term_signals",
    "top_foreign_buyers", "margin_balance_trend", "top_revenue_growers",
    "active_buybacks", "govt_bank_flow_5d", "risk_warnings",
    "market_institutional_5d", "taifex_positioning",
    "single_stock_futures_oi",   # PR #282: per-stock 外資 期貨 net OI shift
    "taiwan_vix",                # PR #283: 台指選擇權波動率指數
    "upcoming_events_calendar",  # PR #284: market-wide 法說 / 除息 calendar
    "broker_concentration",      # PR #285: per-focus-symbol 主力分點
    "overseas_indicators",   # PR #270: SOX/NDX/SPX/DJI/VIX overnight snapshot
    "focus_briefs", "macro", "user_context", "prior_discussions",
    "recent_lessons",
})

# Five archetypes cover the 19 personas without enumerating each one.
# Each profile is the union of "what this persona uses to form a view".
_MACRO_PROFILE = frozenset({
    "index", "macro", "international_sentiment",
    "overseas_indicators",   # PR #270: SOX/NDX/SPX/DJI/VIX — core macro signal
    "taiwan_vix",            # PR #283: TW implied-vol regime
    "top_foreign_buyers", "govt_bank_flow_5d", "market_institutional_5d",
    "taifex_positioning",   # smart-money directional signal — central to macro view
    "news_sentiment", "per_symbol_news_sentiment",
    # PR #219: macro personas need focus_briefs when the topic
    # names specific stocks. "Fed cuts → 2330 受惠" requires seeing
    # 2330's PE / valuation band — without focus_briefs, dalio /
    # macro_analyst can only deliver pure macro views and never
    # ground them in a tradeable name. focus_briefs is empty when
    # focus_symbols is empty, so this is free for non-symbolic
    # topics (no token cost penalty).
    "focus_briefs",
})

_VALUE_PROFILE = frozenset({
    "focus_briefs", "per_symbol_news_sentiment", "top_revenue_growers",
    "active_buybacks", "news_sentiment", "macro",
    "upcoming_events_calendar",  # PR #284: event windows for entry/exit timing
})

_CONTRARIAN_PROFILE = frozenset({
    "top_losers", "risk_warnings", "margin_balance_trend",
    "focus_briefs", "news_sentiment", "per_symbol_news_sentiment",
    "short_term_signals",   # RSI extremes + volume spikes flag reversals
    "taifex_positioning",   # extreme net OI = contrarian setup
    "single_stock_futures_oi",  # PR #282: extreme per-stock futures OI also a contrarian setup
    "taiwan_vix",               # PR #283: 台 VIX > 25 = panic = contrarian setup
    "upcoming_events_calendar", # PR #284: events create dislocation opportunities
    "broker_concentration",     # PR #285: 主力分點 lockstep = setup confirmation/contrarian alarm
    "overseas_indicators",  # PR #270: ^VIX spikes = global panic = contrarian setup
    "macro",
})

_QUANT_PROFILE = frozenset({
    "focus_briefs", "top_gainers", "top_losers",
    "top_foreign_buyers", "market_institutional_5d", "margin_balance_trend",
    "risk_warnings", "news_sentiment", "macro",
    "short_term_signals",   # core Tier-1 quant signals per focus symbol
    "taifex_positioning",   # market-wide directional bias
    "single_stock_futures_oi",  # PR #282: per-stock futures smart-money lead
    "taiwan_vix",               # PR #283: TW VIX as a vol-regime filter
    "upcoming_events_calendar", # PR #284: event-risk gating on entries
    "broker_concentration",     # PR #285: per-focus-symbol 主力分點
    "overseas_indicators",  # PR #270: SOX overnight gap is the dominant TW open driver
})

_PORTFOLIO_PROFILE = frozenset({
    "user_context", "focus_briefs", "per_symbol_news_sentiment",
    "short_term_signals",   # short-term entry/exit timing for held names
    "macro", "news_sentiment", "international_sentiment",
    "overseas_indicators",  # PR #270: risk-on/off positioning needs ^VIX read
    "upcoming_events_calendar",  # PR #284: position sizing around earnings
})

_PERSONA_CONTEXT_PROFILES: dict[str, frozenset[str]] = {
    # CFA-style functional
    "market_analyst":    _QUANT_PROFILE,
    "portfolio_advisor": _PORTFOLIO_PROFILE,
    "risk_manager":      _CONTRARIAN_PROFILE | {"user_context"},
    "macro_analyst":     _MACRO_PROFILE,
    "earnings_analyst":  _VALUE_PROFILE,
    "trading_coach":     _QUANT_PROFILE,
    # `claude_research` has tools — give it everything so it has the
    # full picture before deciding which tool to call.
    "claude_research":   _ALL_PERSONA_BLOCKS,

    # Value / quality investors
    "buffett":  _VALUE_PROFILE,
    "graham":   _VALUE_PROFILE,
    "munger":   _VALUE_PROFILE | {"risk_warnings"},
    "lynch":    _VALUE_PROFILE | {"top_gainers"},
    "fisher":   _VALUE_PROFILE,
    "smith":    _VALUE_PROFILE,

    # Contrarian
    "marks":    _CONTRARIAN_PROFILE,
    "klarman":  _CONTRARIAN_PROFILE,

    # Macro
    "dalio":    _MACRO_PROFILE,
    "soros":    _MACRO_PROFILE | {"top_gainers", "focus_briefs"},

    # Quant
    "simons":   _QUANT_PROFILE,
    "asness":   _QUANT_PROFILE,
}


def _filter_context_for_persona(
    ctx: dict[str, Any], persona_id: str,
) -> dict[str, Any]:
    """Project `ctx` down to the blocks `persona_id` actually uses,
    plus the always-included metadata keys.

    Personas not in `_PERSONA_CONTEXT_PROFILES` (custom personas, new
    additions, typos) get the full context — fail-open so an unknown
    persona never silently loses data, just costs more tokens.
    """
    profile = _PERSONA_CONTEXT_PROFILES.get(persona_id)
    if profile is None:
        return ctx
    allowed = _ALWAYS_INCLUDED_BLOCKS | profile
    return {k: v for k, v in ctx.items() if k in allowed}


# Appended to the user prompt when the persona has tools available so
# the LLM is reminded that fabricating numbers is never necessary.
# Listed tool names mirror what `build_toolset` / `build_openai_compat_
# toolset` ship.
_PERSONA_TOOL_USAGE_HINT = (
    "\n\n## 工具可用\n"
    "你本回合可以呼叫下列工具取得即時數據（傳回值已在工具結果中）："
    "`get_quote` / `run_dcf` / `run_var` / `run_backtest` / `query_user_data`。"
    "**禁止虛構數據** — 若需要某個數字而 `## 市場現況` 與 `focus_briefs` 找不到，"
    "請呼叫對應工具，再把結果寫進 content。每次工具呼叫會自動計入流程，"
    "你只需專注在分析。"
)


async def _ask_persona(
    db: AsyncSession,
    *,
    spec: "AgentSpec",
    persona_id: str,
    topic: str,
    rules: str,
    context: dict[str, Any],
    prior_turns: list[DiscussionTurn],
    user_id: str | None,
    user_role: str | None = None,
) -> AsyncGenerator[dict, None]:
    """Yield raw stream events from one persona's turn. Caller assembles
    the deltas + parses the final JSON.

    Takes a pre-resolved `AgentSpec` so callers can batch-load the
    persona roster's overrides up-front (avoiding an N+1 round trip
    inside the per-persona loop). When the persona's resolved provider
    supports tools and the discussion's owner has the right role,
    `_build_persona_tool_kwargs` adds the MCP / OpenAI-compat toolset
    so the persona can call get_quote / run_dcf / run_var / run_backtest
    / query_user_data instead of guessing from the static context.

    `persona_id` is the canonical agent ID (e.g. `buffett`,
    `macro_analyst`) — used to look up the per-persona context profile
    so the LLM only sees the blocks its archetype actually uses.
    """
    tool_kwargs = _build_persona_tool_kwargs(
        provider=spec.default_provider,
        user_role=user_role,
        user_id=user_id,
    )
    # Filter context down to blocks this persona actually uses — saves
    # tokens and stops weak models from mixing irrelevant blocks (e.g.
    # macro_analyst citing risk_warnings dispositions in a Fed thesis).
    filtered_ctx = _filter_context_for_persona(context, persona_id)
    # Build the schema annotation from what the persona actually sees,
    # not the full block list — otherwise the prompt advertises blocks
    # the LLM can't find in `## 市場現況` and forces it to either
    # hallucinate values or apologise about missing data.
    annotation = _persona_schema_annotation(filtered_ctx)
    user_prompt = _TURN_PROMPT_TEMPLATE.format(
        topic=topic,
        rules=rules,
        annotation=annotation,
        context=json.dumps(filtered_ctx, ensure_ascii=False, indent=2),
        history=_format_history(prior_turns),
    )
    if tool_kwargs:
        user_prompt += _PERSONA_TOOL_USAGE_HINT
    messages = [
        {"role": "system", "content": spec.system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    try:
        from services.runtime_config_service import get_int as _runtime_get_int
        turn_max_tokens = await _runtime_get_int(db, "DISCUSSION_TURN_MAX_TOKENS")
    except Exception as exc:
        log.warning("discussion.runtime_config_failed",
                    extra={"setting": "DISCUSSION_TURN_MAX_TOKENS", "error": str(exc)})
        turn_max_tokens = settings.DISCUSSION_TURN_MAX_TOKENS
    async for event in stream_chat(
        messages=messages,
        provider=spec.default_provider,
        model=spec.default_model,
        max_tokens=turn_max_tokens,
        temperature=0.4,
        db=db,
        user_id=user_id,
        **tool_kwargs,
    ):
        yield event


async def run_round(
    db: AsyncSession,
    discussion: Discussion,
    *,
    user_id: str | None = None,
    user_role: str | None = None,
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
      - tool_call    {round, turn_index, persona_id, id, name, args}
      - tool_result  {round, turn_index, persona_id, id, name, summary, is_error}
      - turn_end     {round, turn_index, persona_id, stance, content}
      - round_end    {round, turn_count}
      - error        {message}            (terminal)

    `user_role` (the discussion owner's role) gates tool availability
    on tool-capable providers — analyst / admin get a real toolset,
    viewers fall through to plain streaming. Providers without tool
    support ignore the role entirely.
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
        focus = extract_focus_symbols(
            discussion.topic, market=discussion.market,
        )
        # Bridge ctx-gathering progress milestones to SSE events so
        # the frontend's preparing card (PR #244) can show
        # "scoring news sentiment..." etc. instead of a static
        # "loading..." for the full 15-30 s window. Asyncio.Queue +
        # sentinel pattern: gather runs as a task; we drain the queue
        # in this generator and yield ctx_progress events; gather's
        # finally puts None as a "done" marker so we exit the loop.
        progress_q: asyncio.Queue[str | None] = asyncio.Queue()

        async def _emit_progress(stage: str) -> None:
            await progress_q.put(stage)

        async def _gather_then_signal() -> dict[str, Any]:
            try:
                return await gather_market_context(
                    db,
                    market=discussion.market,
                    focus_symbols=focus,
                    owner_id=discussion.owner_id,
                    exclude_discussion_id=discussion.id,
                    as_of=discussion.as_of_date,
                    progress_cb=_emit_progress,
                )
            finally:
                # Sentinel — signals to the drainer that no more
                # progress events are coming so it can break out and
                # await the result. Always fires (success or failure)
                # so the caller never deadlocks.
                await progress_q.put(None)

        ctx_task = asyncio.create_task(_gather_then_signal())
        while True:
            stage = await progress_q.get()
            if stage is None:
                break
            yield TurnEvent("ctx_progress", {"stage": stage})
        # Re-raise any exception the gather hit. The sentinel was
        # still fired by the finally block above, so we got here
        # cleanly.
        context = await ctx_task
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
                        persona_id=persona_id,
                        topic=discussion.topic,
                        rules=discussion.rules,
                        context=context,
                        prior_turns=prior_turns,
                        user_id=user_id,
                        user_role=user_role,
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
                        elif etype == "tool_call":
                            # Forward through so the SSE consumer can
                            # show "buffett 正在執行 run_dcf" inline.
                            # Tool-call rounds inside the LLM loop are
                            # already capped by the provider's
                            # `max_turns`; we don't keep a counter here.
                            yield TurnEvent("tool_call", {
                                "round": round_number,
                                "turn_index": idx,
                                "persona_id": persona_id,
                                "id":   event.get("id"),
                                "name": event.get("name"),
                                "args": event.get("args"),
                            })
                        elif etype == "tool_result":
                            yield TurnEvent("tool_result", {
                                "round": round_number,
                                "turn_index": idx,
                                "persona_id": persona_id,
                                "id":       event.get("id"),
                                "name":     event.get("name"),
                                "summary":  event.get("summary", ""),
                                "is_error": event.get("is_error", False),
                            })
                        elif etype == "usage":
                            # **Sum** usage across events instead of
                            # overwriting (PR #216). When the persona's
                            # provider goes through a tool loop —
                            # claude_agent's MCP loop or
                            # _openai_compat_tool_loop's max_turns
                            # iteration — each LLM call emits its
                            # own usage event. The earlier code took
                            # only the LAST one, so a 5-turn tool run
                            # dropped ~80% of the actual prompt token
                            # cost. Now every event is added in.
                            if usage_seen is None:
                                usage_seen = {"prompt_tokens": 0, "completion_tokens": 0}
                            usage_seen["prompt_tokens"] += int(
                                event.get("prompt_tokens", 0)
                            )
                            usage_seen["completion_tokens"] += int(
                                event.get("completion_tokens", 0)
                            )
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
    """Render the full transcript for the synthesizer prompt.

    Persona turns get a `[第N輪/persona_id/stance]` prefix.
    User-input turns (e.g. post-mortem self-critique prompts
    injected via `inject_user_message`) are rendered as a clearly
    labelled directive — without this distinction the synthesizer
    LLM treats `_user` as just another analyst and may try to
    *answer* the directive's questions in the `reasoning` field
    instead of synthesizing the discussion. The longer post-mortem
    prompt (4 structured questions) makes that failure mode worse:
    the LLM dumps a long answer + runs out of `max_tokens` mid-JSON
    and the response fails to parse.
    """
    if not turns:
        return "（無發言）"
    lines = []
    for t in turns:
        body = t.content.strip() or "（同意，無補充）"
        if t.persona_id == USER_PERSONA_ID:
            lines.append(
                f"[第{t.round}輪 · 討論發起人指示（請納入後續分析考量，"
                f"勿視為專家意見、勿直接回答其中的提問）]\n{body}"
            )
        else:
            lines.append(f"[第{t.round}輪/{t.persona_id}/{t.stance}] {body}")
    return "\n".join(lines)


def _try_repair_truncated_json(text: str) -> str | None:
    """Best-effort recovery for JSON output that got truncated mid-
    emission (typical when an LLM hits `max_tokens` while writing the
    object). Walks from the first `{`, tracks string + bracket depth,
    and on EOF closes any open string + dangling brackets so the
    result is parseable.

    Returns the repaired substring, or None when the input has no
    `{` to anchor on. Caller still pipes through `_loads_lenient` —
    repair only fixes the structural truncation, not relaxed-JSON
    quirks (those layers stack).

    Conservative: trims trailing `,` / `:` since closing immediately
    after either is invalid. The recovered fields may be partial
    (e.g. `reasoning` cut mid-sentence), but a partial conclusion
    is infinitely more useful than a parse-error placeholder for
    the operator triaging the discussion.
    """
    start = text.find("{")
    if start < 0:
        return None
    in_string = False
    escape = False
    bracket_stack: list[str] = []   # "{" / "["
    last_real_char = ""
    for c in text[start:]:
        if escape:
            escape = False
            continue
        if in_string:
            if c == "\\":
                escape = True
                continue
            if c == '"':
                in_string = False
            continue
        if c == '"':
            in_string = True
            last_real_char = c
            continue
        if c.isspace():
            continue
        last_real_char = c
        if c in "{[":
            bracket_stack.append(c)
        elif c == "}":
            if bracket_stack and bracket_stack[-1] == "{":
                bracket_stack.pop()
        elif c == "]":
            if bracket_stack and bracket_stack[-1] == "[":
                bracket_stack.pop()

    if not in_string and not bracket_stack:
        return None   # already balanced — caller's earlier pass covers it

    body = text[start:].rstrip()
    # Drop a dangling trailing comma or colon (illegal right before
    # a close-bracket).
    while body and body[-1] in ",:":
        body = body[:-1].rstrip()

    suffix = ""
    if in_string:
        suffix += '"'
    # If we ended right after a `:` (key with no value), drop the
    # whole key/value pair to avoid `"foo":}` which is illegal.
    if last_real_char == ":":
        # Walk back to the last `,` or `{` and trim the orphan key.
        for i in range(len(body) - 1, -1, -1):
            if body[i] in ",{":
                body = body[:i + 1] if body[i] == "," else body[:i + 1]
                if body and body[-1] == ",":
                    body = body[:-1]
                break
    for opener in reversed(bracket_stack):
        suffix += "}" if opener == "{" else "]"
    return body + suffix


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
    # PR #267: last-resort repair for truncated JSON. Reasoning
    # models (M2.7 etc.) under post-mortem load can dump a long
    # chain-of-thought + start the JSON, then run out of
    # `max_tokens` mid-object — `_extract_json_object` returns
    # None because the closing `}` was never emitted. Try closing
    # any open string + brackets and re-parse before falling
    # through to the parse-error placeholder.
    if data is None:
        repaired = _try_repair_truncated_json(cleaned)
        if repaired is not None:
            try:
                data = _loads_lenient(repaired)
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
    # Reuse the most recent round's context snapshot so the synthesiser
    # reasons over the same evidence the personas saw, instead of
    # pulling a fresh `gather_market_context` (which would silently
    # drift if the round was run pre-close and the synthesise runs
    # post-close, or if a connector started failing in between).
    # Falls back to a fresh fetch only for legacy discussions whose
    # rounds predate the round-context snapshot table — keeps old
    # rows synthesisable without manual backfill.
    snapshots = await get_round_contexts(db, discussion_id=discussion.id)
    if snapshots:
        context = snapshots[-1].context
    else:
        focus = extract_focus_symbols(
            discussion.topic, market=discussion.market,
        )
        context = await gather_market_context(
            db,
            market=discussion.market,
            focus_symbols=focus,
            owner_id=discussion.owner_id,
            exclude_discussion_id=discussion.id,
            as_of=discussion.as_of_date,
        )

    # PR #267: detect a post-mortem self-critique cycle in the
    # transcript so the synthesizer prompt can call it out and the
    # token budget can be sized appropriately. Without this guidance
    # the synthesizer treats the round-2 reflections as just more
    # opinions and silently double-weights the original round-1
    # consensus; with longer post-mortem transcripts the JSON also
    # tends to truncate within the default budget.
    has_post_mortem = any(
        t.persona_id == USER_PERSONA_ID
        and (t.content or "").lstrip().startswith("【事後檢討")
        for t in turns
    )

    user_prompt = _SYNTHESIZER_USER_TEMPLATE.format(
        topic=discussion.topic,
        rules=discussion.rules,
        context=json.dumps(context, ensure_ascii=False, indent=2),
        transcript=_format_transcript(turns),
    )

    # PR-C: when this discussion was spawned by a sweep whose
    # parent strategy has learned persona weights, surface them as
    # a tie-breaker hint so the synthesizer can lean on personas
    # with a track record. Returns "" when no weights are available
    # (live discussions, sweeps not tied to a template, fresh
    # templates not yet trained).
    if discussion.sweep_id is not None:
        from models.backtest_sweep import BacktestSweep
        from models.discussion_strategy_template import (
            DiscussionStrategyTemplate,
        )
        from services.persona_weight_learner import (
            format_weights_for_synthesizer,
        )
        sweep_row = await db.scalar(
            select(BacktestSweep).where(BacktestSweep.id == discussion.sweep_id)
        )
        if sweep_row is not None and sweep_row.strategy_id is not None:
            tmpl = await db.scalar(
                select(DiscussionStrategyTemplate).where(
                    DiscussionStrategyTemplate.id == sweep_row.strategy_id,
                )
            )
            if tmpl is not None and tmpl.persona_weights:
                user_prompt += format_weights_for_synthesizer(
                    dict(tmpl.persona_weights),
                )

    if has_post_mortem:
        user_prompt += (
            "\n\n## 補充提示（事後檢討模式）\n"
            "本場討論已包含一輪「事後檢討」（基於實際漲幅排行榜的反思），"
            "請以 personas 在事後檢討輪 (latest round) 的最終立場為主，"
            "整理出修正後的結論。\n"
            "**仍然只能輸出合法 JSON**，不要回答事後檢討提示中的問題、"
            "不要在 JSON 外加任何解說。\n"
            "\n"
            "**額外輸出 `lessons` 欄位**：在 conclusion JSON 內加入 "
            '`"lessons": [...]` 陣列（最多 5 條），把這場事後檢討'
            "學到的事拆成可重用的教訓供未來討論參考。每條 shape：\n"
            "  - `category`：missed_sector | wrong_signal_weight | "
            "missing_data | over_confidence | other 五選一\n"
            "  - `lesson_text`：**繁體中文 ≤ 80 字**，具體點名訊號 / 產業 / "
            "股票，避免「下次要更小心」這類空話\n"
            "  - `related_symbols`：教訓涉及的股票代號（陣列，可空）\n"
            "  - `missed_winners`：漲幅榜上錯過的代號（陣列，可空）\n"
            "若這場討論沒有具體可重用的教訓（純粹是 random walk），"
            "回傳空陣列 `\"lessons\": []`，不要硬擠。"
        )

    messages = [
        {"role": "system", "content": _SYNTHESIZER_SYSTEM},
        {"role": "user", "content": user_prompt},
    ]

    assembled = ""
    usage_seen: dict[str, int] | None = None
    # `max_tokens` is admin-tunable via RuntimeTunablesCard
    # (`DISCUSSION_SYNTHESIZER_MAX_TOKENS`, default 8192). Default gives
    # reasoning models enough room for chain-of-thought (~3-5K tokens)
    # before the conclusion JSON (~1.5K tokens) is emitted.
    try:
        from services.runtime_config_service import get_int as _runtime_get_int
        synth_max_tokens = await _runtime_get_int(
            db, "DISCUSSION_SYNTHESIZER_MAX_TOKENS",
        )
    except Exception as exc:
        log.warning("discussion.runtime_config_failed",
                    extra={"setting": "DISCUSSION_SYNTHESIZER_MAX_TOKENS",
                           "error": str(exc)})
        synth_max_tokens = settings.DISCUSSION_SYNTHESIZER_MAX_TOKENS

    # Post-mortem transcripts are 1.5-2x longer than a single-round
    # discussion (round-1 turns + post-mortem prompt + round-2
    # self-critique replies). Reasoning models need proportionally
    # more output room or the JSON gets truncated mid-object and
    # `_safe_conclusion` falls back to the parse-error placeholder.
    # Bump 50% as a conservative safety margin.
    if has_post_mortem:
        synth_max_tokens = int(synth_max_tokens * 1.5)
    try:
        async for event in stream_chat(
            messages=messages,
            provider=provider,
            model=model,
            max_tokens=synth_max_tokens,
            temperature=0.2,
            db=db,
            user_id=user_id,
        ):
            etype = event.get("type")
            if etype == "delta":
                assembled += event.get("text", "")
            elif etype == "usage":
                # Accumulate (PR #216) — synth doesn't usually run a
                # tool loop today (system task uses a vanilla provider),
                # but the same accumulator pattern protects us when an
                # admin retargets the synthesizer at a tool-capable
                # provider via SystemTasksCard.
                if usage_seen is None:
                    usage_seen = {"prompt_tokens": 0, "completion_tokens": 0}
                usage_seen["prompt_tokens"] += int(
                    event.get("prompt_tokens", 0)
                )
                usage_seen["completion_tokens"] += int(
                    event.get("completion_tokens", 0)
                )
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
    # PR #272: route the write based on whether the transcript already
    # carries a post-mortem self-critique. With post-mortem present,
    # land the synthesizer's output in `post_mortem_conclusion` so
    # the original `conclusion` is preserved for side-by-side
    # comparison in the UI. Without post-mortem, the existing
    # behaviour (overwrite `conclusion`) is unchanged.
    if has_post_mortem:
        discussion.post_mortem_conclusion = conclusion
    else:
        discussion.conclusion = conclusion
    discussion.status = STATUS_DONE
    discussion.updated_at = datetime.now(UTC)
    # Seed `verify_after_date` so the verifier task picks this row up
    # in 5 trading days (PR #218). Used to be set only by the auto-run
    # cron, which left manual discussions permanently un-graded —
    # `prior_discussions` then surfaced `verdict=null` for nearly
    # every cross-session reference, defeating the consistency check.
    # Skip when already set (re-conclude after edit) so we don't
    # push the verification window back artificially.
    #
    # The verifier still grades the ORIGINAL conclusion's recommended
    # symbols — post_mortem_conclusion is informational. If we ever
    # want the verifier to use the post-mortem version instead, we'd
    # update score_discussion_outcomes to prefer the latter when
    # populated; that's intentionally NOT in this PR.
    if discussion.verify_after_date is None:
        from services.tw_trading_calendar import (
            add_trading_days_estimate,
            utcnow_tw_date,
        )
        # 5 trading days matches the verifier's `_WINDOW_TRADING_DAYS`
        # — anything sooner and the bars haven't all resolved yet.
        # In backtest mode (PR #224), anchor on `as_of_date` instead
        # of today so the post-window is the historical 5 trading
        # days after the backtest anchor — verifier picks the row
        # up immediately if as_of + 5d is already in the past.
        anchor = discussion.as_of_date or utcnow_tw_date()
        discussion.verify_after_date = add_trading_days_estimate(anchor, 5)
    await db.commit()
    await db.refresh(discussion)

    # Best-effort: extract structured lessons from the post-mortem
    # synthesizer pass + persist them for the learning loop. Only
    # fires under post-mortem mode (the prompt's `lessons` ask is
    # only included then) and only for backtest discussions
    # (`as_of_date` carries the anchor needed for time-decay
    # scoring). Failure here is non-fatal — the conclusion is
    # already committed; missing a write just means this run
    # didn't contribute to the learning archive.
    if has_post_mortem and discussion.as_of_date is not None:
        try:
            raw_obj = _extract_lessons_payload(assembled)
            if raw_obj:
                from services.discussion_lesson_service import (
                    extract_and_persist_lessons,
                )
                await extract_and_persist_lessons(
                    db,
                    discussion_id=discussion.id,
                    owner_user_id=discussion.owner_id,
                    market=discussion.market,
                    as_of_date=discussion.as_of_date,
                    lessons_payload=raw_obj,
                )
        except Exception as exc:
            log.warning("discussion.lessons.persist_failed",
                        extra={"id": str(discussion.id),
                               "error": str(exc)})
    return conclusion


def _extract_lessons_payload(raw: str) -> Any:
    """Re-parse the synthesizer's raw output to surface the optional
    `lessons` array. `_safe_conclusion` strips it out (only the
    five canonical fields are kept) so we re-run the lenient parse
    here. Returns the raw `lessons` value (`list` when the model
    obeyed; anything else is filtered downstream)."""
    try:
        cleaned = _strip_code_fence(strip_think_blocks(raw))
        data = _loads_lenient(cleaned)
    except (json.JSONDecodeError, ValueError):
        salvaged = _extract_json_object(cleaned) if cleaned else None
        if salvaged is None:
            return None
        try:
            data = _loads_lenient(salvaged)
        except (json.JSONDecodeError, ValueError):
            return None
    if not isinstance(data, dict):
        return None
    return data.get("lessons")
