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
# Year-like 4-digit numbers — keep generous; TW codes never overlap.
# Common 1-5 letter uppercase tokens that look like US tickers but
# aren't. Prevents `discussion_service` from sentimening "USD news".
# Not exhaustive — the topic field is short, false positives are
# cheap (worst case: an empty per-symbol news block), and adding new
# entries here is a 1-line patch.

_VALID_MARKETS = ("TW", "US", "GLOBAL")
_DEFAULT_MARKET = "TW"

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

# When the transcript gets long, only the most recent
# `_FULL_HISTORY_TURNS` turns are passed verbatim. Older turns are
# rendered as a single-line summary ("第1輪/buffett/supplement: 看好 2330,
# 目標價...") capped at `_HISTORY_SUMMARY_CHARS` chars so the prompt
# budget doesn't balloon at round 5 with 8 personas (40 turns × ~300
# Chinese chars × 3 BPE tokens ≈ 36K input tokens just for history).

# Reasoning models surface their chain-of-thought wrapped in <think>...</think>
# blocks. `_ThinkBlockFilter` (below) drops them as a streaming SSE
# filter so the thinking never flashes across the chat UI. The
# post-hoc parsers — `strip_think_blocks` + the JSON-tolerant
# `loads_lenient` / `extract_json_object` / `strip_code_fence` —
# moved out of this module along with the per-turn / conclusion
# parsers; they're imported directly from `services.llm_parsing_utils`
# by `discussion/turn_parsing.py` and `discussion/conclusion_parsing.py`.


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
    # Refresh only the server-default columns we need for the response.
    # A bare `db.refresh(row)` issues SELECT over the full model column
    # list — that breaks the create flow on any deployment whose DB
    # schema lags the model (e.g. an operator who hasn't yet run
    # `alembic upgrade head` past migration 0050 doesn't have
    # `post_mortem_diff`, and the SELECT errors with
    # `UndefinedColumnError` even though the INSERT itself only writes
    # the columns we set explicitly and succeeds).
    #
    # `created_at` / `updated_at` are the only attributes whose values
    # come from the server (`server_default=func.now()`); everything
    # else is set by the Python constructor or stays NULL on insert.
    # Restricting refresh to those two columns lets the row's response
    # serialize cleanly regardless of post-2026 migration state.
    await db.refresh(row, attribute_names=("created_at", "updated_at"))
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


# ── macro block (FRED-backed) ──────────────────────────────────────


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
    topic: str | None = None,
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
        topic=topic,
    )


# Screener utilities extracted to discussion/screener_utils.py.
# Re-export for back-compat with `discussion/context/blocks/{chip,http}.py`
# (which lazy-import here) + any test that reaches in by name.
from services.discussion.screener_utils import (  # noqa: E402,F401
    _compact_screener_row,
    _compact_us_screener_row,
    _is_speculative_etf,
    _tag_industry,
)


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


# Full annotation kept for `synthesize_conclusion` (which sees the
# unfiltered context snapshot, so the full block list is always
# accurate). Built once at import time.


# Matches the opening of a `"content": "` field. Used by the truncation
# salvage path — the persona's content always lives behind this key in
# our prompt template, so finding it gives us a reliable extraction
# anchor when the JSON wrapper got cut off mid-string.

# Standard JSON single-char escape map. `\u####` is handled inline
# because it consumes a variable number of input characters.


# Appended to the user prompt when the persona has tools available so
# the LLM is reminded that fabricating numbers is never necessary.
# Listed tool names mirror what `build_toolset` / `build_openai_compat_
# toolset` ship.
_PERSONA_TOOL_USAGE_HINT = (
    "\n\n## 工具可用\n"
    "你本回合可以呼叫下列工具取得即時 / 歷史數據：\n"
    "- `get_quote`（單檔即時報價）\n"
    "- `compare_quotes`（多檔並排報價，最多 10 檔；省 max_turns 預算）\n"
    "- `get_options_chain`（美股選擇權鏈，含 strike / IV / OI；TW 不支援）\n"
    "- `get_symbol_news`（指定標的最近新聞，可指定 limit ≤ 20）\n"
    "- `get_symbol_sentiment`（指定標的歷史情緒分數聚合，"
    "可調 max_age_hours ≤ 720）\n"
    "- `get_peers`（同業比較，TW 限定，回傳 5-10 檔同產業標的的 PE / PB / "
    "現金股息殖利率）\n"
    "- `get_financials`（三大財報；US 取最近 5 期年度，TW 取近 60 列 "
    "FinMind 季度資料，可推 ROE / 毛利率 / 槓桿 / FCF）\n"
    "- `get_institutional_history`（TW 限定，法人買賣超日序列，days ≤ 90）\n"
    "- `get_margin_history`（TW 限定，融資融券日序列，days ≤ 90）\n"
    "- `get_top_brokers`（TW 限定，主力分點 top buyers + top sellers，"
    "用於辨識特定券商分點的累積 / 派發）\n"
    "- `get_taifex_positioning`（TW 限定，期指三大法人未平倉快照 + 5 日變化，"
    "default contract=TX；外資未平倉領先大盤約 1-2 日）\n"
    "- `run_dcf` / `run_var` / `run_backtest`（分析運算）\n"
    "- `query_user_data`（使用者自身資料，限本人）\n"
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
    as_of_date: date | None = None,
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

    `as_of_date` (backtest mode): forwarded to the toolset builder so
    TW chip-flow tools anchor at the historical date instead of today.
    """
    tool_kwargs = _build_persona_tool_kwargs(
        provider=spec.default_provider,
        user_role=user_role,
        user_id=user_id,
        as_of_date=as_of_date,
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
        freshness_preamble=_format_freshness_preamble(context),
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
        #
        # Queue payload is `dict | None`: each progress milestone
        # gets wrapped in `{stage, done?, total?}` so the per-symbol
        # news fan-out (C1-3) can carry a counter, while the simpler
        # "phase started" events stay as `{stage}` only. `None` is
        # the sentinel marking gather completion.
        progress_q: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        async def _emit_progress(
            stage: str,
            *,
            done: int | None = None,
            total: int | None = None,
        ) -> None:
            payload: dict[str, Any] = {"stage": stage}
            if done is not None:
                payload["done"] = done
            if total is not None:
                payload["total"] = total
            await progress_q.put(payload)

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
                    topic=discussion.topic,
                )
            finally:
                # Sentinel — signals to the drainer that no more
                # progress events are coming so it can break out and
                # await the result. Always fires (success or failure)
                # so the caller never deadlocks.
                await progress_q.put(None)

        ctx_task = asyncio.create_task(_gather_then_signal())
        while True:
            payload = await progress_q.get()
            if payload is None:
                break
            # `payload` already carries `{stage, done?, total?}` —
            # forward verbatim so an older frontend that only reads
            # `stage` keeps working while newer ones can render
            # `done / total` when present.
            yield TurnEvent("ctx_progress", payload)
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

        # PR-4c: filter frozen personas out of the roster. When a
        # discussion is spawned by a sweep, look up the parent
        # strategy's `persona_status` and drop any persona that's
        # been frozen (auto-frozen by the underperformer detector
        # or manually via the admin endpoint). Live discussions
        # without a sweep parent stay unfiltered. Frozen personas
        # remain on `discussion.persona_ids` for audit trail — we
        # only remove them from THIS round's runtime roster.
        runtime_persona_ids = list(discussion.persona_ids)
        if discussion.sweep_id is not None:
            try:
                from models.backtest_sweep import BacktestSweep
                from models.discussion_strategy_template import (
                    DiscussionStrategyTemplate,
                )
                from services.persona_status_service import (
                    filter_roster_by_status,
                )
                sweep_row = await db.scalar(
                    select(BacktestSweep).where(
                        BacktestSweep.id == discussion.sweep_id,
                    )
                )
                if sweep_row is not None and sweep_row.strategy_id is not None:
                    tmpl = await db.scalar(
                        select(DiscussionStrategyTemplate).where(
                            DiscussionStrategyTemplate.id == sweep_row.strategy_id,
                        )
                    )
                    if tmpl is not None:
                        runtime_persona_ids = filter_roster_by_status(
                            persona_ids=runtime_persona_ids,
                            persona_status=tmpl.persona_status,
                        )
            except Exception as exc:
                log.warning(
                    "discussion.persona_status_filter_failed",
                    extra={
                        "discussion_id": str(discussion.id),
                        "error": str(exc),
                    },
                )

        # Batch-load persona overrides up front so the per-persona loop
        # doesn't make N round-trips to the persona_overrides table.
        specs_by_id = await _resolve_persona_specs(db, runtime_persona_ids)
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

        # PR-4c: iterate the FILTERED roster (frozen personas
        # already dropped above) so the round runner doesn't waste
        # a turn slot on someone who's been benched. Existing
        # `specs_by_id` keys match this filtered list 1:1.
        for idx, persona_id in enumerate(runtime_persona_ids):
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
            tool_call_total = 0
            tool_call_breakdown: dict[str, int] = {}
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
                        as_of_date=discussion.as_of_date,
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
                            # show "buffett 正在執行 run_dcf" inline, AND
                            # record the per-tool count for billing/debug
                            # observability — without this the round shows
                            # token cost but no signal for "why was this
                            # turn slow / which tool got called repeatedly".
                            tool_name = event.get("name") or "_unknown"
                            tool_call_total += 1
                            tool_call_breakdown[tool_name] = (
                                tool_call_breakdown.get(tool_name, 0) + 1
                            )
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
            # usage event (some openai-compat backends don't). Tool
            # counts are written even when zero so a row's NULL-vs-{}
            # distinction means "no breakdown captured" not "no calls".
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
                    tool_call_count=tool_call_total,
                    tool_call_breakdown=dict(tool_call_breakdown) or None,
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


# Conclusion synthesizer extracted to discussion/synthesizer.py.
# Re-export below for back-compat with `api/discussion/router.py`
# (calls `synthesize_conclusion`) + `test_discussion_synthesize_*`
# files that reach in via `discussion_service._compute_quality_signals`
# and similar private names.
from services.discussion.synthesizer import (  # noqa: E402,F401
    _apply_calibration_to_conclusion,
    _attach_baseline_delta,
    _compute_quality_signals,
    synthesize_conclusion,
)



# ── Lessons extraction (extracted to discussion/lessons.py) ────────
# Re-export for back-compat with `backtest_sweep_service` + the seven
# `tests/test_discussion_*` files that reach into this module by name.
from services.discussion.lessons import (  # noqa: E402,F401
    _extract_lessons_payload,
    extract_winning_thesis_lessons,
)


# Re-export the JSON-parse helpers from llm_parsing_utils that this
# module's parsers used to expose. Several tests reach in via
# `discussion_service.strip_think_blocks` / `_extract_json_object`
# (the original names this module surfaced); the symbols have moved
# to `services.llm_parsing_utils` but the test surface stays stable.
from services.llm_parsing_utils import (  # noqa: E402,F401
    extract_json_object as _extract_json_object,
    strip_think_blocks,
)


# Conclusion parsing extracted to discussion/conclusion_parsing.py.
# Re-export for back-compat with synthesize_conclusion (still in this
# file) + tests/test_discussion_conclusion_diff.py.
from services.discussion.conclusion_parsing import (  # noqa: E402,F401
    _parse_recommendations,
    _safe_conclusion,
    _try_repair_truncated_json,
    compute_conclusion_diff,
)


# Per-turn JSON parsers extracted to discussion/turn_parsing.py.
# Re-export for back-compat with `_ask_persona` (still in this file)
# + tests/test_discussion_service.py.
from services.discussion.turn_parsing import (  # noqa: E402,F401
    DEFAULT_STANCE,
    VALID_STANCES,
    _decode_partial_json_string,
    _parse_turn_response,
    _salvage_truncated_json,
)


# Transcript / history formatters extracted to discussion/transcript_format.py.
# Re-export for back-compat with `_ask_persona` + `synthesize_conclusion`
# (still in this file).
from services.discussion.transcript_format import (  # noqa: E402,F401
    _FULL_HISTORY_TURNS,
    _HISTORY_SUMMARY_CHARS,
    _MAX_HISTORY_TURNS,
    _format_history,
    _format_transcript,
    _summarize_turn_content,
)


# Symbol extraction extracted to discussion/symbols.py.
# Re-export for back-compat with `gather_market_context` +
# `synthesize_conclusion` (still in this file).
from services.discussion.symbols import (  # noqa: E402,F401
    _BARE_US_TICKER_RE,
    _CASHTAG_RE,
    _MAX_FOCUS_SYMBOLS,
    _TW_SYMBOL_RE,
    _US_TICKER_STOPWORDS,
    _YEAR_MAX,
    _YEAR_MIN,
    _crypto_universe,
    _is_year_like,
    extract_focus_symbols,
)


# Pure technicals + summaries extracted to discussion/technicals.py.
# Re-export for back-compat with focus brief builders (still in this
# file) + the two window constants the builders also reference.
from services.discussion.technicals import (  # noqa: E402,F401
    _FOCUS_BRIEF_CHIP_DAYS,
    _FOCUS_BRIEF_REVENUE_MONTHS,
    _bar_close,
    _compute_technicals,
    _ma,
    _pct_change,
    _rsi,
    _summarize_institutional,
    _summarize_margin,
    _summarize_revenue,
)


# Prompt templates + schema annotation extracted to discussion/prompts.py.
# Re-export for back-compat with `_ask_persona` + `synthesize_conclusion`
# (still in this file).
from services.discussion.prompts import (  # noqa: E402,F401
    _BLOCK_ANNOTATIONS,
    _CONTEXT_SCHEMA_ANNOTATION,
    _SCHEMA_HEADER,
    _SYNTHESIZER_SYSTEM,
    _SYNTHESIZER_USER_TEMPLATE,
    _TURN_PROMPT_TEMPLATE,
    _format_freshness_preamble,
    _persona_schema_annotation,
)


# Macro context block extracted to discussion/macro.py.
# Re-export for back-compat with `gather_market_context` (still in this
# file) + the lazy import in discussion/context/blocks/http.py.
from services.discussion.macro import (  # noqa: E402,F401
    _MACRO_SERIES,
    _assemble_macro_block,
    _macro_summary_from_series,
)


# Persona config extracted to discussion/persona_config.py.
# Re-export for back-compat with `run_round` + `_ask_persona`
# (still in this file) + tests/persona_status_service callers.
from services.discussion.persona_config import (  # noqa: E402,F401
    _ALL_PERSONA_BLOCKS,
    _ALWAYS_INCLUDED_BLOCKS,
    _CONTRARIAN_PROFILE,
    _MACRO_PROFILE,
    _OPENAI_COMPAT_TOOL_PROVIDERS,
    _PERSONA_CONTEXT_PROFILES,
    _PORTFOLIO_PROFILE,
    _QUANT_PROFILE,
    _SHORT_TERM_PROFILE,
    _VALUE_PROFILE,
    _build_persona_tool_kwargs,
    _filter_context_for_persona,
    _resolve_persona_specs,
)


# Focus brief builders extracted to discussion/focus_briefs.py.
# Re-export for back-compat with `gather_market_context` (still in this
# file) + the lazy import in discussion/context/blocks/http.py + tests
# that patch `services.discussion_service._assemble_focus_briefs`.
from services.discussion.focus_briefs import (  # noqa: E402,F401
    _FOCUS_BRIEF_HISTORY_MONTHS,
    _FOCUS_BRIEF_PEER_COUNT,
    _assemble_focus_briefs,
    _build_tw_focus_brief,
    _build_tw_focus_brief_backtest,
    _build_us_focus_brief,
    _get_tw_peers,
)
