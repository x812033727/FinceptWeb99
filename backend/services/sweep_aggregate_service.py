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
from datetime import UTC, date as _date, datetime, timedelta
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
        "brier_score": None,
        "brier_samples": 0,
        "calibrated_brier_score": None,
        "calibrated_brier_samples": 0,
        "reliability": [],
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

    # PR-C1: Brier accumulators. `brier_sum_loss` is the sum of
    # squared errors weighted by per-discussion sample count;
    # dividing by `brier_total_samples` at the end yields the
    # population mean across every (recommendation, outcome) pair
    # in the sweep — equivalent to letting one discussion with 5
    # picks count for 5x as much as a discussion with 1 pick.
    # Reliability pairs hold every (confidence, outcome_binary)
    # tuple for `compute_reliability_buckets`.
    brier_sum_loss = 0.0
    brier_total_samples = 0
    reliability_pairs: list[tuple[float, int]] = []
    # PR-C2 follow-up: parallel accumulator for calibrated_brier.
    # Only contributes when a discussion has BOTH brier_score and
    # calibrated_brier_score set; partial coverage would mix raw
    # + calibrated entries and make the comparison meaningless.
    calibrated_brier_sum_loss = 0.0
    calibrated_brier_total_samples = 0

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

        # PR-C1: roll up the Brier loss + reliability pairs from each
        # resolved discussion. Pre-PR-C0 / unresolved discussions
        # have NULL brier_score and are skipped silently.
        d_brier = getattr(d, "brier_score", None)
        d_outcomes = getattr(d, "outcome_vector", None) or []
        if d_brier is not None and isinstance(d_outcomes, list) and d_outcomes:
            samples = len(d_outcomes)
            brier_sum_loss += d_brier * samples
            brier_total_samples += samples
            for entry in d_outcomes:
                if not isinstance(entry, dict):
                    continue
                try:
                    conf = float(entry.get("confidence"))
                    outcome = int(entry.get("outcome_binary"))
                except (TypeError, ValueError):
                    continue
                if outcome not in (0, 1):
                    continue
                reliability_pairs.append((conf, outcome))

        # PR-C2 follow-up: parallel calibrated-Brier roll-up. Only
        # accumulates when this discussion's calibration coverage
        # was complete (the per-discussion fitter set
        # calibrated_brier_score = None when partial).
        d_cal_brier = getattr(d, "calibrated_brier_score", None)
        if (
            d_cal_brier is not None
            and isinstance(d_outcomes, list) and d_outcomes
        ):
            samples = len(d_outcomes)
            calibrated_brier_sum_loss += d_cal_brier * samples
            calibrated_brier_total_samples += samples

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

    # PR-C1: emit the rolled-up Brier + reliability diagram alongside
    # the existing KPIs. NULL when no resolved discussion contributed
    # — the dashboard renders that as "calibration data not yet
    # available" instead of misleading 0.0.
    from services.discussion_scoreboard_service import (
        compute_reliability_buckets,
    )
    if brier_total_samples > 0:
        avg_brier: float | None = round(
            brier_sum_loss / brier_total_samples, 6,
        )
        reliability = compute_reliability_buckets(reliability_pairs)
    else:
        avg_brier = None
        reliability = []

    avg_calibrated_brier: float | None
    if calibrated_brier_total_samples > 0:
        avg_calibrated_brier = round(
            calibrated_brier_sum_loss / calibrated_brier_total_samples, 6,
        )
    else:
        avg_calibrated_brier = None

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
        "brier_score": avg_brier,
        "brier_samples": brier_total_samples,
        # PR-C2 follow-up: comparison axis for "is calibration
        # actually helping?". NULL when no discussion in the sweep
        # had a complete calibration coverage. Lower than
        # `brier_score` = the calibration curve is reducing error.
        "calibrated_brier_score": avg_calibrated_brier,
        "calibrated_brier_samples": calibrated_brier_total_samples,
        "reliability": reliability,
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


async def fetch_strategy_brier_history(
    db: AsyncSession,
    *,
    owner_id: UUID,
    strategy_id: UUID,
    window_days: int = 90,
) -> list[dict[str, Any]]:
    """Per-sweep Brier time-series for one strategy template.

    Aggregates `discussions.brier_score` and
    `discussions.calibrated_brier_score` per completed sweep,
    ordered by completion time ascending. Filtered to sweeps
    completed within the last `window_days` days so the
    frontend's trend chart doesn't drag in two-year-old early-
    days noise.

    Trend interpretation:
      - **raw_brier descending over time** = the strategy is
        getting better predictions on its own (probably from
        accumulated lessons + persona weight learning)
      - **calibrated_brier consistently below raw_brier** =
        the isotonic curve is reducing error (PR-C2 doing its
        job)
      - **calibrated_brier flat or rising** = curve is fitted
        on a different regime than the recent sweeps; consider
        a manual refit via POST /strategies/{id}/learn (which
        also re-triggers calibration fit) or wait for the
        rolling pool to refresh

    Returns: list of `{sweep_id, anchor_date, completed_at,
    fold_kind, raw_brier, calibrated_brier, samples}`. Sweeps
    that have no resolved discussions (brier_score IS NULL)
    are excluded — they'd be NULL points the chart can't
    plot anyway.

    Strategy-level scope (not sweep-level) — the consumer
    is the per-strategy trend card, which doesn't care about
    individual sweep details beyond the time + Brier values.
    """
    cutoff = datetime.now(UTC) - timedelta(days=window_days)
    stmt = (
        select(
            BacktestSweep.id,
            BacktestSweep.anchor_date,
            BacktestSweep.completed_at,
            BacktestSweep.fold_kind,
            func.avg(Discussion.brier_score).label("raw_brier"),
            func.avg(Discussion.calibrated_brier_score)
            .label("calibrated_brier"),
            func.count(Discussion.brier_score).label("samples"),
        )
        .join(Discussion, Discussion.sweep_id == BacktestSweep.id)
        .where(
            BacktestSweep.strategy_id == strategy_id,
            BacktestSweep.owner_id == owner_id,
            BacktestSweep.status == "completed",
            BacktestSweep.completed_at.is_not(None),
            BacktestSweep.completed_at >= cutoff,
            Discussion.brier_score.is_not(None),
        )
        .group_by(
            BacktestSweep.id,
            BacktestSweep.anchor_date,
            BacktestSweep.completed_at,
            BacktestSweep.fold_kind,
        )
        .order_by(BacktestSweep.completed_at.asc())
    )
    rows = (await db.execute(stmt)).all()
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append({
            "sweep_id": str(r.id),
            "anchor_date": (
                r.anchor_date.isoformat() if r.anchor_date else None
            ),
            "completed_at": (
                r.completed_at.isoformat() if r.completed_at else None
            ),
            "fold_kind": r.fold_kind,
            "raw_brier": (
                round(float(r.raw_brier), 6)
                if r.raw_brier is not None else None
            ),
            "calibrated_brier": (
                round(float(r.calibrated_brier), 6)
                if r.calibrated_brier is not None else None
            ),
            "samples": int(r.samples or 0),
        })
    return out


__all__ = [
    "WINDOW_DAYS", "LESSONS_LIMIT",
    "aggregate_sweep", "aggregate_strategy",
    "fetch_strategy_brier_history",
]
