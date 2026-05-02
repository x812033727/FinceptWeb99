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
# 2048 gives Chinese-output personas (BPE → ~3 tokens/char) ~600-700 chars of
# analysis after a reasoning preamble. With 1024 we still saw truncation mid-
# sentence on long-winded personas (Lynch / Buffett); the salvage path in
# `_parse_turn_response` recovers the partial content but raising the cap
# means it kicks in less often.
_MAX_TURN_TOKENS = 2048
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
) -> Discussion:
    topic = _validate_text(topic, field="topic", max_chars=_MAX_TOPIC_CHARS)
    rules = _validate_text(rules, field="rules", max_chars=_MAX_RULES_CHARS)
    pids = _normalize_persona_ids(persona_ids)
    market = _normalize_market(market)

    row = Discussion(
        owner_id=owner_id,
        topic=topic,
        rules=rules,
        persona_ids=pids,
        market=market,
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
    db: AsyncSession, symbol: str,
) -> dict[str, Any]:
    """Per-TW-symbol mini analyst report. Each sub-call is wrapped in
    its own try so a single connector outage doesn't blank the whole
    brief — the persona just sees "fundamentals: null" and reasons
    with what remained."""
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
    db: AsyncSession, *, market: str, symbols: list[str],
) -> list[dict[str, Any]]:
    """Fan out per-symbol brief assembly concurrently. Cap at
    `_MAX_FOCUS_SYMBOLS` for token-budget protection."""
    if not symbols:
        return []
    syms = symbols[:_MAX_FOCUS_SYMBOLS]
    if market == "TW":
        coros = [_build_tw_focus_brief(db, s) for s in syms]
    elif market == "US":
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
                coros.append(_build_tw_focus_brief(db, s))
            elif s in _crypto_universe():
                continue
            else:
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


async def _assemble_macro_block() -> dict[str, Any]:
    """Pull a small set of FRED macro series concurrently and reduce
    each to its latest value plus 1y / 3m delta. Empty / failing
    series degrade to None so the personas can mention "macro data
    incomplete" instead of confidently citing a missing rate."""
    from services import us_market_service

    async def _pull(name: str) -> tuple[str, list[dict[str, Any]]]:
        try:
            return name, await us_market_service.get_macro_indicator(name)
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


async def gather_market_context(
    db: AsyncSession,
    *,
    market: str = "TW",
    top_n: int = _DEFAULT_TOP_MOVERS,
    focus_symbols: list[str] | None = None,
    owner_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Build a structured snapshot of the market state for the personas.

    Each block degrades gracefully — if any data source is unavailable we
    return an empty list / None for that block instead of raising, so a
    transient outage doesn't block the discussion entirely.

    `focus_symbols` (typically extracted from the discussion topic via
    `extract_focus_symbols`) makes the context include per-symbol news
    sentiment alongside the market-wide aggregate. Empty list / None
    skips the per-symbol block.

    `owner_id` (the discussion's owner) opts the context into a
    `user_context` block carrying the owner's portfolio + watchlist
    summary plus overlap with `focus_symbols`. Personas that don't
    care about portfolio fit (macro_analyst, market_analyst, the
    legendary investors when not asked about position sizing) can
    ignore it; portfolio_advisor / risk_manager use it to give
    portfolio-aware advice instead of generic "should I buy 2330"
    answers.
    """
    ctx: dict[str, Any] = {
        "market": market,
        "captured_at": datetime.now(UTC).isoformat(),
        "top_gainers": [],
        "top_losers": [],
        "index": None,
        "news_sentiment": None,
        "per_symbol_news_sentiment": {},
        # Per-focus-symbol mini analyst report (PR #206). Populated when
        # the topic names specific symbols and `_assemble_focus_briefs`
        # could pull at least the quote — gives personas actual evidence
        # to cite ("2330 已脫離 60 日均，距 52w 高點 -3.5%, RSI 62") instead
        # of guessing from headlines.
        "focus_briefs": [],
        # Cross-market macro snapshot (PR #206) — Fed funds / 10Y / yield
        # spread / DXY / TWD/USD with 1y + 3m deltas. Populated for every
        # discussion regardless of `market` because rates / FX matter
        # everywhere; empty `summary` blocks are silently passed through
        # so a missing FRED key doesn't break the round.
        "macro": None,
        # Discussion owner's portfolio + watchlist summary. Populated
        # only when `owner_id` is supplied (tests passing None get a
        # null block); always None for non-owner reads via
        # round_context snapshot replay (the snapshot itself stays
        # owner-scoped via discussion FK).
        "user_context": None,
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

        # Per-symbol mini analyst report. Best-effort — the assembler
        # already returns partial briefs on connector failure, so a
        # bad day for tw_market_service still yields a useful block.
        try:
            ctx["focus_briefs"] = await _assemble_focus_briefs(
                db, market=market, symbols=list(focus_symbols),
            )
        except Exception as exc:
            _record_error("focus_briefs", exc)

    # Macro snapshot is universal — Fed / 10Y / DXY / TWD/USD matter
    # to TW personas as much as US ones. Errors degrade to an empty
    # block so `_record_error` can still log the connector outage.
    try:
        ctx["macro"] = await _assemble_macro_block()
    except Exception as exc:
        _record_error("macro", exc)

    if owner_id is not None:
        try:
            ctx["user_context"] = await _assemble_user_context(
                db, owner_id=owner_id, focus_symbols=focus_symbols,
            )
        except Exception as exc:
            _record_error("user_context", exc)

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
    "- focus_briefs：**主題提及之個股小型分析師簡報**——`quote` 即時報價、"
    "`technicals`（MA20/60/120、52w 高低與距離、5/20/60 日漲跌幅、RSI14）、"
    "`fundamentals`（PE/PB/殖利率/EPS）、`revenue_trend`（近 6 月營收年/月增）、"
    "`chip_5d`（外資 / 投信 / 自營近 5 日淨買賣）、`margin_latest`（最新融資餘額）、"
    "`peers`（同產業 3 檔可比標的）。**有此區塊就要引用具體數據**，"
    "不要只憑 headlines 推論。\n"
    "- macro：宏觀利率與匯率快照（Fed Funds / US 10Y / 10Y-2Y 殖利率價差 / "
    "DXY / TWD/USD），各帶 `latest_value` + `change_1y` + `change_3m`。"
    "影響全球風險偏好與外資流向，建議在結論中至少提及一次相關方向。\n"
    "- user_context：**討論發起人本人的部位**——`portfolios`（組合清單）、"
    "`holdings`（前 20 大持股，含股數 / 平均成本 / 計價幣別）、"
    "`watchlist_symbols`（自選股，前 30）、`focus_overlap.held` "
    "（主題提及的標的中已持有者）、`focus_overlap.watching`（自選股中相關者）。"
    "**只在你的角色與部位配置 / 風險管理相關時引用**（portfolio_advisor / "
    "risk_manager / 在被問加碼減碼時的 buffett / lynch 等）；其他情境忽略。"
    "**禁止在結論中揭露具體股數或成本價**——僅用於決策邏輯。\n"
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

    sections: list[str] = []
    if older:
        sections.append("（較早輪次摘要）")
        for t in older:
            summary = _summarize_turn_content(t.content)
            sections.append(
                f"- 第{t.round}輪 · {t.persona_id} · {t.stance}：{summary}"
            )
    if older and recent:
        sections.append("")
        sections.append("（最近發言全文）")
    for t in recent:
        body = t.content.strip() or "（同意，無補充）"
        sections.append(f"- 第{t.round}輪 · {t.persona_id} · {t.stance}：{body}")
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
    """
    tool_kwargs = _build_persona_tool_kwargs(
        provider=spec.default_provider,
        user_role=user_role,
        user_id=user_id,
    )
    user_prompt = _TURN_PROMPT_TEMPLATE.format(
        topic=topic,
        rules=rules,
        context=json.dumps(context, ensure_ascii=False, indent=2),
        history=_format_history(prior_turns),
    )
    if tool_kwargs:
        user_prompt += _PERSONA_TOOL_USAGE_HINT
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
        context = await gather_market_context(
            db,
            market=discussion.market,
            focus_symbols=focus,
            owner_id=discussion.owner_id,
        )
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
        )

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
