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
`tw_revenue_monthly` in monthly chunks, walking OLDEST → NEWEST so each
chunk's `_enrich_growth_rates` pass can compute YoY against rows
already-backfilled by previous chunks. Required: paid FINMIND_TOKEN.

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
from datetime import date, timedelta

import data.tw.finmind_connector as finmind
from db.session import AsyncSessionLocal
from services.ingest.repository import (
    RevenueMonthlyRow,
    upsert_revenue_monthly,
)
from tasks.ingest_revenue_tw import _enrich_growth_rates

log = logging.getLogger(__name__)

MARKET = "TW"

# FinMind market-wide returns one row per (symbol, ts), so a 30-day
# chunk covers ~1700 listed companies × 1-2 publish dates ≈ 2-4k rows.
# Going wider risks payload truncation; going narrower wastes calls.
_CHUNK_DAYS = 30


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


async def _process_chunk(chunk_start: date, chunk_end: date) -> tuple[int, int]:
    """Pull one chunk from FinMind, parse, ENRICH (compute yoy/mom from
    payload + DB history), upsert. Returns
    `(fetched_rows, upserted_rows)`."""
    items = await finmind.get_monthly_revenue_market_wide(
        chunk_start.isoformat(), chunk_end.isoformat(),
    )
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
        # we process e.g. Apr 2024, Apr 2023 is already in DB from the
        # prior chunk → YoY computes against real data. Earliest chunk
        # has no prior year → those rows get yoy=None (truthful).
        enriched = await _enrich_growth_rates(db, payload)
        await upsert_revenue_monthly(db, enriched)
    return fetched, len(payload)


async def backfill(start: date, end: date) -> tuple[int, int]:
    """Walk `start..end` in 30-day chunks OLDEST FIRST. Returns
    `(total_fetched, total_upserted)`. Per-chunk failures logged + skipped
    so a transient FinMind 500 doesn't abort an overnight backfill."""
    total_fetched = 0
    total_upserted = 0
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=_CHUNK_DAYS - 1), end)
        log.info(
            "backfill_revenue.chunk_start",
            extra={
                "start": cursor.isoformat(),
                "end": chunk_end.isoformat(),
            },
        )
        try:
            fetched, upserted = await _process_chunk(cursor, chunk_end)
        except Exception as exc:
            log.error(
                "backfill_revenue.chunk_failed",
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
        log.info(
            "backfill_revenue.chunk_done",
            extra={
                "fetched": fetched,
                "upserted": upserted,
                "running_total": total_upserted,
            },
        )
        cursor = chunk_end + timedelta(days=1)

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
