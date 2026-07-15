"""One-shot TW OHLCV backfill.

Run from inside the backend dir:
    python -m scripts.backfill_ohlcv_tw --months 8
    python -m scripts.backfill_ohlcv_tw --symbols 2330,0050 --months 18
    python -m scripts.backfill_ohlcv_tw --months 8 --start-after 2454

Hits TWSE month-by-month per symbol with the connector's shared pacing.
The database is the resume ledger: closed months with at least 15 archived
sessions are skipped by default, so an interrupted run can safely restart.
Concurrency is intentionally 1 (TWSE 429s on overlap).

FinMind is used as a per-symbol gap-fill if TWSE returns nothing for a
month, but we don't fall through automatically because doing so can
consume the configured hourly request budget quickly. Pass `--use-finmind`
to enable gap-fill explicitly.
"""
import argparse
import asyncio
import logging
from datetime import date, timedelta

from sqlalchemy import func, select

import data.tw.finmind_connector as finmind
import data.tw.twse_connector as twse
from db.session import AsyncSessionLocal
from models.ohlcv_daily import OhlcvDaily
from services.ingest.repository import OhlcvBar, upsert_ohlcv_bars

log = logging.getLogger(__name__)


def _months_back(today: date, years: int | None = None, *, months: int | None = None) -> list[date]:
    """Month anchors newest-first; ``years`` remains CLI-backward compatible."""
    count = months if months is not None else (years or 1) * 12
    out: list[date] = []
    cursor = today.replace(day=1)
    for _ in range(count):
        out.append(cursor)
        # Step to the previous month's first day.
        prev_month_last = cursor - timedelta(days=1)
        cursor = prev_month_last.replace(day=1)
    return out


def _month_end(anchor: date) -> date:
    return (anchor.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


async def _can_resume_month(db, symbol: str, anchor: date, today: date) -> bool:
    """Skip a substantially populated closed month; current month always refreshes."""
    if _month_end(anchor) >= today.replace(day=1):
        return False
    archived = await db.scalar(
        select(func.count()).select_from(OhlcvDaily).where(
            OhlcvDaily.market == "TW",
            OhlcvDaily.symbol == symbol,
            OhlcvDaily.ts >= anchor,
            OhlcvDaily.ts <= _month_end(anchor),
        )
    )
    return int(archived or 0) >= 15


async def _backfill_symbol(
    db, symbol: str, anchors: list[date], use_finmind: bool,
    *, resume: bool = True, today: date | None = None,
) -> tuple[int, int]:
    rows_total = 0
    skipped = 0
    current_day = today or date.today()
    for anchor in anchors:
        if resume and await _can_resume_month(db, symbol, anchor, current_day):
            skipped += 1
            continue
        try:
            rows = await twse.get_daily_ohlcv(symbol, anchor)
        except Exception as exc:
            log.warning("backfill.twse_failed",
                        extra={"symbol": symbol, "anchor": anchor.isoformat(),
                               "error": str(exc)})
            rows = []

        if not rows and use_finmind:
            try:
                start = anchor.isoformat()
                end = _month_end(anchor)
                rows = await finmind.get_daily_ohlcv(symbol, start, end.isoformat())
                source = "finmind"
            except Exception as exc:
                log.warning("backfill.finmind_failed",
                            extra={"symbol": symbol, "anchor": anchor.isoformat(),
                                   "error": str(exc)})
                rows = []
                source = "finmind"
        else:
            source = "twse"

        bars = [
            b for b in (
                OhlcvBar.from_connector_row("TW", symbol, source, r) for r in rows
            )
            if b is not None
        ]
        if bars:
            rows_total += await upsert_ohlcv_bars(db, bars)
    return rows_total, skipped


async def _resolve_symbols(explicit: list[str] | None) -> list[str]:
    if explicit:
        return explicit
    from services.tw_market_service import _exchange_map, refresh_symbol_map
    if not _exchange_map:
        await refresh_symbol_map()
    return sorted(_exchange_map.keys())


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--months", type=int, default=8,
                        help="Calendar months to backfill; 8 usually covers the 148-session factor window.")
    parser.add_argument("--years", type=int, default=None,
                        help="Deprecated compatibility alias; overrides --months when supplied.")
    parser.add_argument("--symbols", type=str, default="",
                        help="Comma-separated symbols. Default = full TW universe.")
    parser.add_argument("--use-finmind", action="store_true",
                        help="Fall back to FinMind on TWSE miss (consumes quota).")
    parser.add_argument("--no-resume", action="store_true",
                        help="Refetch already-populated closed months.")
    parser.add_argument("--start-after", type=str, default="",
                        help="Resume the sorted full universe after this symbol.")
    parser.add_argument("--max-symbols", type=int, default=0,
                        help="Process at most N symbols in this invocation (0 = unlimited).")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    explicit = [s.strip() for s in args.symbols.split(",") if s.strip()] or None
    symbols = await _resolve_symbols(explicit)
    if args.start_after:
        symbols = [symbol for symbol in symbols if symbol > args.start_after]
    if args.max_symbols > 0:
        symbols = symbols[:args.max_symbols]
    month_count = args.years * 12 if args.years is not None else args.months
    if month_count < 1 or month_count > 120:
        parser.error("--months must resolve to a value between 1 and 120")
    anchors = _months_back(date.today(), months=month_count)
    log.info("backfill.starting symbols=%d months=%d", len(symbols), len(anchors))

    grand_total = 0
    skipped_total = 0
    async with AsyncSessionLocal() as db:
        for i, sym in enumerate(symbols, 1):
            written, skipped = await _backfill_symbol(
                db, sym, anchors, args.use_finmind, resume=not args.no_resume,
            )
            grand_total += written
            skipped_total += skipped
            if i % 50 == 0:
                log.info(
                    "backfill.progress %d/%d rows_total=%d months_skipped=%d",
                    i, len(symbols), grand_total, skipped_total,
                )

    log.info("backfill.done rows_total=%d months_skipped=%d", grand_total, skipped_total)


if __name__ == "__main__":
    asyncio.run(main())
