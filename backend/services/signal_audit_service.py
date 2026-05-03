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
    "news_sentiment": [
        r"新聞情緒", r"情緒(?:面|偏)", r"sentiment", r"news[_\s]*sentiment",
    ],
    "international_sentiment": [
        r"Fed", r"FOMC", r"國際", r"全球", r"international",
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
    """Per-persona-turn citation map: signal_key → bool (cited?)."""
    round: int
    persona_id: str
    stance: str
    cited: dict[str, bool] = field(default_factory=dict)

    @property
    def cited_count(self) -> int:
        return sum(1 for v in self.cited.values() if v)


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
        """Aggregate `signal_key → {present, cited, persona_count}`
        across every round + every persona. `persona_count` is the
        denominator for the citation rate (signal_present_in_round
        rounds × persona_per_round)."""
        agg: dict[str, dict[str, int]] = defaultdict(
            lambda: {"present": 0, "cited": 0, "persona_count": 0}
        )
        for r in self.rounds:
            for sig in r.signals_present:
                agg[sig]["present"] += 1
                agg[sig]["persona_count"] += len(r.turns)
                for t in r.turns:
                    if t.cited.get(sig):
                        agg[sig]["cited"] += 1
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
        "user_context", "prior_discussions",
    ):
        if _is_meaningful(context.get(top_key)):
            present.add(top_key)
    return present


# ── Citation detection ────────────────────────────────────────────


def _match_keywords(content: str, patterns: list[str]) -> bool:
    """Any-of regex match. Case-insensitive (Chinese tokens unchanged)."""
    for p in patterns:
        if re.search(p, content, re.IGNORECASE):
            return True
    return False


def audit_turn(
    *, round_no: int, persona_id: str, stance: str, content: str,
    signals_present: set[str],
) -> TurnAudit:
    """Return a TurnAudit recording which present-in-context signals
    were keyword-matched in this turn's content. Signals NOT in
    `signals_present` are not checked — citing a signal that wasn't
    in the prompt would mean the LLM hallucinated it, which is a
    different problem than coverage."""
    audit = TurnAudit(round=round_no, persona_id=persona_id, stance=stance)
    for signal_key in signals_present:
        patterns = _SIGNAL_KEYWORDS.get(signal_key)
        if not patterns:
            audit.cited[signal_key] = False
            continue
        audit.cited[signal_key] = _match_keywords(content, patterns)
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
    """
    discussions_audited: int
    discussion_ids: list[str]
    coverage: dict[str, dict[str, int]] = field(default_factory=dict)


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
        lambda: {"present": 0, "cited": 0, "persona_count": 0}
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
            rolled[sig]["persona_count"] += stats["persona_count"]

    summary.coverage = dict(rolled)
    return summary
