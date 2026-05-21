"""One-shot Discussion.verdict backfill using the 4-band classifier.

The new `services.outcome_classifier` (大敗優先, close-price based)
landed in the 4-band cutover, but the existing `verify_discussion_
outcome` task only writes to rows with `verdict IS NULL`. Pre-cutover
rows still carry verdicts produced by the legacy verifier — that
older code graded against intraday HIGH + a 3 % bar, so it
frequently wrote `verdict="win"` to discussions whose closing prices
actually crashed (see screenshot scenario: 2337 D1-D5 all down with
trough −18 %, legacy verdict="win" because day-1 HIGH brushed +3 %).

This script walks every discussion, rebuilds `(d1_open, closes)` per
recommended symbol from `day1_open_prices` + `daily_close_prices`,
runs the new `classify_discussion`, and overwrites `verdict` /
`verdict_reason` whenever the rolled-up band changes. Result: sweep
win_rate, lesson hit_count, persona attribution all align with the
new rule.

Usage:
    cd backend && source .venv/bin/activate

    # Preview: list rows that would change, write nothing
    python -m scripts.backfill_verdicts --dry-run

    # Smaller spot check
    python -m scripts.backfill_verdicts --dry-run --limit 20

    # Real run (commits)
    python -m scripts.backfill_verdicts

Skipped rows (counted but not modified):
  - `daily_close_prices` empty (no closes to classify against)
  - `day1_open_prices` empty (no entry-price reference)
  - `verdict='unverifiable'` (synthesizer returned no symbols —
    nothing to classify, and stale-grace cases that genuinely have
    no data shouldn't get re-graded)
  - classifier returns None (every symbol's d1_open ≤ 0 or every
    close is None)

Idempotent: rows whose new band already matches the existing
`verdict` (and whose reason already starts with the classifier
template) are left untouched, so re-running is safe + cheap.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from sqlalchemy import select, update

from db.session import AsyncSessionLocal
from models.discussion import Discussion
from services.outcome_classifier import classify_discussion

log = logging.getLogger(__name__)


_WINDOW_DAYS = 5


def _format_reason(result, *, win_pct: float, big_win_pct: float, big_loss_pct: float) -> str:
    """Build a verdict_reason matching the verifier's style."""
    sym = result.winner_symbol or "?"
    cls = result.classifications.get(sym) if result.winner_symbol else None
    if cls is None:
        return f"backfill: band={result.band}"
    if result.band == "big_loss":
        return (
            f"{sym} trough close {cls.trough_pct:.2f}% "
            f"(≤ {big_loss_pct:g}% threshold) [backfilled]"
        )
    if result.band == "big_win":
        return (
            f"{sym} D5 close {cls.day5_pct:+.2f}% "
            f"(≥ {big_win_pct:g}% threshold) [backfilled]"
        )
    if result.band == "win":
        return (
            f"{sym} peak close {cls.peak_pct:+.2f}% "
            f"(≥ {win_pct:g}% threshold) [backfilled]"
        )
    return (
        f"{sym} peak {cls.peak_pct:+.2f}% / trough "
        f"{cls.trough_pct:+.2f}% — no band crossed [backfilled]"
    )


async def _run(args: argparse.Namespace) -> int:
    """Walk discussions table, reclassify, optionally commit. Returns the
    number of rows that would change (regardless of dry-run mode)."""
    from config import settings
    big_win_pct = settings.OUTCOME_BIG_WIN_DAY5_THRESHOLD_PCT
    win_pct = settings.OUTCOME_WIN_THRESHOLD_PCT
    big_loss_pct = settings.OUTCOME_BIG_LOSS_THRESHOLD_PCT

    stmt = select(Discussion).order_by(Discussion.created_at)
    if args.limit is not None:
        stmt = stmt.limit(args.limit)

    changed = 0
    skipped_no_data = 0
    skipped_unverifiable = 0
    skipped_unchanged = 0
    examined = 0

    async with AsyncSessionLocal() as db:
        rows = (await db.scalars(stmt)).all()
        for d in rows:
            examined += 1

            if d.verdict == "unverifiable":
                skipped_unverifiable += 1
                continue

            opens = dict(d.day1_open_prices or {})
            daily = dict(d.daily_close_prices or {})
            if not opens or not daily:
                skipped_no_data += 1
                continue

            per_symbol: dict[str, tuple[float, list[float | None]]] = {}
            for sym, open_price in opens.items():
                if open_price is None or open_price <= 0:
                    continue
                closes = daily.get(sym)
                if not isinstance(closes, list):
                    continue
                window_closes = list(closes[:_WINDOW_DAYS])
                # Pad to window length so D5 (= last slot) is unambiguous
                while len(window_closes) < _WINDOW_DAYS:
                    window_closes.append(None)
                per_symbol[sym] = (float(open_price), window_closes)

            if not per_symbol:
                skipped_no_data += 1
                continue

            result = classify_discussion(
                per_symbol=per_symbol,
                big_win_day5_pct=big_win_pct,
                win_pct=win_pct,
                big_loss_pct=big_loss_pct,
            )
            if result is None:
                skipped_no_data += 1
                continue

            new_verdict = result.band
            new_reason = _format_reason(
                result,
                win_pct=win_pct,
                big_win_pct=big_win_pct,
                big_loss_pct=big_loss_pct,
            )

            if d.verdict == new_verdict and (d.verdict_reason or "").endswith(
                "[backfilled]"
            ):
                skipped_unchanged += 1
                continue

            log.info(
                "backfill_verdicts.change",
                extra={
                    "discussion_id": str(d.id),
                    "before": d.verdict,
                    "after": new_verdict,
                    "winner_symbol": result.winner_symbol,
                },
            )
            changed += 1

            if not args.dry_run:
                await db.execute(
                    update(Discussion)
                    .where(Discussion.id == d.id)
                    .values(verdict=new_verdict, verdict_reason=new_reason)
                    .execution_options(synchronize_session=False)
                )

        if not args.dry_run:
            await db.commit()

    log.info(
        "backfill_verdicts.summary",
        extra={
            "examined": examined,
            "changed": changed,
            "skipped_no_data": skipped_no_data,
            "skipped_unverifiable": skipped_unverifiable,
            "skipped_unchanged": skipped_unchanged,
            "dry_run": args.dry_run,
        },
    )
    return changed


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--dry-run", action="store_true",
        help="Log expected changes without committing.",
    )
    p.add_argument(
        "--limit", type=int, default=None,
        help="Process at most N discussions (oldest first).",
    )
    p.add_argument(
        "--log-level", default="INFO",
        help="Logging level (DEBUG/INFO/WARNING/ERROR).",
    )
    args = p.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    changed = asyncio.run(_run(args))
    if args.dry_run:
        print(f"DRY-RUN: would change {changed} row(s). No DB writes.")
    else:
        print(f"Committed {changed} verdict update(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
