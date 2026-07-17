"""Aggregate "missing data" mentions across post-mortem rounds.

After PR #249 introduced the post-mortem self-critique flow, every
backtest discussion that runs to completion can carry a round of
persona reflections answering question 4 of the prompt:

    "缺失的資料：要正確預測這幾檔，目前 ctx 還缺哪一類資料？
     建議下次加入什麼新訊號？"

Persona answers are free-form Traditional Chinese prose, but they
cluster around recurring data categories — "個股期貨未平倉",
"選擇權 IV 偏斜", "美股期貨開盤", "主力進出分點" etc. This service
keyword-matches those answers and rolls up which categories were
mentioned most often across the audit window. The result is an
empirical "what to build next" backlog grounded in actual analyst
gaps, not operator hunches.

Single consumer: the AdminPage `PostMortemGapsCard` that surfaces
the top-mentioned categories. The same service can be re-used by
a future CI gate that flags drift in the gap profile.

Detection model: regex/keyword OR per category, applied to persona
turns that come AFTER the post-mortem injection (`_user` /
`user_input` row whose content starts with "【事後檢討"). False
positives skew the count UPWARD; the goal of the metric is
relative ranking, where directionality matters more than
precision.

Read-only — no new tables, no migrations.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.discussion import Discussion, DiscussionTurn

log = logging.getLogger(__name__)


# Marker for post-mortem injection turns. Mirrors the literal in
# `services/post_mortem_service.format_post_mortem_prompt`.
_POST_MORTEM_MARKER = "【事後檢討"

# Mirrors `services/discussion_service.USER_PERSONA_ID` /
# `USER_INJECTION_STANCE`. Duplicated here to keep the analysis
# service self-contained — the discussion_service module pulls in
# the full SSE / LLM stack.
_USER_PERSONA_ID = "_user"
_USER_INJECTION_STANCE = "user_input"


# ── Data-gap taxonomy ─────────────────────────────────────────────


# Curated list of data categories personas commonly request. Each
# entry's patterns are checked against post-mortem turn content via
# OR. Categories were chosen by reviewing actual post-mortem text
# from the backtest archive — adjust as new themes emerge.
#
# Categories deliberately exclude signals already in the context
# (RSI / KD / external news / TAIFEX index OI / margin balance /
# revenue YoY / dispositions / 八大行庫 / etc.) — measuring
# "personas mention RSI" is interesting for the signal_audit
# coverage stat, not the gap-analysis stat. Here we want to see
# what they want NEXT.
_DATA_GAP_TAXONOMY: dict[str, list[str]] = {
    # Per-symbol futures + options chain — distinct from the existing
    # TAIFEX index-level positioning block.
    "single_stock_futures": [
        r"個股期貨", r"個股期",
        r"single[_\s]*stock[_\s]*future",
    ],
    "options_iv_skew": [
        r"選擇權.*(?:IV|波動率|偏斜)",
        r"VIX", r"波動率指數", r"implied[_\s]*vol",
        r"put[/／]call[_\s]*ratio", r"PCR",
    ],
    # Earnings / corporate-action calendar — broader than the
    # per-symbol upcoming_event flag.
    "earnings_call_calendar": [
        r"法說會.*行事曆", r"法說.*(?:時程|日程)",
        r"earnings[_\s]*calendar", r"公司治理(?:時程|日程)",
    ],
    "analyst_estimates": [
        r"獲利預測", r"EPS[_\s]*預估",
        r"分析師.*(?:預估|目標價|共識)",
        r"consensus[_\s]*estimate",
    ],
    # Overseas linkage — TW market opens after US close, so
    # overnight US futures + SOX direction is a known gap.
    "us_overnight_futures": [
        r"美股(?:期|期貨)", r"US[_\s]*futures",
        r"那斯達克", r"NASDAQ", r"S&?P[_\s]*500",
        r"SOX", r"費半", r"費城半導體",
        r"道瓊", r"DJIA",
    ],
    # Per-broker chip / dot-level concentration. Distinct from the
    # market-wide foreign-investor net flow (top_foreign_buyers).
    "broker_dot_concentration": [
        r"主力進出", r"分點(?:進出|籌碼)?",
        r"籌碼集中(?!度)", r"買賣家數",
        r"broker[_\s]*concentration",
    ],
    # Real-time intraday order-flow detail. Today's context is
    # all daily-bar.
    "intraday_orderflow": [
        r"逐筆", r"盤中.*(?:量|盤口|委買|委賣)",
        r"intraday[_\s]*order[_\s]*flow",
        r"L1[/／]?L2.*盤口", r"\bDOM\b",
    ],
    # Real-time foreign-investor borrow/lending pulse. Today's
    # securities_lending_trend is end-of-day and lagged.
    "realtime_lending": [
        r"即時.*借券", r"intraday[_\s]*lending",
        r"借券.*(?:當沖|當日|盤中)",
    ],
    # Supply-chain visibility — upstream/downstream news beyond
    # generic per-symbol headlines.
    "supply_chain_news": [
        r"供應鏈(?:新聞|動態|消息)",
        r"上下游(?:出貨|拉貨|庫存)",
        r"supply[_\s]*chain",
    ],
    # Insider transactions & block trades.
    "insider_transactions": [
        r"大股東.*(?:申報|轉讓|增減持)",
        r"董監.*持股", r"insider[_\s]*trade",
        r"鉅額(?:成交|交易)", r"block[_\s]*trade",
    ],
    # Credit ratings + watchlist changes.
    "credit_rating": [
        r"信評(?:升|降|調)", r"credit[_\s]*rating",
        r"展望.*(?:正向|負向|穩定)",
    ],
    # Macroeconomic releases + scheduled data drops.
    "macro_calendar": [
        r"經濟(?:數據|指標).*(?:行事曆|日程)",
        r"FOMC.*(?:時程|日期)",
        r"通膨.*(?:報告|公布)",
        r"economic[_\s]*calendar",
    ],
}


def _match_any(content: str, patterns: list[str]) -> bool:
    for p in patterns:
        if re.search(p, content, re.IGNORECASE):
            return True
    return False


# ── Per-discussion extraction ─────────────────────────────────────


@dataclass
class _DiscussionGapHit:
    discussion_id: str
    persona_id: str
    round: int


@dataclass
class PostMortemGapRow:
    """Per-category roll-up for a single audit window."""
    category: str
    mentions: int                 # raw turn-level matches
    discussions_mentioning: int   # distinct discussions w/ ≥1 hit
    persona_count_mentioning: int # distinct personas w/ ≥1 hit


@dataclass
class PostMortemGapSummary:
    """Audit-window summary surfaced to the AdminPage card."""
    discussions_audited: int      # discussions that had a post-mortem round
    discussion_ids: list[str] = field(default_factory=list)
    gaps: list[PostMortemGapRow] = field(default_factory=list)


async def _find_post_mortem_round(
    db: AsyncSession, discussion_id: Any,
) -> int | None:
    """Round number of the user_input turn that injected the
    post-mortem prompt (content starts with `_POST_MORTEM_MARKER`).
    Returns None when no such injection exists.
    Persona reflections live at rounds STRICTLY GREATER than this.
    """
    stmt = (
        select(DiscussionTurn.round)
        .where(
            DiscussionTurn.discussion_id == discussion_id,
            DiscussionTurn.persona_id == _USER_PERSONA_ID,
            DiscussionTurn.stance == _USER_INJECTION_STANCE,
            DiscussionTurn.content.like(f"{_POST_MORTEM_MARKER}%"),
        )
        .order_by(DiscussionTurn.round.asc())
        .limit(1)
    )
    return await db.scalar(stmt)


async def analyze_recent_post_mortems(
    db: AsyncSession,
    *,
    limit: int = 30,
    market: str | None = None,
) -> PostMortemGapSummary:
    """Roll up data-gap mentions across the most recent ``limit``
    concluded discussions matching ``market``.

    Steps per discussion:
      1. Find the post-mortem injection round (skip if none).
      2. Collect persona turns at round > injection_round.
      3. Run keyword OR per category, accumulate hits.

    The roll-up tracks three counters per category:
      - `mentions`: raw hits at the turn level (a single turn may hit
        multiple categories; one category counted once per turn).
      - `discussions_mentioning`: distinct discussions with at least
        one hit. Useful for "X% of post-mortems flagged this".
      - `persona_count_mentioning`: distinct personas with at least
        one hit. Different personas mentioning the same gap is a
        stronger signal than one persona repeating it.

    Empty result for missing windows is fine — operators don't need
    a special signal for "no post-mortems in window", an empty
    `gaps` list makes that obvious.
    """
    stmt = (
        select(Discussion.id)
        .where(Discussion.status == "done")
    )
    if market is not None:
        stmt = stmt.where(Discussion.market == market)
    stmt = stmt.order_by(Discussion.created_at.desc()).limit(limit)
    candidate_ids = (await db.scalars(stmt)).all()

    summary = PostMortemGapSummary(discussions_audited=0)
    # category → {mentions, discussions, personas} where the latter
    # two are sets that we count at the end.
    rolled: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"mentions": 0, "discussions": set(), "personas": set()}
    )

    for did in candidate_ids:
        try:
            inject_round = await _find_post_mortem_round(db, did)
        except Exception as exc:
            log.warning(
                "post_mortem_analysis.find_round_failed",
                extra={"discussion_id": str(did), "error": str(exc)},
            )
            continue
        if inject_round is None:
            continue   # discussion never had a post-mortem round

        turns = (await db.scalars(
            select(DiscussionTurn).where(and_(
                DiscussionTurn.discussion_id == did,
                DiscussionTurn.round > inject_round,
                DiscussionTurn.persona_id != _USER_PERSONA_ID,
            )).order_by(
                DiscussionTurn.round.asc(),
                DiscussionTurn.turn_index.asc(),
            )
        )).all()
        if not turns:
            continue   # injection happened but personas haven't replied

        summary.discussions_audited += 1
        summary.discussion_ids.append(str(did))

        for t in turns:
            content = t.content or ""
            if not content:
                continue
            for category, patterns in _DATA_GAP_TAXONOMY.items():
                if _match_any(content, patterns):
                    rolled[category]["mentions"] += 1
                    rolled[category]["discussions"].add(str(did))
                    rolled[category]["personas"].add(
                        f"{did}:{t.persona_id}"
                    )

    # Materialise rows. Categories with zero hits are dropped — the
    # UI doesn't need a "0 mentions" line per category, and zero-hit
    # categories will explode the table size as the taxonomy grows.
    summary.gaps = sorted(
        (
            PostMortemGapRow(
                category=cat,
                mentions=stats["mentions"],
                discussions_mentioning=len(stats["discussions"]),
                persona_count_mentioning=len(stats["personas"]),
            )
            for cat, stats in rolled.items()
            if stats["mentions"] > 0
        ),
        key=lambda r: (-r.mentions, r.category),
    )
    return summary


__all__ = [
    "PostMortemGapRow",
    "PostMortemGapSummary",
    "analyze_recent_post_mortems",
]
