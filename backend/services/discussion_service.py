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

import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ai.agents import all_persona_ids

# `stream_chat` is kept as a top-level re-export so the lazy imports
# inside `discussion/round_runner.py` + `discussion/synthesizer.py`
# (and the ~38 test sites that `patch("services.discussion_service.
# stream_chat", ...)` to mock the LLM) keep landing on this binding
# even after the round_runner / synthesizer extractions. No direct
# call site remains here.
from ai.llm_router import stream_chat  # noqa: F401

# `settings` is kept as a top-level re-export so the
# `test_build_persona_tool_kwargs_claude_agent_off_when_disabled`
# style tests that monkeypatch.setattr(discussion_service, "settings",
# _SettingsStub(...)) keep working — monkeypatch refuses to set an
# attribute that doesn't already exist on the target module. No
# direct read remains here either after the C3-1 γ extraction
# (round_runner reads its own `from config import settings`).
from config import settings  # noqa: F401
from models.discussion import Discussion, DiscussionTurn
from models.discussion_round_context import DiscussionRoundContext

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

# `_ThinkBlockFilter` (the stateful streaming filter that drops
# reasoning-model `<think>...</think>` blocks from the SSE feed) and
# `TurnEvent` (the dataclass wrapper for each yielded event) moved
# to `discussion/round_runner.py` along with `run_round` / `_ask_persona`
# in the C3-1 γ extraction. Re-exported below for back-compat with
# tests that reach in via `discussion_service._ThinkBlockFilter`.
# Post-hoc text parsers — `strip_think_blocks` + the JSON-tolerant
# `loads_lenient` / `extract_json_object` / `strip_code_fence` —
# moved out of this module along with the per-turn / conclusion
# parsers; they're imported directly from `services.llm_parsing_utils`
# by `discussion/turn_parsing.py` and `discussion/conclusion_parsing.py`.

_DEFAULT_TOP_MOVERS = 8


# Status machine: draft → running → done. The "running" state lets the UI
# show a busy indicator and lets future code reject parallel rounds on the
# same discussion if we ever want to.
STATUS_DRAFT = "draft"
STATUS_RUNNING = "running"
STATUS_DONE = "done"


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
            # B4: every user-authored turn is by definition injected —
            # the flag lets the UI badge it without sniffing stance.
            injected_by_user=True,
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


# ── mid-round user interjections (B4) ──────────────────────────────
#
# While a round is streaming, the owner can interject a question via
# `POST /sessions/{id}/interject`. The router validates + enqueues it
# here; `run_round` drains the queue at every turn boundary, persists
# the question as a `user_input` turn and has the assigned persona
# answer it as an extra turn (both marked `injected_by_user=True`).
#
# In-memory + per-process by design — the round loop runs as an
# asyncio task in the SAME process that accepted the interject request
# (identical single-process assumption as `_BG_ROUND_TASKS` in
# `api/discussion/_helpers.py`, which keeps the SSE stream and its
# background round task co-located). Entries that arrive in the tiny
# window after the loop's final drain simply stay queued and get
# answered at the start of the next round.

_PENDING_INTERJECTIONS: dict[uuid.UUID, list[dict[str, Any]]] = {}
_MAX_PENDING_INTERJECTIONS = 3


def queue_interjection(
    discussion_id: uuid.UUID,
    *,
    question: str,
    target_persona: str | None = None,
) -> dict[str, Any]:
    """Enqueue a mid-round user interjection for `run_round` to pick
    up at the next turn boundary. Raises ValueError on empty/oversized
    questions or when the discussion already has
    `_MAX_PENDING_INTERJECTIONS` unanswered interjections (protects
    the round from being flooded into an unbounded Q&A session)."""
    text = _validate_text(
        question, field="question", max_chars=_MAX_USER_INJECTION_CHARS,
    )
    pending = _PENDING_INTERJECTIONS.setdefault(discussion_id, [])
    if len(pending) >= _MAX_PENDING_INTERJECTIONS:
        raise ValueError(
            f"Too many pending interjections "
            f"(max {_MAX_PENDING_INTERJECTIONS}) — wait for the current "
            "ones to be answered",
        )
    entry = {"question": text, "target_persona": target_persona}
    pending.append(entry)
    return entry


def drain_interjections(discussion_id: uuid.UUID) -> list[dict[str, Any]]:
    """Pop-and-return every pending interjection for the discussion
    (FIFO order). Called by `run_round` at each turn boundary."""
    pending = _PENDING_INTERJECTIONS.pop(discussion_id, None)
    return pending or []


def pending_interjection_count(discussion_id: uuid.UUID) -> int:
    return len(_PENDING_INTERJECTIONS.get(discussion_id) or [])


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


# user_context + prior_discussions extracted to discussion/context_assembly.py.
# Re-export below for back-compat with discussion/context/blocks/owner.py
# (lazy-imports both via this module) + test_discussion_service.py (~10
# tests that call `discussion_service._assemble_user_context` /
# `_assemble_prior_discussions` by name).
from services.discussion.context_assembly import (  # noqa: E402,F401
    _PRIOR_DISCUSSIONS_CAP,
    _PRIOR_DISCUSSIONS_LOOKBACK_DAYS,
    _USER_CONTEXT_HOLDING_CAP,
    _USER_CONTEXT_WATCHLIST_CAP,
    _assemble_prior_discussions,
    _assemble_user_context,
)


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
    strategy: str | None = None,
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

    `strategy` (the discussion's `auto_run_strategy`, when auto-run)
    gates strategy-specific blocks — currently only
    `large_trader_positioning`, fetched when `strategy ==
    "price_signal"`. `None` (the default, matching every caller that
    predates this param) skips it, same as any other strategy.
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
        strategy=strategy,
    )


# Screener utilities extracted to discussion/screener_utils.py.
# Re-export for back-compat with `discussion/context/blocks/{chip,http}.py`
# (which lazy-import here) + any test that reaches in by name.
# Conclusion parsing extracted to discussion/conclusion_parsing.py.
# Re-export for back-compat with synthesize_conclusion (still in this
# file) + tests/test_discussion_conclusion_diff.py.
from services.discussion.conclusion_parsing import (  # noqa: E402,F401
    _parse_recommendations,
    _safe_conclusion,
    _try_repair_truncated_json,
    compute_conclusion_diff,
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

# ── Lessons extraction (extracted to discussion/lessons.py) ────────
# Re-export for back-compat with `backtest_sweep_service` + the seven
# `tests/test_discussion_*` files that reach into this module by name.
from services.discussion.lessons import (  # noqa: E402,F401
    _extract_lessons_payload,
    extract_winning_thesis_lessons,
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
# Round runner extracted to discussion/round_runner.py.
# Re-export below for back-compat with `api/discussion/router.py`
# (calls `run_round`) + the ~17 test_discussion_service.py sites that
# reach in via `discussion_service.run_round` / `_ThinkBlockFilter` /
# `TurnEvent` / `_ask_persona` / `_PERSONA_TOOL_USAGE_HINT`.
from services.discussion.round_runner import (  # noqa: E402,F401
    _PERSONA_TOOL_USAGE_HINT,
    TurnEvent,
    _ask_persona,
    _ThinkBlockFilter,
    interject_followup,
    run_round,
)
from services.discussion.screener_utils import (  # noqa: E402,F401
    _compact_screener_row,
    _compact_us_screener_row,
    _is_speculative_etf,
    _tag_industry,
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

# Transcript / history formatters extracted to discussion/transcript_format.py.
# Re-export for back-compat with `_ask_persona` + `synthesize_conclusion`
# (still in this file).
from services.discussion.transcript_format import (  # noqa: E402,F401
    _FULL_HISTORY_TURNS,
    _FULL_TURN_MAX_CHARS,
    _HISTORY_SUMMARY_CHARS,
    _MAX_HISTORY_TURNS,
    _format_history,
    _format_transcript,
    _summarize_turn_content,
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

# Re-export the JSON-parse helpers from llm_parsing_utils that this
# module's parsers used to expose. Several tests reach in via
# `discussion_service.strip_think_blocks` / `_extract_json_object`
# (the original names this module surfaced); the symbols have moved
# to `services.llm_parsing_utils` but the test surface stays stable.
from services.llm_parsing_utils import (  # noqa: E402,F401
    extract_json_object as _extract_json_object,
)
from services.llm_parsing_utils import strip_think_blocks  # noqa: E402,F401
