"""One-shot TW monthly-revenue backfill from FinMind paid history.

The daily `ingest_revenue_tw` cron only pulls a 90-day lookback window,
so backtest discussions anchored further back have to fall back to
nothing for `top_revenue_growers`. Worse, FinMind's market-wide
`TaiwanStockMonthRevenue` query was paywalled in 2026-04 — when that
happens the daily cron returns early without writing anything, so any
bogus values left over from a previous deploy (e.g. PR #211 fix-target
where `revenue_yoy` was the year integer like `2026`) sit there
indefinitely.

This script pulls the FinMind historical archive (~2017+) into
`tw_revenue_monthly` one month-first at a time, walking OLDEST → NEWEST
so each month's `_enrich_growth_rates` pass can compute YoY against the
prev-year month already written by an earlier iteration. Required: paid
FINMIND_TOKEN.

It asks only for first-of-month dates, and only ever one at a time —
this dataset's market-wide mode answers for `start_date` alone and
ignores `end_date`. See `_month_starts` for the probe that pinned it
down and for what the previous 30-day-chunk walk silently missed.

Run from inside the backend dir:

    export FINMIND_TOKEN=<paid-tier-token>

    # backfill the last 3 years (typical first run)
    python -m scripts.backfill_revenue_finmind --start 2023-01-01 --end 2026-04-30

    # extend further (FinMind starts ~2017)
    python -m scripts.backfill_revenue_finmind --start 2017-01-01 --end 2026-04-30

The upsert overwrites in place via the `(market, symbol, ts)` primary
key, so re-runs are idempotent — running the same range twice produces
the same final state, no duplicates. Older bogus rows whose
`revenue_yoy` was previously the year integer get overwritten with the
correctly-computed value (or NULL when the prior-year baseline still
isn't in the archive).
"""
import argparse
import asyncio
import logging
import sys
from datetime import date

import data.tw.finmind_connector as finmind
from db.session import AsyncSessionLocal
from services.ingest.repository import (
    RevenueMonthlyRow,
    upsert_revenue_monthly,
)
from tasks.ingest_revenue_tw import _enrich_growth_rates, _shift_month

log = logging.getLogger(__name__)

MARKET = "TW"


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


async def _process_month(month: date) -> tuple[int, int]:
    """Pull one month-first from FinMind, parse, ENRICH (compute yoy/mom
    from payload + DB history), upsert. Returns
    `(fetched_rows, upserted_rows)`.

    `month` must be a first-of-month — see `_month_starts` for why.
    `end_date` is deliberately not passed: this dataset's market-wide
    mode ignores it.
    """
    items = await finmind.get_monthly_revenue_market_wide(month.isoformat())
    fetched = len(items)
    if not items:
        return 0, 0

    payload: list[RevenueMonthlyRow] = []
    for r in items:
        sym = (r.get("symbol") or "").strip()
        if not sym:
            continue
        try:
            ts = date.fromisoformat(str(r.get("date", ""))[:10])
        except ValueError:
            continue
        try:
            rev = int(r.get("revenue", 0))
        except (TypeError, ValueError):
            rev = None
        payload.append(RevenueMonthlyRow(
            market=MARKET,
            symbol=sym,
            ts=ts,
            revenue=rev,
            revenue_yoy=None,
            revenue_mom=None,
            source="finmind_backfill",
        ))

    if not payload:
        return fetched, 0

    async with AsyncSessionLocal() as db:
        # _enrich_growth_rates queries DB for prev-year-same-month and
        # prev-month rows. Since we walk oldest → newest, by the time
        # we process e.g. Apr 2024, Apr 2023 is already in DB from an
        # earlier iteration → YoY computes against real data. The first
        # 12 months of any range have no prior year in the archive yet
        # → those rows get yoy=None (truthful), which is why a range
        # must start ~a year before the window you actually care about.
        enriched = await _enrich_growth_rates(db, payload)
        await upsert_revenue_monthly(db, enriched)
    return fetched, len(payload)


def _month_starts(start: date, end: date) -> list[date]:
    """Every first-of-month in `start..end`, oldest first.

    These are the ONLY dates this backfill can usefully ask for.
    FinMind's market-wide mode (`data_id=""`) for
    `TaiwanStockMonthRevenue` returns rows for the single `start_date`
    and ignores `end_date`, and revenue rows only ever land on a
    first-of-month. Probed against the live API:

        start=2024-02-01 end=2024-03-01 -> 2058 rows, all 2024-02-01
        start=2024-02-15 end=2024-03-15 -> 0 rows

    The previous 30-day-chunk walk therefore only hit a month by
    coincidence: a 2024-01-01..2026-07-31 run fetched just 2024-01 and
    2024-03 (4,112 rows) and silently reported success for the other
    29 months. `tasks/ingest_revenue_tw._month_starts_in_window` already
    encoded this for the daily cron; this script never did.
    """
    months: list[date] = []
    cursor = date(start.year, start.month, 1)
    if cursor < start:
        cursor = _shift_month(cursor, 1)
    while cursor <= end:
        months.append(cursor)
        cursor = _shift_month(cursor, 1)
    return months


async def backfill(start: date, end: date) -> tuple[int, int]:
    """Walk the month-firsts in `start..end` OLDEST FIRST. Returns
    `(total_fetched, total_upserted)`. Oldest-first matters: each
    month's `_enrich_growth_rates` pass computes YoY against the
    prev-year month, which the loop has already written.

    Per-month failures are logged + skipped so a transient FinMind 500
    doesn't abort an overnight backfill.
    """
    total_fetched = 0
    total_upserted = 0
    months = _month_starts(start, end)
    log.info(
        "backfill_revenue.plan",
        extra={"months": len(months), "first": months[0].isoformat() if months else None,
               "last": months[-1].isoformat() if months else None},
    )
    for month in months:
        log.info("backfill_revenue.month_start", extra={"month": month.isoformat()})
        try:
            fetched, upserted = await _process_month(month)
        except Exception as exc:
            log.error(
                "backfill_revenue.month_failed",
                extra={"month": month.isoformat(), "error": str(exc)},
            )
            continue

        total_fetched += fetched
        total_upserted += upserted
        # An empty month is worth seeing at WARNING: it means either the
        # archive genuinely lacks that month or the dataset got
        # paywalled mid-run, and both look identical in a success count.
        log.log(
            logging.WARNING if fetched == 0 else logging.INFO,
            "backfill_revenue.month_done",
            extra={
                "month": month.isoformat(),
                "fetched": fetched,
                "upserted": upserted,
                "running_total": total_upserted,
            },
        )

    return total_fetched, total_upserted


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--start", type=_parse_date, required=True,
                        help="ISO date (YYYY-MM-DD) — backfill start, inclusive")
    parser.add_argument("--end", type=_parse_date, required=True,
                        help="ISO date (YYYY-MM-DD) — backfill end, inclusive")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.start > args.end:
        print("error: --start must be on or before --end", file=sys.stderr)
        return 2

    fetched, upserted = asyncio.run(backfill(args.start, args.end))
    print(
        f"\n[ok] FinMind returned {fetched} rows; "
        f"{upserted} were enriched + upserted into tw_revenue_monthly. "
        f"YoY/MoM growth rates computed for rows whose prev-year / prev-"
        f"month baseline was already in the archive (earliest backfilled "
        f"month gets yoy=None — no baseline to compare against)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
