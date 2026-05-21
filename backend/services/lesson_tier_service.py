"""Lesson usage telemetry + tier promotion (PR-B2).

PR-B0 added the columns; PR-B1 wired regime + tier into the fetch
ranking; PR-B2 closes the loop by tracking which lessons actually
got cited in a discussion's prompt and how often those discussions
ended in a win, then promoting consistently-useful episodic
lessons up to the longer-lived `semantic` tier.

Flow:

  - Every fetch that lands a lesson in a discussion's ctx prompt
    bumps `usage_count` and stamps `last_used_at` via
    `record_lesson_usage`. Called from the lessons context block
    after the per-round ctx is assembled.

  - When a discussion's verdict resolves to win, every lesson that
    appeared in any of its persisted round-context snapshots gets
    its `hit_count` bumped via `record_lesson_outcome`. Hooked
    from `post_mortem_service.evaluate_recommendation_outcome`'s
    write path (or any other verdict resolver) so live + backtest
    + auto-run all funnel through the same accounting.

  - `promote_eligible_lessons` walks episodic lessons in a market
    and promotes those meeting `usage_count >= 5 AND
    hit_count / usage_count >= 0.6` to `semantic`. Phase 3 of the
    sweep worker calls this so promotion happens roughly once per
    sweep without a separate cron.

The structural tier is admin-only — `promote_to_structural` is
exposed via `/admin/lessons/{id}/promote` for the operator's
manual override; nothing else writes to it. This is the
intentional escape valve: episodic / semantic should never
auto-promote to structural because the threshold of "permanent
advice" is qualitative judgement the system can't reliably
infer. Once an admin marks a lesson structural the recency decay
is fully turned off and the only way out is another admin call.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Iterable
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.discussion import Discussion
from models.discussion_lesson import DiscussionLesson
from models.discussion_round_context import DiscussionRoundContext

log = logging.getLogger(__name__)


PROMOTE_MIN_USAGE = 5
PROMOTE_MIN_HIT_RATE = 0.6
VALID_TIERS = ("episodic", "semantic", "structural")


async def record_lesson_usage(
    db: AsyncSession,
    *,
    lesson_ids: Iterable[int],
) -> int:
    """Bump `usage_count` + stamp `last_used_at` on every supplied
    lesson row. Returns the number of rows touched. Best-effort —
    a transient DB hiccup logs and returns 0 rather than raising
    so the caller's prompt assembly isn't disturbed.
    """
    ids = [int(i) for i in lesson_ids if i is not None]
    if not ids:
        return 0
    try:
        result = await db.execute(
            update(DiscussionLesson)
            .where(DiscussionLesson.id.in_(ids))
            .values(
                usage_count=DiscussionLesson.usage_count + 1,
                last_used_at=datetime.now(UTC),
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return result.rowcount or 0
    except Exception as exc:
        log.warning(
            "lesson_tier.record_usage_failed",
            extra={"error": str(exc), "n_ids": len(ids)},
        )
        try:
            await db.rollback()
        except Exception:
            pass
        return 0


async def record_lesson_outcome(
    db: AsyncSession,
    *,
    discussion_id: UUID,
) -> dict[str, Any]:
    """Walk every persisted round context for `discussion_id`,
    extract the lesson IDs that landed in any prompt, and bump
    `hit_count` when the discussion's verdict is `win`.

    Returns a status dict for telemetry:
      `{"verdict": str | None, "lesson_ids": [...], "updated": int}`.

    Idempotency: the function bumps once per call. Callers (the
    verdict-write path) should only call it on the verdict
    transition, not on every save. We don't enforce that here —
    overcounting is a soft failure (mostly just inflates
    promotion eligibility), under-counting would be silent which
    is worse.
    """
    discussion = await db.scalar(
        select(Discussion).where(Discussion.id == discussion_id)
    )
    status_payload: dict[str, Any] = {
        "verdict": None, "lesson_ids": [], "updated": 0,
    }
    if discussion is None:
        return status_payload
    status_payload["verdict"] = discussion.verdict
    from services.outcome_classifier import is_winning_verdict
    if not is_winning_verdict(discussion.verdict):
        return status_payload

    snapshots_q = select(DiscussionRoundContext.context).where(
        DiscussionRoundContext.discussion_id == discussion_id,
    )
    snapshots = list((await db.scalars(snapshots_q)).all())
    lesson_ids: set[int] = set()
    for snap in snapshots:
        if not isinstance(snap, dict):
            continue
        recent = snap.get("recent_lessons") or {}
        if not isinstance(recent, dict):
            continue
        for entry in recent.get("market") or []:
            _id = _extract_lesson_id(entry)
            if _id is not None:
                lesson_ids.add(_id)
        per_symbol = recent.get("per_symbol") or {}
        if isinstance(per_symbol, dict):
            for entries in per_symbol.values():
                for entry in entries or []:
                    _id = _extract_lesson_id(entry)
                    if _id is not None:
                        lesson_ids.add(_id)

    if not lesson_ids:
        return status_payload
    status_payload["lesson_ids"] = sorted(lesson_ids)

    try:
        result = await db.execute(
            update(DiscussionLesson)
            .where(DiscussionLesson.id.in_(lesson_ids))
            .values(hit_count=DiscussionLesson.hit_count + 1)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        rowcount = result.rowcount or 0
        status_payload["updated"] = rowcount
        # Audit follow-up MA #3: when rowcount < expected, the
        # round-context snapshot referenced lesson IDs that no
        # longer exist (deleted between context capture and
        # verdict resolution). Surface the gap so the operator
        # can spot "tier promotion is starving for hits because
        # half the bumps are missing the target row" — not just
        # silently fail. The UPDATE itself succeeded; this is
        # informational only.
        expected = len(lesson_ids)
        if rowcount < expected:
            log.warning(
                "lesson_tier.record_outcome_partial",
                extra={
                    "discussion_id": str(discussion_id),
                    "expected": expected,
                    "actual": rowcount,
                    "missing": expected - rowcount,
                },
            )
    except Exception as exc:
        log.warning(
            "lesson_tier.record_outcome_failed",
            extra={
                "discussion_id": str(discussion_id),
                "error": str(exc),
            },
        )
        try:
            await db.rollback()
        except Exception:
            pass
    return status_payload


async def promote_eligible_lessons(
    db: AsyncSession,
    *,
    market: str,
) -> list[dict[str, Any]]:
    """Promote every episodic lesson in `market` that meets
    `usage_count >= PROMOTE_MIN_USAGE AND hit_count / usage_count
    >= PROMOTE_MIN_HIT_RATE` up to `semantic`. Returns the list of
    promoted rows for audit purposes.

    Doesn't promote semantic → structural automatically — that
    transition is qualitative judgement the system can't infer.
    Operators flip individual rows via
    `promote_to_structural` (admin endpoint).
    """
    stmt = select(DiscussionLesson).where(
        DiscussionLesson.market == market,
        DiscussionLesson.tier == "episodic",
        DiscussionLesson.usage_count >= PROMOTE_MIN_USAGE,
    )
    rows = list((await db.scalars(stmt)).all())
    promoted: list[dict[str, Any]] = []
    now = datetime.now(UTC)
    for row in rows:
        usage = row.usage_count or 0
        hits = row.hit_count or 0
        if usage <= 0:
            continue
        rate = hits / usage
        if rate < PROMOTE_MIN_HIT_RATE:
            continue
        row.tier = "semantic"
        row.promoted_at = now
        promoted.append({
            "lesson_id": row.id,
            "usage_count": usage,
            "hit_count": hits,
            "hit_rate": round(rate, 4),
        })
    if promoted:
        await db.commit()
    return promoted


async def promote_to_structural(
    db: AsyncSession,
    *,
    lesson_id: int,
    owner_id: UUID,
) -> DiscussionLesson | None:
    """Admin manual promotion to structural. Owner-scoped: an admin
    can only promote lessons within their own learning history,
    matching the rest of the lesson API surface (`fetch_relevant_
    lessons`, `delete_lesson`, etc. all filter by `owner_user_id`).
    Cross-tenant promotion would silently leak one operator's
    learning history into another's.

    Idempotent — calling on an already-structural lesson re-stamps
    `promoted_at` but doesn't error. Returns None when the lesson
    doesn't exist OR belongs to a different owner; the caller
    surfaces 404 either way (don't distinguish so we don't leak
    the existence of foreign rows).
    """
    row = await db.scalar(
        select(DiscussionLesson).where(
            DiscussionLesson.id == lesson_id,
            DiscussionLesson.owner_user_id == owner_id,
        )
    )
    if row is None:
        return None
    row.tier = "structural"
    row.promoted_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(row)
    return row


def _extract_lesson_id(entry: Any) -> int | None:
    if not isinstance(entry, dict):
        return None
    raw = entry.get("id")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


# ── PR-4c: lesson demotion + archive ──────────────────────────────


# Thresholds for the demotion gate. Picked conservative — a
# semantic lesson is the model's "this still works" verdict, so
# we only demote when the rolling hit-rate has clearly broken.
DEMOTE_HIT_RATE_THRESHOLD = 0.40
DEMOTE_MIN_USAGES = 5     # need ≥5 recent usages to trust the rate
ARCHIVE_UNUSED_DAYS = 60
ARCHIVE_HIT_RATE_THRESHOLD = 0.30
RECENT_HIT_WINDOW = 10


async def update_recent_hit_rates(
    db: AsyncSession,
    *,
    market: str | None = None,
) -> int:
    """Recompute `recent_hit_rate_10` for every non-archived
    lesson, optionally scoped to a market.

    Approximation: we don't track individual usage events; the
    cron computes recent_hit_rate as `min(hit_count, RECENT_HIT_
    WINDOW) / min(usage_count, RECENT_HIT_WINDOW)` which is exact
    when the lesson has ≤ 10 usages and a reasonable proxy
    otherwise (skews slightly conservative — a lesson with 100
    hits on 100 usages all-time will read as 1.0 even after a
    couple of recent misses, but the demotion threshold of 0.40
    leaves enough slack that even a 10-usage drift to 4 hits
    flips the gate).

    Returns the count of rows updated.
    """
    stmt = select(DiscussionLesson).where(
        DiscussionLesson.archived_at.is_(None),
    )
    if market is not None:
        stmt = stmt.where(DiscussionLesson.market == market)
    rows = list((await db.scalars(stmt)).all())
    updated = 0
    for row in rows:
        usage = int(row.usage_count or 0)
        hits = int(row.hit_count or 0)
        if usage <= 0:
            new_rate = None
        else:
            denom = min(usage, RECENT_HIT_WINDOW)
            numer = min(hits, RECENT_HIT_WINDOW)
            new_rate = round(numer / denom, 4)
        if row.recent_hit_rate_10 != new_rate:
            row.recent_hit_rate_10 = new_rate
            updated += 1
    if updated:
        await db.commit()
    return updated


async def demote_stale_lessons(
    db: AsyncSession,
    *,
    market: str,
) -> list[dict[str, Any]]:
    """Demote semantic lessons whose `recent_hit_rate_10` has
    fallen below `DEMOTE_HIT_RATE_THRESHOLD` back to episodic.

    Counterpart to `promote_eligible_lessons`: keeps the tier
    membership honest. A previously-good lesson whose regime has
    shifted should drop back to episodic so it ages out of the
    fetch ranking instead of holding the synthesizer's prompt
    hostage forever.

    Skips lessons without enough recent usages (`usage_count <
    DEMOTE_MIN_USAGES`) — can't trust a rolling rate computed
    over too few samples.

    Returns the list of demoted rows for audit.
    """
    stmt = select(DiscussionLesson).where(
        DiscussionLesson.market == market,
        DiscussionLesson.tier == "semantic",
        DiscussionLesson.archived_at.is_(None),
    )
    rows = list((await db.scalars(stmt)).all())
    demoted: list[dict[str, Any]] = []
    now = datetime.now(UTC)
    for row in rows:
        if (row.usage_count or 0) < DEMOTE_MIN_USAGES:
            continue
        rate = row.recent_hit_rate_10
        if rate is None or rate >= DEMOTE_HIT_RATE_THRESHOLD:
            continue
        row.tier = "episodic"
        row.demoted_at = now
        demoted.append({
            "lesson_id": row.id,
            "previous_tier": "semantic",
            "recent_hit_rate_10": rate,
        })
    if demoted:
        await db.commit()
    return demoted


async def archive_stale_lessons(
    db: AsyncSession,
    *,
    market: str,
) -> list[dict[str, Any]]:
    """Soft-delete lessons that are simultaneously unused-for-
    a-while AND have a low recent hit rate.

    Two conditions, both must hold (intentionally strict):
      - `last_used_at < now - ARCHIVE_UNUSED_DAYS days`
      - `recent_hit_rate_10 < ARCHIVE_HIT_RATE_THRESHOLD`
        OR `usage_count > 0 AND hit_count == 0`

    Skips structural lessons (admin-curated; never auto-archive).
    The OR branch covers lessons that were used a few times,
    never won, and then got ignored — those are dead weight even
    when they have only 1-2 usages, so we don't gate on the
    DEMOTE_MIN_USAGES floor.

    Returns the audit list. Demote_stale_lessons should normally
    run BEFORE this so semantic lessons get a chance to drop to
    episodic and start the archive aging clock — without that
    sequence a tier-promoted but newly-failing lesson would skip
    archive forever.
    """
    from datetime import timedelta as _timedelta

    cutoff = datetime.now(UTC) - _timedelta(days=ARCHIVE_UNUSED_DAYS)
    stmt = select(DiscussionLesson).where(
        DiscussionLesson.market == market,
        DiscussionLesson.tier != "structural",
        DiscussionLesson.archived_at.is_(None),
        DiscussionLesson.last_used_at.is_not(None),
        DiscussionLesson.last_used_at < cutoff,
    )
    rows = list((await db.scalars(stmt)).all())
    archived: list[dict[str, Any]] = []
    now = datetime.now(UTC)
    for row in rows:
        usage = int(row.usage_count or 0)
        hits = int(row.hit_count or 0)
        rate = row.recent_hit_rate_10
        rate_below = rate is not None and rate < ARCHIVE_HIT_RATE_THRESHOLD
        zero_hits = usage > 0 and hits == 0
        if not (rate_below or zero_hits):
            continue
        row.archived_at = now
        archived.append({
            "lesson_id": row.id,
            "tier_at_archive": row.tier,
            "usage_count": usage,
            "hit_count": hits,
            "recent_hit_rate_10": rate,
        })
    if archived:
        await db.commit()
    return archived


__all__ = [
    "PROMOTE_MIN_USAGE",
    "PROMOTE_MIN_HIT_RATE",
    "VALID_TIERS",
    "DEMOTE_HIT_RATE_THRESHOLD",
    "DEMOTE_MIN_USAGES",
    "ARCHIVE_UNUSED_DAYS",
    "ARCHIVE_HIT_RATE_THRESHOLD",
    "RECENT_HIT_WINDOW",
    "record_lesson_usage",
    "record_lesson_outcome",
    "promote_eligible_lessons",
    "promote_to_structural",
    "update_recent_hit_rates",
    "demote_stale_lessons",
    "archive_stale_lessons",
]
