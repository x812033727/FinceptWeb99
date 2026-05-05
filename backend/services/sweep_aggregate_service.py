"""Sweep / strategy aggregation (PR-B).

Reads the spawned discussions of a sweep (or all sweeps that
share a strategy template) and folds them into a single KPI
payload the dashboard renders:

  - dates_total / completed / failed counts
  - verdict counts + win_rate (win / (win + loss))
  - avg_pnl_d1 .. avg_pnl_d5 — averaged across recommended
    symbols of every spawned discussion that has resolved
    daily_close_prices
  - per_persona stats — for each persona in the sweep's roster:
    discussions_count, win_count, hit_rate, agree/dissent turn
    counts. PR-C reads `hit_rate` to compute learned weights.
  - lessons — recent unique post-mortem takeaways from
    `discussion_lessons` filtered by owner + market + the
    sweep's date window.

Empty-state semantics: a sweep with no completed discussions
returns the same shape with zero counts so the frontend can
render a "still warming up" placeholder without branching.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date as _date
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.backtest_sweep import BacktestSweep
from models.discussion import Discussion
from models.discussion import DiscussionTurn
from models.discussion_lesson import DiscussionLesson


WINDOW_DAYS = 5
LESSONS_LIMIT = 20


async def aggregate_sweep(
    db: AsyncSession,
    sweep: BacktestSweep,
) -> dict[str, Any]:
    """Aggregate a single sweep's spawned discussions."""
    discs = await _fetch_sweep_discussions(db, sweep.id)
    payload = _aggregate_discussions(
        discussions=discs,
        roster=list(sweep.persona_ids or []),
    )
    payload.update({
        "scope": "sweep",
        "sweep_id": str(sweep.id),
        "strategy_id": (
            str(sweep.strategy_id) if sweep.strategy_id else None
        ),
        "anchor_date": sweep.anchor_date.isoformat(),
        "trading_days_count": sweep.trading_days_count,
        "completed_count": len(sweep.completed_dates or []),
        "failed_count": len(sweep.failed_dates or []),
        # PR-A0: walk-forward fold metadata so the aggregate UI can
        # render the train / test badge and link back to the
        # sibling fold for side-by-side KPIs.
        "fold_kind": getattr(sweep, "fold_kind", "production"),
        "parent_sweep_id": (
            str(sweep.parent_sweep_id)
            if getattr(sweep, "parent_sweep_id", None) else None
        ),
    })
    payload["lessons"] = await _recent_lessons(
        db,
        owner_id=sweep.owner_id,
        market=sweep.market,
        date_range=_date_range_from_sweep(sweep),
    )
    payload["per_persona"] = await _augment_persona_turns(
        db, payload["per_persona"], [d.id for d in discs],
    )
    return payload


async def aggregate_strategy(
    db: AsyncSession,
    *,
    owner_id: UUID,
    strategy_id: UUID,
) -> dict[str, Any]:
    """Aggregate every sweep that referenced this template, then
    union their child discussions.

    Soft-deleted templates are still aggregatable (PR-B's dashboard
    surfaces them so a deleted strategy's historical performance
    stays visible)."""
    sweeps_q = select(BacktestSweep).where(
        BacktestSweep.owner_id == owner_id,
        BacktestSweep.strategy_id == strategy_id,
    )
    sweeps = list((await db.scalars(sweeps_q)).all())
    if not sweeps:
        return _empty_payload(
            scope="strategy",
            strategy_id=str(strategy_id),
            sweep_count=0,
        )

    # Pull every spawned discussion for any of the matching sweeps
    # in a single round-trip.
    sweep_ids = [s.id for s in sweeps]
    disc_q = (
        select(Discussion)
        .where(Discussion.sweep_id.in_(sweep_ids))
        .order_by(Discussion.created_at)
    )
    discs = list((await db.scalars(disc_q)).all())

    # Roster := union of all persona_ids across the constituent
    # sweeps. PR-C feeds this into weight learning so a roster
    # change between sweeps still gets per-persona credit.
    roster: list[str] = []
    seen: set[str] = set()
    for s in sweeps:
        for pid in (s.persona_ids or []):
            if pid not in seen:
                seen.add(pid)
                roster.append(pid)

    payload = _aggregate_discussions(
        discussions=discs, roster=roster,
    )
    payload.update({
        "scope": "strategy",
        "strategy_id": str(strategy_id),
        "sweep_count": len(sweeps),
    })

    # Lesson aggregation across all sweep dates.
    union_range: tuple[_date | None, _date | None] = (None, None)
    for s in sweeps:
        sr = _date_range_from_sweep(s)
        if sr[0] is None:
            continue
        lo = sr[0] if union_range[0] is None else min(union_range[0], sr[0])
        hi = sr[1] if union_range[1] is None else max(union_range[1], sr[1])
        union_range = (lo, hi)
    market = sweeps[0].market
    payload["lessons"] = await _recent_lessons(
        db, owner_id=owner_id, market=market, date_range=union_range,
    )
    payload["per_persona"] = await _augment_persona_turns(
        db, payload["per_persona"], [d.id for d in discs],
    )
    return payload


# ── internals ────────────────────────────────────────────────────


def _empty_payload(**extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "discussions_total": 0,
        "verdict_counts": {
            "win": 0, "loss": 0, "unverifiable": 0, "pending": 0,
        },
        "win_rate": None,
        "avg_pnl_pct": [None] * WINDOW_DAYS,
        "per_persona": [],
        "lessons": [],
    }
    base.update(extra)
    return base


async def _fetch_sweep_discussions(
    db: AsyncSession, sweep_id: UUID,
) -> list[Discussion]:
    stmt = (
        select(Discussion)
        .where(Discussion.sweep_id == sweep_id)
        .order_by(Discussion.created_at)
    )
    return list((await db.scalars(stmt)).all())


def _aggregate_discussions(
    *,
    discussions: list[Discussion],
    roster: list[str],
) -> dict[str, Any]:
    """Pure fold over a list of Discussion rows. No I/O.

    Returns the verdict / pnl / per-persona shell — caller layers
    in lessons + persona turn counts (separate queries).
    """
    if not discussions:
        empty = _empty_payload()
        # Still emit per-persona shells so the dashboard can render
        # the roster at zero stats instead of an empty placeholder
        # the operator can't tell apart from "no roster configured".
        empty["per_persona"] = [
            {
                "persona_id": pid,
                "discussions_count": 0,
                "win_count": 0,
                "hit_rate": None,
                "agree_turn_count": 0,
                "dissent_turn_count": 0,
            }
            for pid in roster
        ]
        return empty

    verdict_ctr: Counter[str] = Counter()
    # `pnl_sum[day]` accumulates across every (discussion, symbol)
    # pair that has a resolved close on that day. `pnl_n[day]`
    # counts the contributing samples.
    pnl_sum = [0.0] * WINDOW_DAYS
    pnl_n = [0] * WINDOW_DAYS

    # Per-persona aggregates. The roster is sweep-defined; we do
    # NOT auto-discover personas off the discussions because a
    # roster mid-sweep edit (currently impossible in API but cheap
    # to defend) would otherwise drop the originals.
    persona_disc_count: defaultdict[str, int] = defaultdict(int)
    persona_win_count: defaultdict[str, int] = defaultdict(int)

    for d in discussions:
        verdict = d.verdict or "pending"
        verdict_ctr[verdict] += 1

        # Per-symbol D1-D5 from daily_close_prices + day1_open_prices.
        opens = d.day1_open_prices or {}
        daily = d.daily_close_prices or {}
        for sym, closes in daily.items():
            base_open = opens.get(sym)
            if base_open is None or base_open == 0:
                continue
            for i, c in enumerate(closes[:WINDOW_DAYS]):
                if c is None:
                    continue
                pnl_sum[i] += (c - base_open) / base_open
                pnl_n[i] += 1

        # Persona attribution (in-roster only).
        roster_set = set(roster)
        for pid in (d.persona_ids or []):
            if pid not in roster_set:
                continue
            persona_disc_count[pid] += 1
            if verdict == "win":
                persona_win_count[pid] += 1

    avg_pnl: list[float | None] = []
    for i in range(WINDOW_DAYS):
        if pnl_n[i] == 0:
            avg_pnl.append(None)
        else:
            avg_pnl.append(pnl_sum[i] / pnl_n[i])

    win = verdict_ctr.get("win", 0)
    loss = verdict_ctr.get("loss", 0)
    win_rate: float | None
    if win + loss == 0:
        win_rate = None
    else:
        win_rate = win / (win + loss)

    per_persona: list[dict[str, Any]] = []
    for pid in roster:
        n = persona_disc_count.get(pid, 0)
        w = persona_win_count.get(pid, 0)
        per_persona.append({
            "persona_id": pid,
            "discussions_count": n,
            "win_count": w,
            "hit_rate": (w / n) if n else None,
            # filled by _augment_persona_turns
            "agree_turn_count": 0,
            "dissent_turn_count": 0,
        })

    return {
        "discussions_total": len(discussions),
        "verdict_counts": {
            "win": win,
            "loss": loss,
            "unverifiable": verdict_ctr.get("unverifiable", 0),
            "pending": verdict_ctr.get("pending", 0),
        },
        "win_rate": win_rate,
        "avg_pnl_pct": avg_pnl,
        "per_persona": per_persona,
        "lessons": [],   # filled by caller
    }


async def _augment_persona_turns(
    db: AsyncSession,
    per_persona: list[dict[str, Any]],
    discussion_ids: list[UUID],
) -> list[dict[str, Any]]:
    """Fill agree/dissent turn counts via a single GROUP BY query
    over `discussion_turns`. Cheap because the per-sweep discussion
    set is bounded (60 max) and each has ~roster × rounds turns."""
    if not per_persona or not discussion_ids:
        return per_persona

    stmt = (
        select(
            DiscussionTurn.persona_id,
            DiscussionTurn.stance,
            func.count().label("n"),
        )
        .where(DiscussionTurn.discussion_id.in_(discussion_ids))
        .group_by(DiscussionTurn.persona_id, DiscussionTurn.stance)
    )
    rows = (await db.execute(stmt)).all()
    by_persona: defaultdict[str, dict[str, int]] = defaultdict(
        lambda: {"agree": 0, "dissent": 0, "supplement": 0},
    )
    for pid, stance, n in rows:
        if stance in ("agree", "dissent", "supplement"):
            by_persona[pid][stance] = int(n)

    for entry in per_persona:
        s = by_persona.get(entry["persona_id"], {})
        entry["agree_turn_count"] = s.get("agree", 0) + s.get("supplement", 0)
        entry["dissent_turn_count"] = s.get("dissent", 0)
    return per_persona


def _date_range_from_sweep(
    sweep: BacktestSweep,
) -> tuple[_date | None, _date | None]:
    """Return (min, max) of resolved or completed dates so the
    lesson query window matches what actually got run, not what was
    requested. Falls back to the anchor date when nothing has
    resolved yet."""
    iso_dates: list[str] = list(sweep.resolved_dates or []) + list(
        sweep.completed_dates or [],
    )
    parsed: list[_date] = []
    for s in iso_dates:
        try:
            parsed.append(_date.fromisoformat(s))
        except ValueError:
            continue
    if not parsed:
        if sweep.anchor_date:
            return (sweep.anchor_date, sweep.anchor_date)
        return (None, None)
    return (min(parsed), max(parsed))


async def _recent_lessons(
    db: AsyncSession,
    *,
    owner_id: UUID,
    market: str,
    date_range: tuple[_date | None, _date | None],
) -> list[dict[str, Any]]:
    if date_range[0] is None or date_range[1] is None:
        return []
    stmt = (
        select(
            DiscussionLesson.category,
            DiscussionLesson.lesson_text,
            DiscussionLesson.as_of_date,
            DiscussionLesson.related_symbols,
            DiscussionLesson.created_at,
        )
        .where(
            DiscussionLesson.owner_user_id == owner_id,
            DiscussionLesson.market == market,
            DiscussionLesson.as_of_date >= date_range[0],
            DiscussionLesson.as_of_date <= date_range[1],
        )
        .order_by(DiscussionLesson.created_at.desc())
        .limit(LESSONS_LIMIT)
    )
    rows = (await db.execute(stmt)).all()
    return [
        {
            "category": cat,
            "lesson_text": text,
            "as_of_date": d.isoformat(),
            "related_symbols": list(syms or []),
            "created_at": ts.isoformat() if ts else None,
        }
        for (cat, text, d, syms, ts) in rows
    ]


__all__ = [
    "WINDOW_DAYS", "LESSONS_LIMIT",
    "aggregate_sweep", "aggregate_strategy",
]
