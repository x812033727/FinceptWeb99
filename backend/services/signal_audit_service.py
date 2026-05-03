"""Audit which discussion-context signals personas actually cited.

Adding signals to ``gather_market_context`` is cheap; the question
that matters is whether the LLM consumes them. This module joins
``discussion_round_contexts`` with ``discussion_turns`` and checks,
per (round, persona, signal), whether the persona's content text
mentioned the signal at all.

Detection is keyword/regex-based — coarse but cheap. We accept some
false positives (a persona casually mentioning "RSI" without quoting
the actual value still counts as "cited") because the goal is to
spot signals with **zero** uptake, which keyword OR is plenty for.
False positives in the other direction (cited but missed by the
matcher) are bigger risk; the keyword set here errs on the side of
inclusive Chinese + English variants.

Two consumers:
  - ``scripts/audit_signal_usage.py`` — admin CLI, prints a
    readable per-round / per-persona / coverage report.
  - Future: an admin-API endpoint surfacing the same numbers in
    the UI so we can tune ``_PERSONA_CONTEXT_PROFILES`` based on
    real usage stats instead of hunches.
"""
from __future__ import annotations

import logging
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.discussion import Discussion, DiscussionTurn
from models.discussion_round_context import DiscussionRoundContext

log = logging.getLogger(__name__)


# Keyword/regex patterns per signal. Match in either Chinese or
# English so personas writing in either language still register.
# NOTE: order matters — `_match_keywords` iterates and short-circuits
# on first hit, so put the most distinctive token first.
_SIGNAL_KEYWORDS: dict[str, list[str]] = {
    # Per-symbol short-term metrics
    "short_term_signals.volume_ratio": [
        r"量能", r"成交量", r"放量", r"爆量", r"volume[_\s]*ratio",
    ],
    "short_term_signals.return_5d": [
        r"5\s*[日天]", r"近五日", r"五日", r"return[_\s]*5",
    ],
    "short_term_signals.return_20d": [
        r"20\s*[日天]", r"近廿日", r"return[_\s]*20", r"月線",
    ],
    "short_term_signals.rsi_14": [
        r"RSI", r"超買", r"超賣",
    ],
    "short_term_signals.kd_k": [
        r"\bKD\b", r"\bK\s*值", r"\bD\s*值", r"隨機指標",
    ],
    "short_term_signals.gap_pct": [
        r"跳空", r"開盤跳", r"\bgap\b",
    ],
    "short_term_signals.industry_rs": [
        r"類股", r"產業", r"領先(?:類股|產業)?", r"落後(?:類股|產業)?",
        r"sector", r"\bRS\b", r"相對強度",
    ],
    "short_term_signals.day_trading_trend": [
        r"當沖", r"當日沖銷", r"day[_\s]*trad", r"散戶亢奮",
    ],
    "short_term_signals.securities_lending_trend": [
        r"借券", r"securities[_\s]*lending", r"lending[_\s]*balance",
    ],
    "short_term_signals.upcoming_event": [
        r"法說", r"法人說明會", r"除息", r"除權",
        r"earnings(?:\s*call|\s*date)?", r"ex.div(?:idend)?",
    ],
    # Market-wide
    "taifex_positioning": [
        r"台指期", r"未平倉", r"\bOI\b", r"net[_\s]*oi", r"taifex",
    ],
    "single_stock_futures_oi": [
        r"個股期(?:貨)?", r"single[_\s]*stock[_\s]*future",
        r"外資.*個股期", r"期貨.*多單", r"期貨.*空單",
        # Common contract codes — personas often reference them by name
        # ("CDF 連 5 日 +1500 口" = 台積電期).
        r"\b(?:CDF|CFF|CCF|IIF|GLF|GVF|HCF|NEF|NHF|NJF)\b",
    ],
    "taiwan_vix": [
        r"台.*VIX", r"VIX[_\s]*TW", r"VXTW",
        r"臺指選擇權.*波動率", r"台.*波動率指數",
        # Generic "波動率" alone is too broad (US ^VIX uses it too in
        # overseas_indicators); require TW-specific qualifier.
    ],
    "news_sentiment": [
        r"新聞情緒", r"情緒(?:面|偏)", r"sentiment", r"news[_\s]*sentiment",
    ],
    "international_sentiment": [
        r"Fed", r"FOMC", r"國際", r"全球", r"international",
    ],
    "overseas_indicators": [
        r"\bSOX\b", r"費半", r"費城半導體",
        r"NASDAQ", r"那斯達克", r"\bNDX\b",
        r"S&?P\s*500", r"\bSPX\b",
        r"道瓊", r"\bDJIA\b", r"\bDJI\b",
        r"\bVIX\b", r"波動率指數",
        r"美股(?:收盤|開盤|期貨)?",
        r"overseas[_\s]*indicators?",
    ],
    "top_foreign_buyers": [
        r"外資(?:買超|連買|買進)", r"foreign[_\s]*buy", r"投信買超",
    ],
    "margin_balance_trend": [
        r"融資(?:餘額)?", r"融券(?:餘額)?", r"margin[_\s]*balance",
    ],
    "top_revenue_growers": [
        r"營收年增", r"YoY", r"月營收", r"revenue[_\s]*growth",
    ],
    "active_buybacks": [
        r"庫藏股", r"buyback",
    ],
    "govt_bank_flow_5d": [
        r"八大行庫", r"國家隊", r"govt[_\s]*bank",
    ],
    "risk_warnings": [
        r"處置股", r"暫停交易", r"disposition", r"suspended",
    ],
    "market_institutional_5d": [
        r"全市場(?:三大|法人)", r"market[_\s]*institutional",
    ],
    "macro": [
        r"Fed.*rate", r"yield.*curve", r"殖利率", r"\bDXY\b", r"美元指數",
    ],
    "focus_briefs": [
        # Citing a focus brief means quoting hard numbers from it —
        # PE / valuation band / 52w high. Less tractable to keyword
        # match exactly; fall back to "PE" / "本益比" / "52週" tokens.
        r"\bPE\b", r"本益比", r"52\s*週", r"52w",
        r"valuation[_\s]*band",
    ],
    "user_context": [
        r"持股(?:中|為)", r"用戶持有", r"current[_\s]*holding",
    ],
    "prior_discussions": [
        r"先前討論", r"前次討論", r"prior[_\s]*discussion",
    ],
}


@dataclass
class TurnAudit:
    """Per-persona-turn citation map.

    `cited[signal]`             — keyword match ("RSI", "外資", ...).
                                  Only populated for signals that
                                  WERE in the round's prompt context.
    `cited_with_value[signal]`  — keyword AND a numeric token
                                  within ±60 chars (PR #258).
                                  Distinguishes "RSI 67.3 已逼近超買"
                                  (concrete value) from "RSI 偏高"
                                  (generic mention).
    `hallucinated[signal]`      — PR #259: turn cited a signal that
                                  was NOT in the round's prompt
                                  context AND quoted a numeric value
                                  next to the keyword. The strict
                                  definition (requires a number, not
                                  just a keyword) keeps general
                                  knowledge mentions out of the
                                  hallucination count — the persona
                                  may legitimately know "RSI is a
                                  technical indicator" without our
                                  prompt feeding it; the problem is
                                  inventing "RSI 67.3" when our
                                  prompt didn't.

    The flags are mutually exclusive on `signals_present`: a signal
    either appears in `cited` (when present) or in `hallucinated`
    (when absent), never both. `cited_with_value` is the strict
    subset of `cited`.
    """
    round: int
    persona_id: str
    stance: str
    cited: dict[str, bool] = field(default_factory=dict)
    cited_with_value: dict[str, bool] = field(default_factory=dict)
    hallucinated: dict[str, bool] = field(default_factory=dict)

    @property
    def cited_count(self) -> int:
        return sum(1 for v in self.cited.values() if v)

    @property
    def cited_with_value_count(self) -> int:
        return sum(1 for v in self.cited_with_value.values() if v)

    @property
    def hallucinated_count(self) -> int:
        return sum(1 for v in self.hallucinated.values() if v)


@dataclass
class RoundAudit:
    round: int
    signals_present: set[str] = field(default_factory=set)
    turns: list[TurnAudit] = field(default_factory=list)


@dataclass
class DiscussionAudit:
    discussion_id: str
    topic: str
    rounds: list[RoundAudit] = field(default_factory=list)

    def coverage(self) -> dict[str, dict[str, int]]:
        """Aggregate `signal_key → {present, cited, cited_with_value,
        persona_count}` across every round + every persona.

        - `cited` counts keyword-only matches (existing behaviour).
        - `cited_with_value` counts the strict subset where the
          turn's content also contained a numeric token near the
          keyword (PR #258). Always ≤ `cited`.
        - `persona_count` is the denominator for both rates.

        Two rates are useful: a high `cited` with low
        `cited_with_value` flags a signal personas mention but
        don't actually leverage the data — typically a prompt-tuning
        opportunity (push for concrete numbers).
        """
        agg: dict[str, dict[str, int]] = defaultdict(
            lambda: {
                "present": 0, "cited": 0,
                "cited_with_value": 0, "persona_count": 0,
            }
        )
        for r in self.rounds:
            for sig in r.signals_present:
                agg[sig]["present"] += 1
                agg[sig]["persona_count"] += len(r.turns)
                for t in r.turns:
                    if t.cited.get(sig):
                        agg[sig]["cited"] += 1
                    if t.cited_with_value.get(sig):
                        agg[sig]["cited_with_value"] += 1
        return dict(agg)

    def hallucinations(self) -> dict[str, dict[str, int]]:
        """PR #259: per-signal hallucination stats.

        `signal_key → {absent_rounds, hallucinated, persona_count_absent}`

        - `absent_rounds`         number of rounds where the signal
                                  was NOT in the prompt context
        - `hallucinated`          turns that cited the signal WITH a
                                  numeric value despite it being
                                  absent (i.e., the persona produced
                                  data we didn't supply)
        - `persona_count_absent`  denominator: total persona turns
                                  across rounds where the signal was
                                  absent

        Hallucination rate = `hallucinated / persona_count_absent`.

        Only signals with `absent_rounds > 0` are returned — a signal
        present in every round can't be hallucinated relative to this
        discussion's data.

        False-positive note: a value that legitimately came from
        another signal block (e.g., a YoY number quoted from
        `focus_briefs` triggering the `top_revenue_growers` regex)
        still flags here. The metric is an upper bound; investigate
        the underlying turns before treating high rates as proof of
        fabrication.
        """
        agg: dict[str, dict[str, int]] = defaultdict(
            lambda: {
                "absent_rounds": 0, "hallucinated": 0,
                "persona_count_absent": 0,
            }
        )
        all_signals = set(_SIGNAL_KEYWORDS.keys())
        for r in self.rounds:
            absent = all_signals - r.signals_present
            for sig in absent:
                agg[sig]["absent_rounds"] += 1
                agg[sig]["persona_count_absent"] += len(r.turns)
                for t in r.turns:
                    if t.hallucinated.get(sig):
                        agg[sig]["hallucinated"] += 1
        return dict(agg)


# ── Signal-presence detection ─────────────────────────────────────


def _detect_present_signals(context: dict[str, Any]) -> set[str]:
    """Walk the context dict and return the set of signal keys that
    have non-empty / non-None content. Mirrors the keys in
    `_SIGNAL_KEYWORDS` so missing intersection = signal isn't even
    in the prompt (different problem from "in prompt but ignored")."""
    present: set[str] = set()

    def _is_meaningful(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, (list, dict, str)):
            return len(value) > 0
        return True

    # Per-symbol short_term_signals — check each sub-field across all symbols.
    sts = context.get("short_term_signals") or {}
    for _, signals in sts.items():
        if not isinstance(signals, dict):
            continue
        for sub_key in (
            "volume_ratio", "return_5d", "return_20d", "rsi_14",
            "kd_k", "gap_pct", "industry_rs",
            "day_trading_trend", "securities_lending_trend",
            "upcoming_event",
        ):
            full_key = f"short_term_signals.{sub_key}"
            if _is_meaningful(signals.get(sub_key)):
                present.add(full_key)

    # Top-level blocks — single-key presence check.
    for top_key in (
        "taifex_positioning", "news_sentiment", "international_sentiment",
        "top_foreign_buyers", "margin_balance_trend", "top_revenue_growers",
        "active_buybacks", "govt_bank_flow_5d", "risk_warnings",
        "market_institutional_5d", "macro", "focus_briefs",
        "single_stock_futures_oi",   # PR #282
        "taiwan_vix",                # PR #283
        "user_context", "prior_discussions",
    ):
        if _is_meaningful(context.get(top_key)):
            present.add(top_key)

    # Overseas indicators (PR #269): returned as `{as_of, indices}`,
    # so the dict is non-empty even when the connector failed
    # (just `indices=[]`). Check the actual payload.
    overseas = context.get("overseas_indicators")
    if isinstance(overseas, dict) and overseas.get("indices"):
        present.add("overseas_indicators")
    return present


# ── Citation detection ────────────────────────────────────────────


# A "concrete value" is a number with at least 1 digit, optionally
# decimal, optionally signed, optionally with a unit suffix. The
# unit suffixes (% / pp / 倍 / x / 口 / 張) are common in zh-TW
# financial writing — including them tightens the false-positive
# rate (a bare year like "2026" near "RSI" wouldn't usually carry
# a unit, while a real RSI citation does: "RSI 67.3" → digit;
# "外資 +3500 口" → digit + 口). Plain digit fallback handles
# "RSI 67" (no decimal, no unit).
_NUMERIC_TOKEN = re.compile(
    r"[+\-]?\d+(?:[\.,]\d+)?\s*(?:%|pp|倍|x|口|張|億|萬)?",
    re.IGNORECASE,
)
# How wide a window around each keyword match to scan for a numeric
# token. 60 chars covers a typical 1-2 sentence citation in zh-TW
# (which packs ~30 chars per "sentence") without bleeding into
# unrelated paragraphs.
_VALUE_PROXIMITY_WINDOW = 60


def _match_keywords(content: str, patterns: list[str]) -> bool:
    """Any-of regex match. Case-insensitive (Chinese tokens unchanged)."""
    for p in patterns:
        if re.search(p, content, re.IGNORECASE):
            return True
    return False


def _has_value_near_keyword(content: str, patterns: list[str]) -> bool:
    """True when ANY of `patterns` matches AND a numeric token sits
    within ±`_VALUE_PROXIMITY_WINDOW` chars of that match.

    "RSI 67.3 已逼近超買" → True (digit next to "RSI")
    "RSI 偏高" → False (no digit near "RSI")
    "RSI 在 30/70 之間" → True (digit window catches "30")
                         — accepted false positive: this style
                         IS actually citing the bands, just not
                         the actual reading.

    The False-positive rate of plain numeric matching is acceptable
    for the coverage-stat use case — false positives bias the rate
    UPWARD (more lenient), and the goal of the metric is to spot
    signals NEVER cited with values, where the floor matters.
    """
    for p in patterns:
        for m in re.finditer(p, content, re.IGNORECASE):
            start = max(0, m.start() - _VALUE_PROXIMITY_WINDOW)
            end = min(len(content), m.end() + _VALUE_PROXIMITY_WINDOW)
            if _NUMERIC_TOKEN.search(content[start:end]):
                return True
    return False


def audit_turn(
    *, round_no: int, persona_id: str, stance: str, content: str,
    signals_present: set[str],
) -> TurnAudit:
    """Return a TurnAudit recording per-signal citation flags.

    For each signal in `_SIGNAL_KEYWORDS`:
      - if the signal IS in `signals_present`:
          • `cited[sig]`            keyword match anywhere in content
          • `cited_with_value[sig]` keyword match AND numeric token
                                    within ±60 chars (PR #258)
      - if the signal is NOT in `signals_present`:
          • `hallucinated[sig]`     keyword match AND numeric token
                                    within ±60 chars (PR #259).
                                    The numeric requirement is the
                                    discriminator: a persona writing
                                    "RSI is overbought territory"
                                    from training data is fine, but
                                    "RSI 67.3 已逼近超買" without our
                                    prompt supplying the value is a
                                    fabrication.
    """
    audit = TurnAudit(round=round_no, persona_id=persona_id, stance=stance)
    for signal_key, patterns in _SIGNAL_KEYWORDS.items():
        if not patterns:
            continue
        cited = _match_keywords(content, patterns)
        cited_with_value = (
            cited and _has_value_near_keyword(content, patterns)
        )
        if signal_key in signals_present:
            audit.cited[signal_key] = cited
            audit.cited_with_value[signal_key] = cited_with_value
        else:
            audit.hallucinated[signal_key] = cited_with_value
    return audit


# ── DB-bound entry point ──────────────────────────────────────────


async def audit_discussion(
    db: AsyncSession,
    discussion_id: uuid.UUID,
) -> DiscussionAudit | None:
    """Build a `DiscussionAudit` from the persisted round contexts +
    turns. Returns None when the discussion doesn't exist OR has no
    persisted round contexts (auto-run rows older than the PR #209
    snapshot column won't have them)."""
    disc = await db.scalar(
        select(Discussion).where(Discussion.id == discussion_id)
    )
    if disc is None:
        return None

    contexts = (await db.scalars(
        select(DiscussionRoundContext)
        .where(DiscussionRoundContext.discussion_id == discussion_id)
        .order_by(DiscussionRoundContext.round.asc())
    )).all()
    if not contexts:
        return None

    turns = (await db.scalars(
        select(DiscussionTurn)
        .where(DiscussionTurn.discussion_id == discussion_id)
        .order_by(
            DiscussionTurn.round.asc(),
            DiscussionTurn.turn_index.asc(),
        )
    )).all()
    turns_by_round: dict[int, list[DiscussionTurn]] = defaultdict(list)
    for t in turns:
        turns_by_round[t.round].append(t)

    audit = DiscussionAudit(
        discussion_id=str(discussion_id), topic=disc.topic,
    )
    for ctx_row in contexts:
        present = _detect_present_signals(ctx_row.context or {})
        round_audit = RoundAudit(round=ctx_row.round, signals_present=present)
        for t in turns_by_round.get(ctx_row.round, []):
            round_audit.turns.append(audit_turn(
                round_no=ctx_row.round,
                persona_id=t.persona_id,
                stance=t.stance,
                content=t.content or "",
                signals_present=present,
            ))
        audit.rounds.append(round_audit)
    return audit


# ── Bulk roll-up across recent discussions ────────────────────────


@dataclass
class BulkAuditSummary:
    """Aggregate signal-citation stats across N recent discussions.

    `discussions_audited` is the count of rows that had persisted
    round contexts AND at least one persona turn — discussions
    written before PR #209 (no round context column) or rounds where
    every persona timed out (no turn rows) are silently excluded.

    `coverage[signal]` mirrors `DiscussionAudit.coverage()` but
    summed across every audited discussion. Citation rate per
    signal: `cited / persona_count`.

    `hallucinations[signal]` mirrors `DiscussionAudit.hallucinations()`
    summed across every audited discussion. Hallucination rate per
    signal: `hallucinated / persona_count_absent` (PR #259).
    """
    discussions_audited: int
    discussion_ids: list[str]
    coverage: dict[str, dict[str, int]] = field(default_factory=dict)
    hallucinations: dict[str, dict[str, int]] = field(default_factory=dict)


async def audit_recent_discussions(
    db: AsyncSession,
    *,
    limit: int = 30,
    market: str | None = None,
    status: str = "concluded",
) -> BulkAuditSummary:
    """Roll up signal-citation stats across the most recent `limit`
    discussions matching `market` / `status`. Default is last 30
    concluded discussions in any market.

    Returns a BulkAuditSummary; never raises (per-discussion failure
    is logged and skipped so one bad row can't blank the report).
    """
    stmt = select(Discussion.id).where(Discussion.status == status)
    if market is not None:
        stmt = stmt.where(Discussion.market == market)
    stmt = stmt.order_by(Discussion.created_at.desc()).limit(limit)
    candidate_ids = (await db.scalars(stmt)).all()

    summary = BulkAuditSummary(discussions_audited=0, discussion_ids=[])
    rolled: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "present": 0, "cited": 0,
            "cited_with_value": 0, "persona_count": 0,
        }
    )
    rolled_hall: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "absent_rounds": 0, "hallucinated": 0,
            "persona_count_absent": 0,
        }
    )

    for did in candidate_ids:
        try:
            audit = await audit_discussion(db, did)
        except Exception as exc:
            log.warning(
                "signal_audit.discussion_failed",
                extra={"discussion_id": str(did), "error": str(exc)},
            )
            continue
        if audit is None:
            continue   # no persisted round contexts
        summary.discussions_audited += 1
        summary.discussion_ids.append(str(did))
        for sig, stats in audit.coverage().items():
            rolled[sig]["present"] += stats["present"]
            rolled[sig]["cited"] += stats["cited"]
            rolled[sig]["cited_with_value"] += stats.get("cited_with_value", 0)
            rolled[sig]["persona_count"] += stats["persona_count"]
        for sig, stats in audit.hallucinations().items():
            rolled_hall[sig]["absent_rounds"] += stats["absent_rounds"]
            rolled_hall[sig]["hallucinated"] += stats["hallucinated"]
            rolled_hall[sig]["persona_count_absent"] += (
                stats["persona_count_absent"]
            )

    summary.coverage = dict(rolled)
    summary.hallucinations = dict(rolled_hall)
    return summary


# ── PR #263: daily snapshot persistence for sparkline trends ──────


async def snapshot_audit_to_history(
    db: AsyncSession,
    *,
    captured_at: date | None = None,
    market: str | None = None,
    days_window: int = 7,
) -> int:
    """Compute today's bulk audit and upsert one row per signal into
    `signal_audit_history`. Idempotent against the (captured_at,
    market, signal) PK — re-running on the same UTC day overwrites
    in place rather than appending duplicates.

    Returns the number of rows written. Zero is a normal no-op when
    the audit window contains no concluded discussions.

    `days_window=7` matches the AdminPage SignalAuditCard default;
    the snapshot represents "the last 7 days of activity, rolled up
    on UTC day X".
    """
    from datetime import datetime as _dt
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    from models.signal_audit_history import SignalAuditHistory

    # `audit_recent_discussions` pulls the most recent N concluded
    # discussions; `days_window` is informational here (the bulk
    # audit doesn't take a day filter — it's already implicit via
    # `limit`). We pass through unchanged.
    summary = await audit_recent_discussions(
        db, limit=days_window * 10, market=market,
    )
    when = captured_at or _dt.utcnow().date()

    all_signals = (
        set(summary.coverage.keys())
        | set(summary.hallucinations.keys())
    )
    rows: list[dict[str, Any]] = []
    for sig in all_signals:
        cov = summary.coverage.get(sig, {})
        hall = summary.hallucinations.get(sig, {})
        rows.append({
            "captured_at":            when,
            "market":                 market,
            "signal":                 sig,
            "discussions_audited":    summary.discussions_audited,
            "present_count":          cov.get("present", 0),
            "cited_count":            cov.get("cited", 0),
            "cited_with_value_count": cov.get("cited_with_value", 0),
            "hallucinated_count":     hall.get("hallucinated", 0),
            "persona_count":          cov.get("persona_count", 0),
            "persona_count_absent":   hall.get("persona_count_absent", 0),
        })
    if not rows:
        return 0

    # Dialect-aware upsert: aiosqlite (tests) vs asyncpg (production).
    # Postgres has a unique-constraint quirk with NULLABLE PK columns
    # — `market` can be NULL — so the conflict target is explicit.
    dialect = db.bind.dialect.name if db.bind else "postgresql"
    if dialect == "sqlite":
        stmt = sqlite_insert(SignalAuditHistory).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["captured_at", "market", "signal"],
            set_={
                col: getattr(stmt.excluded, col) for col in (
                    "discussions_audited", "present_count",
                    "cited_count", "cited_with_value_count",
                    "hallucinated_count", "persona_count",
                    "persona_count_absent",
                )
            },
        )
    else:
        stmt = pg_insert(SignalAuditHistory).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["captured_at", "market", "signal"],
            set_={
                col: getattr(stmt.excluded, col) for col in (
                    "discussions_audited", "present_count",
                    "cited_count", "cited_with_value_count",
                    "hallucinated_count", "persona_count",
                    "persona_count_absent",
                )
            },
        )
    await db.execute(stmt)
    await db.commit()
    return len(rows)


async def read_all_signals_history(
    db: AsyncSession,
    *,
    market: str | None = None,
    days: int = 30,
) -> dict[str, list[dict[str, Any]]]:
    """Bulk variant of `read_signal_history`: returns
    `{signal: [points...]}` for every signal that has ≥ 1 row in
    the window. Used by the AdminPage sparkline column to fetch
    all signals' trends in one round-trip instead of N parallel
    requests per row.
    """
    from datetime import datetime as _dt, timedelta as _td

    from models.signal_audit_history import SignalAuditHistory

    today = _dt.utcnow().date()
    cutoff = today - _td(days=days)
    stmt = (
        select(SignalAuditHistory)
        .where(SignalAuditHistory.captured_at >= cutoff)
        .order_by(
            SignalAuditHistory.signal.asc(),
            SignalAuditHistory.captured_at.asc(),
        )
    )
    if market is None:
        stmt = stmt.where(SignalAuditHistory.market.is_(None))
    else:
        stmt = stmt.where(SignalAuditHistory.market == market)
    rows = (await db.scalars(stmt)).all()

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        denom_cite = r.persona_count or 0
        denom_hall = r.persona_count_absent or 0
        grouped[r.signal].append({
            "captured_at":            r.captured_at.isoformat(),
            "discussions_audited":    r.discussions_audited,
            "present_count":          r.present_count,
            "cited_count":            r.cited_count,
            "cited_with_value_count": r.cited_with_value_count,
            "hallucinated_count":     r.hallucinated_count,
            "persona_count":          r.persona_count,
            "persona_count_absent":   r.persona_count_absent,
            "citation_rate": (
                round(r.cited_count / denom_cite, 4)
                if denom_cite else 0.0
            ),
            "value_citation_rate": (
                round(r.cited_with_value_count / denom_cite, 4)
                if denom_cite else 0.0
            ),
            "hallucination_rate": (
                round(r.hallucinated_count / denom_hall, 4)
                if denom_hall else 0.0
            ),
        })
    return dict(grouped)


async def read_signal_history(
    db: AsyncSession,
    *,
    signal: str,
    market: str | None = None,
    days: int = 30,
) -> list[dict[str, Any]]:
    """Return the per-day series for a single signal over the last
    `days` UTC days. Output is sorted ascending by date so the
    frontend can plot left-to-right without reversing.

    Each row carries the raw counters plus pre-computed rates so
    the sparkline renderer doesn't need to handle div-by-zero:
        citation_rate         = cited / persona_count      (or 0)
        value_citation_rate   = cited_with_value / persona_count
        hallucination_rate    = hallucinated / persona_count_absent

    Days with zero discussions audited still appear in the result
    set if a snapshot row exists — the cron writes one row per
    signal per day regardless of activity. Days where the cron
    didn't run are simply absent (gap in series).
    """
    from datetime import datetime as _dt, timedelta as _td

    from models.signal_audit_history import SignalAuditHistory

    today = _dt.utcnow().date()
    cutoff = today - _td(days=days)
    stmt = (
        select(SignalAuditHistory)
        .where(
            SignalAuditHistory.signal == signal,
            SignalAuditHistory.captured_at >= cutoff,
        )
        .order_by(SignalAuditHistory.captured_at.asc())
    )
    if market is None:
        stmt = stmt.where(SignalAuditHistory.market.is_(None))
    else:
        stmt = stmt.where(SignalAuditHistory.market == market)
    rows = (await db.scalars(stmt)).all()

    out: list[dict[str, Any]] = []
    for r in rows:
        denom_cite = r.persona_count or 0
        denom_hall = r.persona_count_absent or 0
        out.append({
            "captured_at":           r.captured_at.isoformat(),
            "discussions_audited":   r.discussions_audited,
            "present_count":         r.present_count,
            "cited_count":           r.cited_count,
            "cited_with_value_count": r.cited_with_value_count,
            "hallucinated_count":    r.hallucinated_count,
            "persona_count":         r.persona_count,
            "persona_count_absent":  r.persona_count_absent,
            "citation_rate": (
                round(r.cited_count / denom_cite, 4)
                if denom_cite else 0.0
            ),
            "value_citation_rate": (
                round(r.cited_with_value_count / denom_cite, 4)
                if denom_cite else 0.0
            ),
            "hallucination_rate": (
                round(r.hallucinated_count / denom_hall, 4)
                if denom_hall else 0.0
            ),
        })
    return out
