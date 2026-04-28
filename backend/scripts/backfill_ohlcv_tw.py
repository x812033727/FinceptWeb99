"""One-shot TW OHLCV backfill.

Run from inside the backend dir:
    python -m scripts.backfill_ohlcv_tw --years 5
    python -m scripts.backfill_ohlcv_tw --symbols 2330,0050 --years 2

Hits TWSE month-by-month per symbol. TWSE's historical endpoint has no
quota and ~1 s pacing per request; ~2000 syms × 60 months ≈ 120k requests
≈ 33 hours. Run overnight on a stable host. Concurrency is intentionally
1 (TWSE 429s on overlap).

FinMind is used as a per-symbol gap-fill if TWSE returns nothing for a
month, but we don't fall through automatically because doing so would
exhaust the 600/day quota in the first half-hour. Pass `--use-finmind`
to enable gap-fill explicitly.
"""
import argparse
import asyncio
import logging
from datetime import date, timedelta

import data.tw.finmind_connector as finmind
import data.tw.twse_connector as twse
from db.session import AsyncSessionLocal
from services.ingest.repository import OhlcvBar, upsert_ohlcv_bars

log = logging.getLogger(__name__)


def _months_back(today: date, years: int) -> list[date]:
    """List of month-anchor dates (the 1st of each month) going back `years`."""
    out: list[date] = []
    cursor = today.replace(day=1)
    for _ in range(years * 12):
        out.append(cursor)
        # Step to the previous month's first day.
        prev_month_last = cursor - timedelta(days=1)
        cursor = prev_month_last.replace(day=1)
    return out


async def _backfill_symbol(
    db, symbol: str, anchors: list[date], use_finmind: bool,
) -> int:
    rows_total = 0
    for anchor in anchors:
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
                end = (anchor.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
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
    return rows_total


async def _resolve_symbols(explicit: list[str] | None) -> list[str]:
    if explicit:
        return explicit
    from services.tw_market_service import _exchange_map, refresh_symbol_map
    if not _exchange_map:
        await refresh_symbol_map()
    return sorted(_exchange_map.keys())


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", type=int, default=5)
    parser.add_argument("--symbols", type=str, default="",
                        help="Comma-separated symbols. Default = full TW universe.")
    parser.add_argument("--use-finmind", action="store_true",
                        help="Fall back to FinMind on TWSE miss (consumes quota).")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    explicit = [s.strip() for s in args.symbols.split(",") if s.strip()] or None
    symbols = await _resolve_symbols(explicit)
    anchors = _months_back(date.today(), args.years)
    log.info("backfill.starting symbols=%d months=%d", len(symbols), len(anchors))

    grand_total = 0
    async with AsyncSessionLocal() as db:
        for i, sym in enumerate(symbols, 1):
            written = await _backfill_symbol(db, sym, anchors, args.use_finmind)
            grand_total += written
            if i % 50 == 0:
                log.info("backfill.progress %d/%d rows_total=%d", i, len(symbols), grand_total)

    log.info("backfill.done rows_total=%d", grand_total)


if __name__ == "__main__":
    asyncio.run(main())
