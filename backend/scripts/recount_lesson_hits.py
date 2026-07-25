"""Retroactive lesson-hit recount under the corrected semantics.

Spec Part 2 requires the PR to answer: how many of the existing
episodic lessons WOULD cross the promotion floor once correct
abstentions count? A zero here means the semantics change alone is
insufficient and the threshold discussion starts now, not in two weeks.

From-scratch recount (idempotent), dry-run by default; --apply
overwrites hit_count. Never promotes — the Sunday job owns that.

--apply can LOWER hit_count, not just raise it: rows inflated by
historical double-bump bugs recount downward once this script's
from-scratch aggregation replaces whatever incremental count is
currently stored. DEMOTE_HIT_RATE_THRESHOLD (lesson_tier_service.py)
reads hit_count too, so a lowered count can push a lesson toward
demotion on its next cron pass — read the dry-run's raised/lowered
split below before passing --apply.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter

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


async def _collect(db) -> list[tuple[bool, set[int]]]:
    discussions = (await db.scalars(
        select(Discussion).where(Discussion.verdict.is_not(None))
    )).all()
    rows: list[tuple[bool, set[int]]] = []
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
        rows.append((qualifies, _lesson_ids_from_snapshots(snaps)))
    return rows


async def main(apply: bool) -> None:
    from db.session import AsyncSessionLocal
    from tasks.promote_lessons import LESSON_MARKETS

    async with AsyncSessionLocal() as db:
        rows = await _collect(db)
        hits = aggregate_hits(rows)
        lessons = (await db.scalars(select(DiscussionLesson))).all()
        raised = 0
        lowered = 0
        tier_counts: Counter[str] = Counter()
        market_promotable: Counter[str] = Counter()
        for lesson in lessons:
            tier_counts[lesson.tier] += 1
            new_hits = hits.get(lesson.id, 0)
            old_hits = lesson.hit_count or 0
            if new_hits > old_hits:
                raised += 1
            elif new_hits < old_hits:
                lowered += 1
            # Mirror promote_eligible_lessons' own filter (lesson_tier_
            # service.py): only "episodic" lessons are promotion
            # candidates — semantic/structural rows are already past
            # that gate and never re-enter it.
            if lesson.tier == "episodic" and would_promote(
                usage=lesson.usage_count or 0, hits=new_hits,
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

        print(f"discussions considered: {len(rows)} "
              f"(qualifying: {sum(1 for q, _ in rows if q)})")
        print(f"lessons: {len(lessons)}, "
              f"hit_count raised: {raised}, lowered: {lowered}")
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
        for lesson in lessons:
            await db.execute(
                update(DiscussionLesson)
                .where(DiscussionLesson.id == lesson.id)
                .values(hit_count=hits.get(lesson.id, 0))
                .execution_options(synchronize_session=False)
            )
        await db.commit()
        print("applied")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    asyncio.run(main(args.apply))
