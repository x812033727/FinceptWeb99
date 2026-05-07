"""Persistence + retrieval for the discussion learning loop.

Each post-mortem (verdict=miss) re-synthesizer pass extracts a small
list of structured "what we got wrong" lessons. They land in
`discussion_lessons` (migration 0040) and are read back into the
ctx of subsequent discussions on the same market via
`fetch_relevant_lessons` so personas see prior failures alongside
the live signals.

Quality gates at write time
---------------------------
  - `len(lesson_text.strip()) >= 20` — drop fragments that don't carry
    enough signal to be useful in a future prompt.
  - `len(lesson_text) <= 200` — truncate verbose LLM emissions so a
    single oversized lesson can't blow the round-1 token budget.
  - `lesson_text_hash` dedup — if the same exact text was persisted
    for the same owner within the last 60 days, drop the duplicate
    (pointlessly re-injecting identical advice trains the personas to
    over-anchor on it).
  - Hard cap of 5 lessons written per discussion so an over-eager
    synthesizer can't dump 30 wandering takeaways in one pass.

Backtest correctness
--------------------
  `fetch_relevant_lessons` filters by `as_of_date <= anchor` (where
  anchor is `Discussion.as_of_date` for backtest mode, or `None` for
  live which matches everything). Without this filter, sweep
  discussions anchored at 2025-06-01 would see lessons learned
  AFTER that date — using future knowledge to grade past
  decisions, invalidating the entire backtest signal.

Score formula
-------------
  `recency = exp(-age_days * ln(2) / halflife)` * symbol_boost
  where halflife defaults to 60 days (admin-tunable via runtime
  config) and symbol_boost = 1.5 when `focus_symbols ∩
  related_symbols` is non-empty.

  Anchor for `age_days` is `discussion.as_of_date` (backtest) or
  today (live). Using `lesson.as_of_date` rather than `created_at`
  for the lesson side means freshness is governed by when the
  lesson's event occurred, not when the synthesizer happened to
  emit it — important if you backfill lessons retroactively.
"""
from __future__ import annotations

import hashlib
import logging
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from models.discussion_lesson import DiscussionLesson

log = logging.getLogger(__name__)

# Seven categories the synthesizer must constrain itself to. Anything
# outside this set is coerced to "other" at write time.
#
# PR-B0 shipped 5 miss-side categories. PR-B3 adds 2 win-side
# categories so the post-mortem on a successful discussion can capture
# *why* it worked (which signal combo + thesis actually played out)
# without poisoning the existing miss-side accounting.
ALLOWED_CATEGORIES: frozenset[str] = frozenset({
    # miss-side (PR-B0)
    "missed_sector",
    "wrong_signal_weight",
    "missing_data",
    "over_confidence",
    "other",
    # win-side (PR-B3)
    "correct_signal_combo",
    "successful_thesis",
})

# Win-side categories — used by PR-B3 to split the fetch render into
# 「過去命中經驗」vs 「過去失誤教訓」blocks so the LLM sees positive
# vs negative cases unambiguously rather than mixed in one list.
WIN_CATEGORIES: frozenset[str] = frozenset({
    "correct_signal_combo",
    "successful_thesis",
})

_MIN_LESSON_LEN = 20
_MAX_LESSON_LEN = 200
_MAX_LESSONS_PER_DISCUSSION = 5
_DEDUP_WINDOW_DAYS = 60

# PR-B0: Regime tagging at lesson-write time. The 6 valid combinations
# of (VIX bucket × TAIEX trend); the 3 weak combinations
# (low_VIX × down, high_VIX × up, high_VIX × range) classify as None
# so the lesson lands without a regime tag rather than mis-tagged.
_VALID_REGIME_LABELS: frozenset[str] = frozenset({
    "low_up", "low_range", "mid_up", "mid_down", "mid_range", "high_down",
})


def _classify_regime(ctx: dict[str, Any] | None) -> str | None:
    """Classify the market state at lesson-write time using two
    signals already in ctx — TAIWAN VIX (`taiwan_vix.value`) and
    TAIEX 5d/20d MA cross (from `index.history[].close`).

    Returns one of 6 labels: low_up / low_range / mid_up / mid_down /
    mid_range / high_down, or None when:
      - ctx is missing / wrong shape
      - VIX field is absent (cron hasn't populated, weekend, outage)
      - TAIEX history has fewer than 20 closes
      - the (VIX, trend) combination falls into the 3 weak buckets

    Persistence is best-effort: caller writes the lesson regardless,
    NULL regime just means PR-B1 doesn't get the same-regime fetch
    boost for this row.
    """
    if not isinstance(ctx, dict):
        return None
    vix_block = ctx.get("taiwan_vix")
    if not isinstance(vix_block, dict):
        return None
    try:
        vix_value = float(vix_block.get("value"))
    except (TypeError, ValueError):
        return None
    if vix_value < 15:
        vix_bucket = "low"
    elif vix_value > 25:
        vix_bucket = "high"
    else:
        vix_bucket = "mid"

    index_block = ctx.get("index")
    if not isinstance(index_block, dict):
        return None
    history = index_block.get("history")
    if not isinstance(history, list) or len(history) < 20:
        return None
    closes: list[float] = []
    for bar in history:
        if not isinstance(bar, dict):
            continue
        try:
            closes.append(float(bar.get("close")))
        except (TypeError, ValueError):
            continue
    if len(closes) < 20:
        return None

    ma5_now = sum(closes[-5:]) / 5
    ma20_now = sum(closes[-20:]) / 20
    # Slope of the 5d MA: compare to itself 5 bars ago. When history
    # is exactly 20 bars we still have closes[-10:-5] available.
    ma5_then = sum(closes[-10:-5]) / 5 if len(closes) >= 10 else ma5_now

    if ma5_now > ma20_now and ma5_now > ma5_then:
        trend = "up"
    elif ma5_now < ma20_now and ma5_now < ma5_then:
        trend = "down"
    else:
        trend = "range"

    label = f"{vix_bucket}_{trend}"
    return label if label in _VALID_REGIME_LABELS else None


@dataclass(frozen=True)
class LessonSummary:
    """Compact view of a lesson surfaced into the ctx prompt."""
    id: int
    as_of_date: str        # ISO
    category: str
    lesson_text: str
    related_symbols: list[str]
    missed_winners: list[str]
    tier: str = "episodic"
    regime: str | None = None


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize_category(raw: Any) -> str:
    if not isinstance(raw, str):
        return "other"
    cleaned = raw.strip().lower()
    return cleaned if cleaned in ALLOWED_CATEGORIES else "other"


def _normalize_symbols(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for s in raw:
        if not isinstance(s, str):
            continue
        s = s.strip()
        if not s or len(s) > 32:
            continue
        if s not in out:
            out.append(s)
    return out[:20]


async def extract_and_persist_lessons(
    db: AsyncSession,
    *,
    discussion_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    market: str,
    as_of_date: date,
    lessons_payload: Any,
    ctx: dict[str, Any] | None = None,
    verdict: str | None = None,
) -> list[DiscussionLesson]:
    """Parse + persist lessons from a synthesizer payload.

    `lessons_payload` is whatever the synthesizer emitted under the
    `lessons` key — defensively assumed to be `list[dict]` but
    handled gracefully for any other shape (None / dict / str all
    return [] silently). Quality-gated per the module docstring.

    `ctx` is the `gather_market_context` snapshot the synthesizer
    reasoned over; PR-B0 reads it once to compute the `regime`
    label persisted on every new lesson row. Optional + best-effort
    — older callers passing nothing get the legacy regime=NULL
    behavior.

    `verdict` (PR-B3) — when 'win', the synthesizer was prompted
    with the win-case template so any emitted lessons are expected
    to use the win-side categories (`correct_signal_combo`,
    `successful_thesis`). When 'miss' or None, the legacy miss-
    side categories apply. We don't enforce the category mapping
    here (the synthesizer's free-form output is normalized to
    'other' on mismatch) — the gate is purely informational so
    callers can post-process telemetry by win/miss split.

    Returns the list of newly-persisted rows (0..5). Caller can use
    the count for telemetry but does not need to flush — this
    function commits.
    """
    if not isinstance(lessons_payload, list) or not lessons_payload:
        return []

    regime_label = _classify_regime(ctx)
    written: list[DiscussionLesson] = []
    seen_hashes_in_payload: set[str] = set()
    dedup_threshold = datetime.now(UTC) - timedelta(days=_DEDUP_WINDOW_DAYS)

    for raw in lessons_payload:
        if len(written) >= _MAX_LESSONS_PER_DISCUSSION:
            break
        if not isinstance(raw, dict):
            continue
        text_raw = raw.get("lesson_text") or ""
        if not isinstance(text_raw, str):
            continue
        text = text_raw.strip()
        if len(text) < _MIN_LESSON_LEN:
            continue
        if len(text) > _MAX_LESSON_LEN:
            text = text[:_MAX_LESSON_LEN]

        h = _hash_text(text)
        if h in seen_hashes_in_payload:
            continue
        seen_hashes_in_payload.add(h)

        # DB-side dedup: same owner, same hash, written in the last
        # 60 days. Best-effort — any DB hiccup falls through to write
        # (a duplicated lesson is annoying but not corrupting).
        try:
            existing = await db.scalar(
                select(DiscussionLesson.id).where(
                    DiscussionLesson.owner_user_id == owner_user_id,
                    DiscussionLesson.lesson_text_hash == h,
                    DiscussionLesson.created_at >= dedup_threshold,
                ).limit(1)
            )
        except Exception as exc:
            log.debug("discussion_lessons.dedup_check_failed",
                      extra={"error": str(exc)})
            existing = None
        if existing is not None:
            try:
                from middleware.metrics import LESSONS_DEDUP_SKIPPED_TOTAL
                LESSONS_DEDUP_SKIPPED_TOTAL.labels(market=market).inc()
            except Exception:
                pass
            continue

        row = DiscussionLesson(
            discussion_id=discussion_id,
            owner_user_id=owner_user_id,
            market=market,
            as_of_date=as_of_date,
            category=_normalize_category(raw.get("category")),
            lesson_text=text,
            lesson_text_hash=h,
            related_symbols=_normalize_symbols(raw.get("related_symbols")),
            missed_winners=_normalize_symbols(raw.get("missed_winners")),
            regime=regime_label,
        )
        db.add(row)
        written.append(row)

    if not written:
        return []

    try:
        await db.commit()
    except Exception as exc:
        log.warning("discussion_lessons.commit_failed: %s", exc)
        await db.rollback()
        return []

    try:
        from middleware.metrics import LESSONS_PERSISTED_TOTAL
        for row in written:
            LESSONS_PERSISTED_TOTAL.labels(
                market=market, category=row.category,
            ).inc()
    except Exception:
        pass

    return written


# ── retrieval ────────────────────────────────────────────────────


async def _resolve_halflife(db: AsyncSession) -> int:
    try:
        from services.runtime_config_service import get_int as _get_int
        return await _get_int(db, "LESSONS_DECAY_HALFLIFE_DAYS")
    except Exception:
        return settings.LESSONS_DECAY_HALFLIFE_DAYS


# PR-B1: tier-aware half-life multipliers. base = the runtime
# configured `LESSONS_DECAY_HALFLIFE_DAYS` (default 60). episodic
# lessons keep that exact half-life; semantic ones decay 3x slower
# (180d default); structural lessons don't decay at all.
_TIER_HALFLIFE_MULTIPLIER: dict[str, float | None] = {
    "episodic":   1.0,
    "semantic":   3.0,
    "structural": None,   # never decays
}

# PR-B1: regime-match boost. Same regime doubles the score, opposite
# regime cuts it to 0.7 (still kept as a tail candidate so a sweep
# in a thin regime still gets *some* prior context). NULL on either
# side stays neutral — we don't punish lessons that landed before
# regime tagging existed (B0).
_REGIME_MATCH_BOOST = 2.0
_REGIME_MISMATCH_BOOST = 0.7

# PR-B3: win-side lessons (correct_signal_combo / successful_thesis)
# get a multiplicative penalty so the ranking treats them with
# slightly more skepticism than miss-side lessons. Survivor-bias
# motivation: the LLM is much more likely to spin a confident-looking
# narrative in hindsight than to honestly identify failure modes,
# so the calibrated long-term value of a win lesson is roughly 80%
# of a miss lesson at the same age + regime.
_WIN_LESSON_BOOST = 0.8


def _score(
    lesson: DiscussionLesson, *,
    anchor: date, focus_symbols: set[str], halflife_days: int,
    current_regime: str | None = None,
) -> float:
    tier = getattr(lesson, "tier", None) or "episodic"
    multiplier = _TIER_HALFLIFE_MULTIPLIER.get(tier, 1.0)

    age_days = max(0, (anchor - lesson.as_of_date).days)
    if multiplier is None:
        recency = 1.0   # structural lessons don't decay
    else:
        half = max(1, int(halflife_days * multiplier))
        recency = math.exp(-age_days * math.log(2) / half)

    related = set(lesson.related_symbols or [])
    symbol_boost = (
        1.5 if focus_symbols and (focus_symbols & related) else 1.0
    )

    lesson_regime = getattr(lesson, "regime", None)
    if not current_regime or not lesson_regime:
        regime_boost = 1.0
    elif lesson_regime == current_regime:
        regime_boost = _REGIME_MATCH_BOOST
    else:
        regime_boost = _REGIME_MISMATCH_BOOST

    # PR-B3: 0.8x penalty on win-side lessons so survivor bias
    # doesn't make them outrank a same-age miss-side lesson at
    # the top of the ranking.
    category = getattr(lesson, "category", "") or ""
    win_boost = _WIN_LESSON_BOOST if category in WIN_CATEGORIES else 1.0

    return recency * symbol_boost * regime_boost * win_boost


def _to_summary(lesson: DiscussionLesson) -> LessonSummary:
    return LessonSummary(
        id=lesson.id,
        as_of_date=lesson.as_of_date.isoformat(),
        category=lesson.category,
        lesson_text=lesson.lesson_text[:_MAX_LESSON_LEN],
        related_symbols=list(lesson.related_symbols or []),
        missed_winners=list(lesson.missed_winners or []),
        tier=getattr(lesson, "tier", None) or "episodic",
        regime=getattr(lesson, "regime", None),
    )


async def fetch_relevant_lessons(
    db: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    market: str,
    focus_symbols: set[str] | None = None,
    discussion_as_of: date | None = None,
    limit: int = 5,
    current_regime: str | None = None,
) -> list[LessonSummary]:
    """Owner+market scoped fetch with backtest-safe time filter.

    `discussion_as_of` is the anchor. When set, only lessons with
    `as_of_date <= anchor` are returned (so a backtest discussion
    can't peek at lessons learned in its future); when None
    (live mode) the filter is bypassed.

    `current_regime` (PR-B1) is the regime label for the current
    discussion's market state — when provided, lessons learned
    under the same regime get a 2x boost in the ranking, lessons
    from a different regime drop to 0.7x (still eligible — never
    fully dropped so a thin regime can still surface fallback
    context). NULL on either side is neutral.

    Returns up to `limit` `LessonSummary` rows ranked by the
    time-decay × symbol-boost × regime-boost score, descending.
    """
    if limit <= 0 or owner_user_id is None or not market:
        return []

    halflife = await _resolve_halflife(db)
    anchor = discussion_as_of or datetime.now(UTC).date()

    # Cap candidate fan-out to avoid pathological growth of
    # discussion_lessons making the per-discussion fetch slow.
    # 200 = ~6 months of typical usage; well above what we'd ever
    # want to inject.
    candidate_window = max(limit * 20, 50)

    conditions = [
        DiscussionLesson.owner_user_id == owner_user_id,
        DiscussionLesson.market == market,
        # PR-4c: archived lessons are soft-deleted from the fetch
        # ranking. The row is preserved (audit / unarchive surface)
        # but doesn't pollute persona prompts.
        DiscussionLesson.archived_at.is_(None),
    ]
    if discussion_as_of is not None:
        conditions.append(DiscussionLesson.as_of_date <= discussion_as_of)

    stmt = (
        select(DiscussionLesson)
        .where(and_(*conditions))
        .order_by(DiscussionLesson.as_of_date.desc())
        .limit(candidate_window)
    )
    rows = (await db.scalars(stmt)).all()
    if not rows:
        return []

    focus = focus_symbols or set()
    scored: list[tuple[float, DiscussionLesson]] = []
    for lesson in rows:
        scored.append((
            _score(
                lesson, anchor=anchor, focus_symbols=focus,
                halflife_days=halflife,
                current_regime=current_regime,
            ),
            lesson,
        ))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [_to_summary(lesson) for _score_val, lesson in scored[:limit]]


def summary_to_dict(s: LessonSummary) -> dict[str, Any]:
    """Serialize a LessonSummary for ctx injection. Includes the
    `id` so the round-context snapshot carries it through —
    `lesson_tier_service.record_lesson_outcome` walks the snapshot
    to bump hit_count after a verdict lands. The ID is an internal
    autoincrement PK; the LLM ignores it but the audit path
    depends on it.
    """
    return {
        "id":             s.id,
        "as_of_date":     s.as_of_date,
        "category":       s.category,
        "lesson_text":    s.lesson_text,
        "related_symbols": list(s.related_symbols),
        "missed_winners":  list(s.missed_winners),
        "tier":           s.tier,
        "regime":         s.regime,
    }


# ── admin CRUD ───────────────────────────────────────────────────


async def list_recent_lessons(
    db: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    market: str | None = None,
    limit: int = 50,
) -> list[DiscussionLesson]:
    """Admin / owner readback for the DiscussionLessonsCard. Scoped
    to the requesting owner; admin sees their own bucket only (a
    future enhancement could expose an `all_users` toggle when role
    == admin). Most-recent first."""
    conditions = [DiscussionLesson.owner_user_id == owner_user_id]
    if market:
        conditions.append(DiscussionLesson.market == market)
    stmt = (
        select(DiscussionLesson)
        .where(and_(*conditions))
        .order_by(DiscussionLesson.created_at.desc())
        .limit(min(max(limit, 1), 200))
    )
    return list((await db.scalars(stmt)).all())


async def delete_lesson(
    db: AsyncSession,
    *,
    lesson_id: int,
    owner_user_id: uuid.UUID,
) -> bool:
    """Owner-scoped hard delete. Returns True iff a row was removed.
    No-op (returns False) when the row belongs to another user or
    doesn't exist — silently ignoring foreign deletes prevents
    accidental cross-owner mutation through enumeration."""
    row = await db.scalar(
        select(DiscussionLesson).where(
            and_(
                DiscussionLesson.id == lesson_id,
                DiscussionLesson.owner_user_id == owner_user_id,
            )
        )
    )
    if row is None:
        return False
    await db.delete(row)
    await db.commit()
    return True


def lesson_to_dict(row: DiscussionLesson) -> dict[str, Any]:
    """Admin/list serialisation. Includes the row id so the UI can
    issue deletes."""
    return {
        "id":              row.id,
        "discussion_id":   str(row.discussion_id),
        "market":          row.market,
        "as_of_date":      row.as_of_date.isoformat(),
        "category":        row.category,
        "lesson_text":     row.lesson_text,
        "related_symbols": list(row.related_symbols or []),
        "missed_winners":  list(row.missed_winners or []),
        "created_at":      row.created_at.isoformat() if row.created_at else None,
    }
