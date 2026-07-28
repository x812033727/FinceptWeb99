"""Tier-threshold calibration sweep over graded experiment picks.

Read-only operator script: sweeps candidate ``T_CONSENSUS`` values over
the graded experiment sessions (``auto_run_sequence >= 900``) and prints
per-threshold recommend-tier size, win rate and mean D5 excess vs the
TAIEX total-return benchmark. The output lands in the PR body; the
winning threshold ships as ``services.daily_pick_tier.T_CONSENSUS``.

Selection rule (spec: confidence-tiering): keep tier win rate >= 0.8
with >= 1/3 of picks in the recommend tier; ties break toward the
larger tier. If nothing clears the bar, the best available row is
reported honestly — the shipped constant is then a fallback, not a
conclusion.

Usage (read-only):
    python -m scripts.calibrate_pick_tier
"""
from __future__ import annotations

import asyncio
from typing import Any

from services.daily_pick_tier import T_HALLUC

THRESHOLDS = [0.75, 0.80, 0.85, 0.90, 0.95]


def sweep(picks: list[dict[str, Any]], thresholds: list[float]) -> list[dict[str, Any]]:
    """Pure partition of graded picks per candidate consensus threshold.

    Each pick: ``{"consensus", "warnings", "excess_pp", "win"}``. The
    hallucination gate (``warnings <= T_HALLUC``) applies inside the
    sweep — a warning-heavy pick never reaches the recommend tier no
    matter the threshold, mirroring ``tier_for``.
    """
    rows: list[dict[str, Any]] = []
    for threshold in thresholds:
        recommend = [
            p for p in picks
            if p["consensus"] >= threshold and p["warnings"] <= T_HALLUC
        ]
        watch = [p for p in picks if p not in recommend]
        rows.append({
            "threshold": threshold,
            "n_recommend": len(recommend),
            "recommend_win_rate": _rate(recommend),
            "recommend_mean_excess": _mean(recommend),
            "n_watch": len(watch),
            "watch_win_rate": _rate(watch),
        })
    return rows


def _rate(picks: list[dict[str, Any]]) -> float | None:
    if not picks:
        return None
    return round(sum(1 for p in picks if p["win"]) / len(picks), 2)


def _mean(picks: list[dict[str, Any]]) -> float | None:
    if not picks:
        return None
    return round(sum(p["excess_pp"] for p in picks) / len(picks), 2)


def select_threshold(rows: list[dict[str, Any]], n_total: int) -> dict[str, Any] | None:
    """Spec selection rule; None when nothing clears the bar."""
    qualifying = [
        r for r in rows
        if r["recommend_win_rate"] is not None
        and r["recommend_win_rate"] >= 0.8
        and r["n_recommend"] * 3 >= n_total
    ]
    if not qualifying:
        return None
    # Ties break toward the larger tier: max coverage, then the lower
    # threshold (same coverage at a lower bar = identical tier anyway).
    return max(qualifying, key=lambda r: (r["n_recommend"], -r["threshold"]))


async def main() -> None:
    # DB imports stay inside main() so importing the module never builds
    # the async engine (subprocess-verified in the test file).
    from sqlalchemy import select

    from db.session import AsyncSessionLocal
    from models.discussion import Discussion
    from services.daily_scoreboard_service import (
        _anchor_date,
        _benchmark_return_pct,
        _load_benchmark_series,
        _picked_return_pct,
    )

    async with AsyncSessionLocal() as db:
        discussions = (
            await db.scalars(
                select(Discussion).where(
                    Discussion.auto_run.is_(True),
                    Discussion.auto_run_sequence >= 900,
                    Discussion.status == "done",
                    Discussion.conclusion.is_not(None),
                ).order_by(Discussion.created_at)
            )
        ).all()
        benchmark = await _load_benchmark_series(db)

    picks: list[dict[str, Any]] = []
    skipped = 0
    for d in discussions:
        conclusion = d.conclusion or {}
        if not (conclusion.get("recommended_symbols") or []):
            continue  # abstention — nothing to tier
        consensus = conclusion.get("consensus_score")
        if not isinstance(consensus, (int, float)):
            skipped += 1
            continue
        quality = conclusion.get("quality_signals")
        warnings = (
            quality.get("hallucination_warnings")
            if isinstance(quality, dict) else None
        )
        picked = _picked_return_pct(d)
        index = _benchmark_return_pct(benchmark, _anchor_date(d))
        if picked is None or index is None:
            skipped += 1  # ungraded / immature window — honest exclusion
            continue
        excess = picked - index
        picks.append({
            "consensus": float(consensus),
            "warnings": len(warnings) if isinstance(warnings, list) else T_HALLUC + 1,
            "excess_pp": excess,
            "win": excess > 0,
        })

    print(f"graded experiment picks: {len(picks)} (skipped {skipped}: "
          "no consensus / window not mature)")
    rows = sweep(picks, THRESHOLDS)
    header = (f"{'T_cons':>7} {'n_rec':>6} {'rec_win':>8} "
              f"{'rec_excess':>11} {'n_watch':>8} {'watch_win':>10}")
    print(header)
    for r in rows:
        print(f"{r['threshold']:>7.2f} {r['n_recommend']:>6d} "
              f"{_fmt(r['recommend_win_rate']):>8} "
              f"{_fmt(r['recommend_mean_excess']):>11} "
              f"{r['n_watch']:>8d} {_fmt(r['watch_win_rate']):>10}")
    winner = select_threshold(rows, len(picks))
    if winner is None:
        print("selection rule: NO threshold reaches win_rate>=0.8 with >=1/3 "
              "coverage — ship the best available and record the honest number.")
    else:
        print(f"selection rule verdict: T_CONSENSUS = {winner['threshold']:.2f} "
              f"(n_recommend={winner['n_recommend']}, "
              f"win_rate={winner['recommend_win_rate']})")


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


if __name__ == "__main__":
    asyncio.run(main())
