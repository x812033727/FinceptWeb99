"""Backfill `fundamentals_snapshots` for past sessions.

`ingest_fundamentals_tw` stamps one row per symbol per day using
TWSE's `BWIBBU_ALL`, which is a today-only cross-section with no date
parameter. So the table only ever had history from the day the job was
switched on — on the live deployment that meant **ten days**, all of
2026-07-11 onward.

That is invisible day to day and fatal to any historical replay. The
daily screener reads PE from this table (`load_candidate_rows`) and
`chip_quality` requires `0 < pe <= 30`, so reconstructing a candidate
pool for any session before the table starts yields *zero* candidates
for that strategy — not an error, just an empty pool that looks like
"quiet day". A backtest built on that would be measuring a strategy
that never ran.

Two sources, matching what the daily job assembles:

  - **valuation ratios** — the legacy dated report `BWIBBU_d`, which
    does take a date and serves arbitrary past sessions (verified back
    to 2026-04). No FinMind quota involved.
  - **statement-derived payload** (roe / operating_cash_flow /
    ocf_positive_quarters / debt_ratio) — FinMind market-wide
    statements for the six quarters ending on/before the target day,
    reusing `ingest_fundamentals_tw._load_statement_payloads`. These
    only change at quarter boundaries, so the fetch is cached per
    quarter-set: a three-month window costs ~2 sets x 18 calls rather
    than 18 per day.

Usage::

    # Preview a 90-day window — no writes, reports what is missing
    python -m scripts.backfill_fundamentals_history --days 90 --dry-run

    # Real run
    python -m scripts.backfill_fundamentals_history --days 90

    # Explicit window
    python -m scripts.backfill_fundamentals_history \
        --start 2026-04-20 --end 2026-07-11

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
from models.fundamentals_snapshot import FundamentalsSnapshot
from services.ingest.repository import (
    FundamentalsSnapshotRow,
    upsert_fundamentals_snapshots,
)
from tasks.ingest_fundamentals_tw import (
    _STATEMENT_QUARTERS,
    _load_statement_payloads,
    _recent_quarter_ends,
)

log = logging.getLogger(__name__)

# TWSE serves these reports without a documented rate limit, but a
# 90-day walk is ~65 requests; pace them.
_PACING_SECONDS = 1.5


async def _archived_sessions(start: date, end: date) -> set[date]:
    async with AsyncSessionLocal() as db:
        rows = await db.scalars(
            select(FundamentalsSnapshot.as_of)
            .where(
                FundamentalsSnapshot.market == "TW",
                FundamentalsSnapshot.as_of >= start,
                FundamentalsSnapshot.as_of <= end,
            )
            .distinct()
        )
        return {r if isinstance(r, date) else r.date() for r in rows.all()}


async def _payloads_for(day: date, cache: dict) -> dict:
    """Statement payloads as they stood on `day`.

    Keyed by the quarter set rather than the day: statements change
    four times a year, so every day inside a quarter shares an answer
    and re-fetching per day would burn FinMind quota for identical
    results.
    """
    key = tuple(_recent_quarter_ends(day, _STATEMENT_QUARTERS))
    if key in cache:
        return cache[key]
    try:
        payloads = await _load_statement_payloads(as_of=day)
    except Exception as exc:  # noqa: BLE001 — reported, never fatal
        log.warning(
            "backfill_fundamentals.statements_failed",
            extra={"day": day.isoformat(), "error": str(exc)},
        )
        payloads = {}
    cache[key] = payloads
    log.info(
        "backfill_fundamentals.statements_loaded",
        extra={
            "quarters": [q.isoformat() for q in key],
            "symbols": len(payloads),
        },
    )
    return payloads


async def backfill(
    start: date, end: date, *, dry_run: bool, force: bool,
) -> dict[str, int]:
    have = set() if force else await _archived_sessions(start, end)
    quarter_cache: dict = {}
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
            ratios = await twse.get_all_valuation_ratios(day)
        except Exception as exc:  # noqa: BLE001
            stats["failed"] += 1
            log.warning(
                "backfill_fundamentals.day_failed",
                extra={"day": day.isoformat(), "error": str(exc)},
            )
            day += timedelta(days=1)
            continue
        if not ratios:
            stats["non_trading"] += 1
            day += timedelta(days=1)
            continue

        payloads = await _payloads_for(day, quarter_cache)
        rows = [
            FundamentalsSnapshotRow(
                market="TW",
                symbol=symbol,
                as_of=day,
                pe_ratio=v.get("pe_ratio"),
                pb_ratio=v.get("pb_ratio"),
                dividend_yield=v.get("dividend_yield"),
                eps=None,
                revenue=None,
                payload=payloads.get(symbol),
                source="twse-bwibbu-d",
            )
            for symbol, v in ratios.items()
            if symbol
        ]
        if dry_run:
            print(f"{day}  would write {len(rows)} rows "
                  f"({sum(1 for r in rows if r.payload)} with statement payload)")
        else:
            async with AsyncSessionLocal() as db:
                written = await upsert_fundamentals_snapshots(db, rows)
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

    print(f"backfilling fundamentals {start} .. {end}"
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
