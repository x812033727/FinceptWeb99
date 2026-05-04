"""Deep historical backfill — multi-year, per-symbol, resumable.

Purpose: bring a dataset's local mirror up to date from N years
ago to today. Distinct from `scripts/backfill.py` (single-chunk
operator command) and `scripts/run_due.py` (daily incremental
cron) — this one is the "we just turned on a new dataset, fill in
the archive" workflow.

Strategy:
  - Resolve the symbol universe from `tw_stock_info` (or accept an
    explicit --symbols-file)
  - Iterate months between --start and --end (default: today − 10y → today)
  - Per (symbol, month) chunk → ingest_chunk
  - Skip chunks already in `backfill_progress.status='done'` for the
    same (dataset_code, symbol, source, range_start, range_end) key
  - Print progress every 25 chunks: "X/Y chunks (Z%), ETA HH:MM"

Idempotent: re-running picks up where the previous invocation left
off because `ingest_chunk` is itself idempotent and
`backfill_progress` records the chunk-level outcome. Crash-safe:
each chunk commits independently, so a SIGKILL mid-run loses at
most the current chunk's work.

Usage (from backend/):

    # Default: 10y archive of TaiwanStockPrice using
    # tw_stock_info universe
    python -m finmind.scripts.deep_backfill --dataset TaiwanStockPrice

    # Explicit window + universe
    python -m finmind.scripts.deep_backfill \\
        --dataset TaiwanStockPrice --start 2014-01-01 --end 2024-12-31 \\
        --symbols-file universe.txt

    # Market-wide dataset (no per-symbol fan-out)
    python -m finmind.scripts.deep_backfill \\
        --dataset TaiwanStockTotalMarginPurchaseShortSale --years 5
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from sqlalchemy import select  # noqa: E402

from finmind.db.session import (  # noqa: E402
    FinmindAsyncSessionLocal,
    finmind_engine,
)
from finmind.ingest.runner import ingest_chunk  # noqa: E402
from finmind.models.backfill_progress import BackfillProgress  # noqa: E402
from finmind.models.dataset_source import DatasetSource  # noqa: E402
from finmind.scheduler.runner import (  # noqa: E402
    get_universe_from_tw_stock_info,
)


def _month_starts(start: date, end: date) -> list[date]:
    """First-of-each-month date list spanning [start, end]."""
    out: list[date] = []
    cur = start.replace(day=1)
    while cur <= end:
        out.append(cur)
        cur = (
            cur.replace(year=cur.year + 1, month=1)
            if cur.month == 12
            else cur.replace(month=cur.month + 1)
        )
    return out


def _month_end(month_start: date) -> date:
    """Last day of the month containing `month_start`."""
    if month_start.month == 12:
        next_month = month_start.replace(year=month_start.year + 1, month=1)
    else:
        next_month = month_start.replace(month=month_start.month + 1)
    return next_month - timedelta(days=1)


async def _already_done(
    session,
    *,
    dataset_code: str,
    symbol: str | None,
    source: str,
    range_start: date,
    range_end: date,
) -> bool:
    """Resume helper — True iff this exact chunk already completed
    in a previous invocation."""
    result = (
        await session.execute(
            select(BackfillProgress.status).where(
                BackfillProgress.dataset_code == dataset_code,
                BackfillProgress.symbol.is_(None) if symbol is None
                else BackfillProgress.symbol == symbol,
                BackfillProgress.source == source,
                BackfillProgress.range_start == range_start,
                BackfillProgress.range_end == range_end,
            )
        )
    ).scalar_one_or_none()
    return result == "done"


def _format_eta(elapsed: float, done: int, total: int) -> str:
    if done == 0:
        return "—"
    rate = done / elapsed  # chunks/sec
    remaining = (total - done) / rate
    if remaining > 3600:
        return f"{remaining / 3600:.1f}h"
    return f"{int(remaining // 60)}m{int(remaining % 60)}s"


async def _resolve_symbols(args, session) -> list[str | None]:
    """Per-symbol datasets → list of symbols. Market-wide datasets →
    [None] (one chunk per month, no fan-out)."""
    ds = await session.get(DatasetSource, args.dataset)
    if ds is None:
        raise SystemExit(
            f"unknown dataset: {args.dataset} — run init_db first"
        )
    if not ds.local_table:
        raise SystemExit(
            f"{args.dataset}: no local_table — Phase 1 hasn't built "
            "the destination table for this dataset yet"
        )

    if not ds.per_symbol:
        return [None]

    if args.symbols:
        return [s.strip() for s in args.symbols.split(",") if s.strip()]
    if args.symbols_file:
        return [
            line.strip()
            for line in Path(args.symbols_file).read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
    universe = await get_universe_from_tw_stock_info(session)
    if not universe:
        raise SystemExit(
            "per-symbol dataset but no universe supplied — pass "
            "--symbols / --symbols-file, OR enable TaiwanStockInfo "
            "and run it once to populate tw_stock_info"
        )
    return list(universe)


async def amain() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--dataset",
        required=True,
        help="FinMind dataset code (e.g. TaiwanStockPrice).",
    )
    parser.add_argument("--start", help="Range start YYYY-MM-DD.")
    parser.add_argument("--end", help="Range end YYYY-MM-DD.")
    parser.add_argument(
        "--years",
        type=int,
        default=10,
        help="Years back from --end (or today) when --start is omitted.",
    )
    parser.add_argument(
        "--symbols",
        help=(
            "Comma-separated symbol universe for per-symbol datasets. "
            "Default: read from tw_stock_info."
        ),
    )
    parser.add_argument(
        "--symbols-file",
        help="File with one symbol per line.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "Stop after this many chunks (useful for smoke-testing a "
            "deep run before committing to the full archive)."
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
        force=True,
    )

    end = (
        datetime.strptime(args.end, "%Y-%m-%d").date()
        if args.end
        else datetime.now(tz=timezone.utc).date()
    )
    start = (
        datetime.strptime(args.start, "%Y-%m-%d").date()
        if args.start
        else end.replace(year=end.year - args.years)
    )

    async with FinmindAsyncSessionLocal() as session:
        symbols = await _resolve_symbols(args, session)

    months = _month_starts(start, end)
    total_chunks = len(symbols) * len(months)
    if args.limit is not None:
        total_chunks = min(total_chunks, args.limit)

    print(
        f"deep_backfill: {args.dataset} | {len(symbols)} symbols × "
        f"{len(months)} months = {total_chunks} chunks "
        f"({start} .. {end})"
    )

    started = time.monotonic()
    done = 0
    skipped_resume = 0
    failed = 0
    rows_total = 0

    for month_start in months:
        rs = max(start, month_start)
        re = min(end, _month_end(month_start))
        for symbol in symbols:
            if args.limit is not None and done + skipped_resume >= args.limit:
                break

            async with FinmindAsyncSessionLocal() as session:
                # Resume check — skip chunks already in 'done'.
                if await _already_done(
                    session,
                    dataset_code=args.dataset,
                    symbol=symbol,
                    # We use FinMind here; if the operator pre-flipped
                    # active_source the runner picks the new source
                    # internally — but the resume key tracks 'finmind'
                    # for this script's purposes (operator can pass
                    # --rerun later when changing sources).
                    source="finmind",
                    range_start=rs,
                    range_end=re,
                ):
                    skipped_resume += 1
                    continue

                result = await ingest_chunk(
                    session,
                    dataset_code=args.dataset,
                    symbol=symbol,
                    range_start=rs,
                    range_end=re,
                )

            if result.status == "done":
                done += 1
                rows_total += result.rows_written
            elif result.status == "failed":
                failed += 1
            # 'skipped' (mapping not found etc.) goes uncounted

            processed = done + failed + skipped_resume
            if processed % 25 == 0 or processed == total_chunks:
                pct = (
                    int(processed / total_chunks * 100)
                    if total_chunks
                    else 100
                )
                eta = _format_eta(
                    time.monotonic() - started, processed, total_chunks
                )
                print(
                    f"  {processed}/{total_chunks} ({pct}%) — "
                    f"done={done} failed={failed} resume_skip={skipped_resume}, "
                    f"{rows_total:,} rows, ETA {eta}"
                )

    elapsed = time.monotonic() - started
    print(
        f"deep_backfill: complete in {elapsed:.0f}s — "
        f"done={done} failed={failed} resume_skip={skipped_resume} "
        f"({rows_total:,} rows written)"
    )

    await finmind_engine.dispose()
    return 0 if failed == 0 else 1


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    os.chdir(_BACKEND_DIR)
    main()
