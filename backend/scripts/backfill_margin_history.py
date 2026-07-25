"""Backfill `tw_margin_daily` for past sessions.

`ingest_margin_tw` reads TWSE's OpenAPI `MI_MARGN`, a today-only
snapshot with no date parameter. So the table only ever had history
from the day the job was switched on — on the live deployment that
meant **eleven days**, all of 2026-07-12 onward.

That is invisible day to day and quietly degrades historical replays:
`focus_briefs` shows `margin_latest` to every persona in live mode,
and a replayed session anchored before 07-12 simply got a null there.
The panel then reasons about leverage without the leverage figure.

Source: the legacy `www.twse.com.tw` MI_MARGN report, which does take
a date and serves arbitrary past sessions. Safe to read historically —
the margin ledger is published daily and never restated, the same
property that makes the institutional archive replay-safe (and that
`tw_revenue_monthly.revenue_yoy` lacks, which is why that one stays
masked in backtest mode).

Usage::

    # Preview a 90-day window — no writes, reports what is missing
    python -m scripts.backfill_margin_history --days 90 --dry-run

    # Real run
    python -m scripts.backfill_margin_history --days 90

    # Explicit window
    python -m scripts.backfill_margin_history \
        --start 2026-04-01 --end 2026-07-11

Idempotent: sessions already present are skipped unless ``--force``.
Non-trading days answer with no rows and are skipped without a write,
so weekends and holidays cost one cheap request each and nothing else.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date, timedelta

from sqlalchemy import select

import data.tw.twse_connector as twse
from db.session import AsyncSessionLocal
from models.tw_chip_metrics import TwMarginDaily
from services.ingest.repository import MarginDailyRow, upsert_margin_daily

log = logging.getLogger(__name__)

# TWSE serves this report without a documented rate limit, but a
# 90-day walk is ~65 requests; pace them.
_PACING_SECONDS = 1.5


async def _archived_sessions(start: date, end: date) -> set[date]:
    async with AsyncSessionLocal() as db:
        rows = await db.scalars(
            select(TwMarginDaily.ts)
            .where(
                TwMarginDaily.market == "TW",
                TwMarginDaily.ts >= start,
                TwMarginDaily.ts <= end,
            )
            .distinct()
        )
        return {r if isinstance(r, date) else r.date() for r in rows.all()}


async def backfill(
    start: date, end: date, *, dry_run: bool, force: bool,
) -> dict[str, int]:
    have = set() if force else await _archived_sessions(start, end)
    stats = {
        "requested": 0, "skipped_present": 0, "non_trading": 0,
        "written_sessions": 0, "written_rows": 0, "failed": 0,
    }

    day = start
    first = True
    while day <= end:
        if day.weekday() >= 5:
            day += timedelta(days=1)
            continue
        stats["requested"] += 1
        if day in have:
            stats["skipped_present"] += 1
            day += timedelta(days=1)
            continue
        if not first:
            await asyncio.sleep(_PACING_SECONDS)
        first = False
        try:
            quotes = await twse.get_margin(day)
        except Exception as exc:  # noqa: BLE001 — reported, never fatal
            stats["failed"] += 1
            log.warning(
                "backfill_margin.day_failed",
                extra={"day": day.isoformat(), "error": str(exc)},
            )
            day += timedelta(days=1)
            continue
        if not quotes:
            stats["non_trading"] += 1
            day += timedelta(days=1)
            continue

        rows = [
            MarginDailyRow(
                market="TW",
                symbol=q["symbol"],
                ts=day,
                margin_purchase=q.get("margin_purchase"),
                margin_balance=q.get("margin_balance"),
                short_sale=q.get("short_sale"),
                short_balance=q.get("short_balance"),
                source="twse-mi-margn-d",
            )
            for q in quotes
            if q.get("symbol")
        ]
        if dry_run:
            print(f"{day}  would write {len(rows)} rows")
        else:
            async with AsyncSessionLocal() as db:
                written = await upsert_margin_daily(db, rows)
            stats["written_rows"] += written
            print(f"{day}  wrote {written} rows")
        stats["written_sessions"] += 1
        day += timedelta(days=1)

    return stats


def _parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=None,
                    help="calendar days back from today (mutually exclusive with --start)")
    ap.add_argument("--start", type=date.fromisoformat, default=None)
    ap.add_argument("--end", type=date.fromisoformat, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="re-fetch sessions already present")
    args = ap.parse_args(argv)
    if args.days is None and args.start is None:
        ap.error("one of --days or --start is required")
    return args


async def _main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parse_args(argv)
    end = args.end or date.today()
    start = args.start or (end - timedelta(days=args.days))
    if start > end:
        print("start must be on or before end", file=sys.stderr)
        return 2

    print(f"backfilling margin {start} .. {end}"
          f"{' (dry run)' if args.dry_run else ''}")
    stats = await backfill(start, end, dry_run=args.dry_run, force=args.force)
    print("\n".join(f"{k}: {v}" for k, v in stats.items()))
    # A run where every requested session failed is an outage, not a
    # quiet window — exit non-zero so a caller can tell.
    if stats["failed"] and not stats["written_sessions"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(sys.argv[1:])))
