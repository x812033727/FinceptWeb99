"""Retroactive lesson-hit recount under the corrected semantics.

Spec Part 2 requires the PR to answer: how many of the existing
episodic lessons WOULD cross the promotion floor once correct
abstentions count? A zero here means the semantics change alone is
insufficient and the threshold discussion starts now, not in two weeks.

From-scratch recount (idempotent), dry-run by default; --apply writes
hit_count (raising only, by default — see below).

ERA MISMATCH, not a double-bump bug: the numerator this script rebuilds
comes from RETAINED discussions only (prod has retained discussion rows
since 2026-07-12), while the denominator, each lesson's `usage_count`,
is a LIFETIME counter that has been accumulating since the lesson
system shipped (2026-05-09). A lesson used steadily since May whose
citing discussions from before mid-July were pruned recounts to a much
lower hit_count than its true lifetime hit rate — not because it
stopped working, but because the evidence for its early hits no longer
exists to recount. Expect this to zero ~296 lessons and lower ~313
more; a WOULD-PROMOTE=0 headline from this alone is a retention-window
artifact, not evidence the semantics fix failed.

Two downstream consumers read hit_count and would treat a lowered
number as real signal rather than a retention-window casualty:
  - `DEMOTE_HIT_RATE_THRESHOLD` (services/lesson_tier_service.py)
    demotes a semantic lesson back to episodic when its rolling
    hit-rate falls below it.
  - `archive_stale_lessons`'s zero-hit branch
    (services/lesson_tier_service.py, run by
    services/backtest_sweep_service.py after EVERY sweep) soft-archives
    any lesson with `usage_count > 0 AND hit_count == 0`.
`--apply` therefore REFUSES to lower any hit_count by default — pass
`--allow-lower` to override, once the era mismatch above is something
an operator has actually accounted for.

Never promotes — the Sunday job owns that.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import date, datetime

from sqlalchemy import select, update

from models.discussion import Discussion
from models.discussion_lesson import DiscussionLesson
from models.discussion_round_context import DiscussionRoundContext
from services.lesson_tier_service import (
    PROMOTE_MIN_HIT_RATE,
    PROMOTE_MIN_USAGE,
    _extract_lesson_id,
    qualifies_for_hit,
)


def _is_experiment(d) -> bool:
    """Mirror of tasks.verify_discussion_outcome.is_experiment — inlined
    so importing this script never pulls in that module's own
    module-scope `from db.session import AsyncSessionLocal` (which
    builds the engine at import time). Importing the real function here
    would defeat the whole point of deferring our own db.session import
    into main() below."""
    return bool((getattr(d, "candidate_snapshot", None) or {}).get("experiment"))


def aggregate_hits(rows: list[tuple[bool, set[int]]]) -> dict[int, int]:
    """One hit per qualifying discussion per cited lesson."""
    counts: Counter[int] = Counter()
    for qualifies, lesson_ids in rows:
        if not qualifies:
            continue
        for lid in lesson_ids:
            counts[lid] += 1
    return dict(counts)


def would_promote(*, usage: int, hits: int) -> bool:
    return (
        usage >= PROMOTE_MIN_USAGE
        and usage > 0
        and hits / usage >= PROMOTE_MIN_HIT_RATE
    )


def partition_changes(
    current: dict[int, int], recomputed: dict[int, int],
) -> tuple[int, int, int]:
    """Compare a lesson's stored `hit_count` against the from-scratch
    recount, per lesson id. `current` must cover every lesson (not just
    ones with a nonzero count) — lessons absent from `recomputed` are
    treated as recounting to 0, same convention as `aggregate_hits`'s
    output.

    Returns `(raised, lowered, zeroed)`: counts of lessons whose
    recomputed count is higher, lower, and (a subset of `lowered`)
    exactly zero. `zeroed` is what feeds the archive-eligibility delta
    — `archive_stale_lessons` soft-archives any `usage_count > 0 AND
    hit_count == 0` row.
    """
    raised = lowered = zeroed = 0
    for lesson_id, old in current.items():
        new = recomputed.get(lesson_id, 0)
        if new > old:
            raised += 1
        elif new < old:
            lowered += 1
            if new == 0:
                zeroed += 1
    return raised, lowered, zeroed


def describe_range(values: list) -> tuple | None:
    """(min, max) over the non-None values, or None if there are none."""
    values = [v for v in values if v is not None]
    if not values:
        return None
    return (min(values), max(values))


def overlap_warning(
    *,
    evidence_range: tuple[datetime, datetime] | None,
    lesson_age_range: tuple[date, date] | None,
) -> str | None:
    """Warn when the evidence window (retained discussions) doesn't
    fully cover the lesson-age range (as_of_date span). If lessons are
    older than the earliest retained discussion, part of their lifetime
    usage_count denominator has NO retained numerator evidence at all —
    the recount for those lessons is undercounted by retention, not by
    merit, and the zero/lowered counts above should be read with that
    in mind."""
    if evidence_range is None or lesson_age_range is None:
        return None
    evidence_start, _evidence_end = evidence_range
    lesson_min, _lesson_max = lesson_age_range
    evidence_start_date = (
        evidence_start.date() if hasattr(evidence_start, "date")
        else evidence_start
    )
    if lesson_min < evidence_start_date:
        return (
            f"WARNING: evidence window starts {evidence_start_date} but "
            f"lessons go back to {lesson_min} — hit_count evidence is "
            f"retention-limited while usage_count is lifetime; recomputed "
            f"hits for lessons older than the evidence window are "
            f"undercounted, not zero by merit."
        )
    return None


def _lesson_ids_from_snapshots(snapshots) -> set[int]:
    ids: set[int] = set()
    for snap in snapshots:
        if not isinstance(snap, dict):
            continue
        recent = snap.get("recent_lessons") or {}
        if not isinstance(recent, dict):
            continue
        for entry in recent.get("market") or []:
            lid = _extract_lesson_id(entry)
            if lid is not None:
                ids.add(lid)
        per_symbol = recent.get("per_symbol") or {}
        if isinstance(per_symbol, dict):
            for entries in per_symbol.values():
                for entry in entries or []:
                    lid = _extract_lesson_id(entry)
                    if lid is not None:
                        ids.add(lid)
    return ids


async def _collect(
    db,
) -> list[tuple[bool, set[int], datetime]]:
    """Returns `(qualifies, cited_lesson_ids, discussion_created_at)`
    per retained, non-experiment, verified discussion. The timestamp
    rides along so `main()` can report the evidence window without a
    second, harder-to-keep-in-sync query."""
    discussions = (await db.scalars(
        select(Discussion).where(Discussion.verdict.is_not(None))
    )).all()
    rows: list[tuple[bool, set[int], datetime]] = []
    for d in discussions:
        if _is_experiment(d):
            continue
        pool = d.pool_performance or {}
        pool_avg = pool.get("avg_return_pct") if isinstance(pool, dict) else None
        qualifies = qualifies_for_hit(d.verdict, pool_avg)
        snaps = (await db.scalars(
            select(DiscussionRoundContext.context).where(
                DiscussionRoundContext.discussion_id == d.id
            )
        )).all()
        rows.append((qualifies, _lesson_ids_from_snapshots(snaps), d.created_at))
    return rows


async def main(apply: bool, allow_lower: bool) -> None:
    from db.session import AsyncSessionLocal
    from tasks.promote_lessons import LESSON_MARKETS

    async with AsyncSessionLocal() as db:
        rows = await _collect(db)
        hits = aggregate_hits([(q, lids) for q, lids, _ts in rows])
        lessons = (await db.scalars(select(DiscussionLesson))).all()

        current_hits = {lesson.id: (lesson.hit_count or 0) for lesson in lessons}
        raised, lowered, zeroed = partition_changes(current_hits, hits)

        tier_counts: Counter[str] = Counter()
        market_promotable: Counter[str] = Counter()
        current_zero_hit = 0
        recomputed_zero_hit = 0
        for lesson in lessons:
            tier_counts[lesson.tier] += 1
            usage = lesson.usage_count or 0
            new_hits = hits.get(lesson.id, 0)
            if usage > 0 and (lesson.hit_count or 0) == 0:
                current_zero_hit += 1
            if usage > 0 and new_hits == 0:
                recomputed_zero_hit += 1
            # Mirror promote_eligible_lessons' own filter (lesson_tier_
            # service.py): only "episodic" lessons are promotion
            # candidates — semantic/structural rows are already past
            # that gate and never re-enter it.
            if lesson.tier == "episodic" and would_promote(
                usage=usage, hits=new_hits,
            ):
                market_promotable[lesson.market] += 1

        # The Sunday cron (tasks/promote_lessons.py) only ever promotes
        # markets in LESSON_MARKETS (today just "TW") — a lesson in any
        # other market clearing the floor is not something that job will
        # act on. Report both so a non-TW lesson crossing the floor is
        # visible but doesn't inflate the number that answers "would the
        # cron actually promote something".
        cron_promotable = sum(market_promotable[m] for m in LESSON_MARKETS)
        all_promotable = sum(market_promotable.values())

        evidence_range = describe_range([ts for _q, _l, ts in rows])
        lesson_age_range = describe_range([lesson.as_of_date for lesson in lessons])

        print(f"discussions considered: {len(rows)} "
              f"(qualifying: {sum(1 for q, _l, _t in rows if q)})")
        print(f"evidence window (retained discussions seen): {evidence_range}")
        print(f"lesson age range (as_of_date): {lesson_age_range}")
        warning = overlap_warning(
            evidence_range=evidence_range, lesson_age_range=lesson_age_range,
        )
        if warning:
            print(warning)
        print(f"lessons: {len(lessons)}, "
              f"hit_count raised: {raised}, lowered: {lowered} (of which "
              f"zeroed: {zeroed})")
        archive_delta = recomputed_zero_hit - current_zero_hit
        print(f"archive-eligible (usage>0 & hit_count==0): "
              f"current={current_zero_hit}, "
              f"after recompute={recomputed_zero_hit} ({archive_delta:+d}) "
              f"— archive_stale_lessons picks these up on the next "
              f"backtest sweep")
        print("by tier: " + ", ".join(
            f"{tier}={count}" for tier, count in sorted(tier_counts.items())
        ))
        print("WOULD-PROMOTE by market: " + (", ".join(
            f"{market}={count}"
            for market, count in sorted(market_promotable.items())
        ) or "(none)"))
        print(f"WOULD-PROMOTE ({', '.join(LESSON_MARKETS)}, "
              f"= Sunday cron): {cron_promotable}")
        print(f"WOULD-PROMOTE (all markets, secondary): {all_promotable}")
        if not apply:
            print("dry-run — pass --apply to write")
            return
        if lowered and not allow_lower:
            print(f"refusing to lower {lowered} lesson(s)' hit_count "
                  f"(pass --allow-lower to override) — writing {raised} "
                  f"raise(s) only")
        for lesson in lessons:
            new_hits = hits.get(lesson.id, 0)
            old_hits = lesson.hit_count or 0
            if new_hits == old_hits:
                continue
            if new_hits < old_hits and not allow_lower:
                continue
            await db.execute(
                update(DiscussionLesson)
                .where(DiscussionLesson.id == lesson.id)
                .values(hit_count=new_hits)
                .execution_options(synchronize_session=False)
            )
        await db.commit()
        print("applied")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument(
        "--allow-lower", action="store_true",
        help="also write lowered hit_count values (refused by default; "
             "see module docstring on the retention-window era mismatch)",
    )
    args = ap.parse_args()
    asyncio.run(main(args.apply, args.allow_lower))
