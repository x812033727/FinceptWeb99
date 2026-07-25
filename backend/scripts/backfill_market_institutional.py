"""One-shot TW full-market 三大法人 backfill from FinMind.

`tasks/ingest_holdings_aggregates_tw._ingest_total_institutional` asks
FinMind for `today - _TOTAL_INSTITUTIONAL_LOOKBACK_DAYS` onwards. That
is a ROLLING window, so history older than the lookback was never
fetched at all -- on a deployment that has been running for months,
`tw_market_institutional_daily` still only holds the last ~30 days.

That matters for backtests. The `market_institutional_5d` context block
reads this table, and a replay anchored before the window gets an empty
block. `_record_data_gaps` then names it, personas read "no chip data",
and the "籌碼同向" leg of their entry checklist can never be satisfied
-- so a replay abstains for a reason that is an artifact of the archive
rather than of the market. Observed directly: replaying 2026-04-24 and
2026-04-27 produced `abstained: true` on every strategy, each abstain
reason citing missing chip confirmation, while live runs on the same
code and the same batch size produced recommendations.

Unlike `TaiwanStockMonthRevenue` (see `backfill_revenue_finmind`), this
dataset's market-wide mode DOES honour a date range. Probed live:

    start=2026-04-01 end=2026-04-30 -> 120 rows / 20 trading days
    start=2026-04-01 end=None       -> 456 rows / 76 trading days

so one call per run is enough; the range is chunked only to keep any
single response small and to make a partial failure cheap to retry.

Run from inside the backend dir:

    python -m scripts.backfill_market_institutional --start 2026-04-01

The upsert is keyed on `(market, ts)`, so re-runs are idempotent.
"""
import argparse
import asyncio
import logging
import sys
from datetime import date, timedelta

import data.tw.finmind_connector as finmind
from db.session import AsyncSessionLocal
from services.ingest.repository import upsert_market_institutional_daily
from tasks.ingest_holdings_aggregates_tw import _aggregate_total_institutional

log = logging.getLogger(__name__)

# 90 days keeps each response to a few hundred rows while covering a
# typical 60-session replay window in one or two calls.
_CHUNK_DAYS = 90


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


async def _process_chunk(start: date, end: date) -> tuple[int, int]:
    """Fetch, aggregate and upsert one date range. Returns
    `(fetched_rows, upserted_days)`."""
    items = await finmind.get_total_institutional_market_wide(
        start.isoformat(), end.isoformat(),
    )
    if not items:
        return 0, 0
    payload = _aggregate_total_institutional(items)
    if not payload:
        return len(items), 0
    async with AsyncSessionLocal() as db:
        written = await upsert_market_institutional_daily(db, payload)
    return len(items), written


async def backfill(start: date, end: date) -> tuple[int, int]:
    """Walk `start..end` in chunks. Returns `(total_fetched,
    total_upserted)`.

    Order does not matter here -- unlike the revenue backfill, nothing
    in this table is derived from an earlier row -- but oldest-first
    keeps the log readable against the replay window.

    A chunk that raises is logged and skipped so one transient FinMind
    5xx doesn't abandon the rest of the range.
    """
    total_fetched = 0
    total_upserted = 0
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=_CHUNK_DAYS - 1), end)
        try:
            fetched, upserted = await _process_chunk(cursor, chunk_end)
        except Exception as exc:
            log.error(
                "backfill_market_institutional.chunk_failed",
                extra={
                    "start": cursor.isoformat(),
                    "end": chunk_end.isoformat(),
                    "error": str(exc),
                },
            )
            cursor = chunk_end + timedelta(days=1)
            continue

        total_fetched += fetched
        total_upserted += upserted
        # An empty chunk is a WARNING, not an INFO: "fetched 0, no
        # error" is exactly what a paywalled dataset looks like, and it
        # is indistinguishable from success in a total-rows count.
        log.log(
            logging.WARNING if fetched == 0 else logging.INFO,
            "backfill_market_institutional.chunk_done",
            extra={
                "start": cursor.isoformat(),
                "end": chunk_end.isoformat(),
                "fetched": fetched,
                "upserted": upserted,
                "running_total": total_upserted,
            },
        )
        cursor = chunk_end + timedelta(days=1)

    return total_fetched, total_upserted


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--start", type=_parse_date, required=True,
                        help="ISO date (YYYY-MM-DD) — backfill start, inclusive")
    parser.add_argument("--end", type=_parse_date, default=None,
                        help="ISO date (YYYY-MM-DD) — backfill end, inclusive "
                             "(default: today)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    end = args.end or date.today()
    if args.start > end:
        print("error: --start must be on or before --end", file=sys.stderr)
        return 2

    fetched, upserted = asyncio.run(backfill(args.start, end))
    print(
        f"\n[ok] FinMind returned {fetched} rows; {upserted} trading days "
        f"upserted into tw_market_institutional_daily "
        f"({args.start.isoformat()} .. {end.isoformat()})."
    )
    # Zero days written over a range that should contain trading days is
    # a failure worth a non-zero exit, not a quiet success line.
    return 0 if upserted else 1


if __name__ == "__main__":
    raise SystemExit(main())
