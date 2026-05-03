"""Backfill driver — Phase A & B unified entry point.

Usage (from `backend/`):

    # Single chunk:
    python -m finmind.scripts.backfill \\
        --dataset TaiwanStockPrice \\
        --symbol 2330 \\
        --start 2024-01-01 --end 2024-01-31

    # All enabled datasets, last N days:
    python -m finmind.scripts.backfill --enabled --days 30

    # Specific dataset, full universe (per-symbol fan-out):
    python -m finmind.scripts.backfill \\
        --dataset TaiwanStockPrice --start 2024-01-01 --end 2024-12-31 \\
        --symbols-file symbols.txt

The driver itself is single-threaded — concurrency is the operator's
job (run multiple processes pinned to disjoint dataset_code lists).
That trades simplicity for blast-radius: a flaky connector / bad
mapping crashes one process at most, instead of breaking a worker
pool.

Resume: re-running the same command picks up where the previous
invocation left off because `ingest_chunk` is idempotent and
`backfill_progress` records done chunks.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from finmind.db.session import (  # noqa: E402
    FinmindAsyncSessionLocal,
    finmind_engine,
)
from finmind.ingest.runner import (  # noqa: E402
    ingest_chunk,
    list_enabled_datasets,
)


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


async def _run_one(
    dataset_code: str,
    symbol: str | None,
    start: date,
    end: date,
) -> None:
    async with FinmindAsyncSessionLocal() as session:
        result = await ingest_chunk(
            session,
            dataset_code=dataset_code,
            symbol=symbol,
            range_start=start,
            range_end=end,
        )
    sym_str = f" {symbol}" if symbol else ""
    print(
        f"  {dataset_code}{sym_str} {start}..{end}: "
        f"{result.status} ({result.rows_written} rows)"
        + (f" — {result.error}" if result.error else "")
    )


async def _run_enabled(start: date, end: date) -> None:
    """Walk every enabled dataset (no per-symbol fan-out — runs the
    market-wide form which is what most enabled crons need). For per-
    symbol bulk backfill, use --dataset + --symbols-file."""
    async with FinmindAsyncSessionLocal() as session:
        datasets = await list_enabled_datasets(session)

    if not datasets:
        print("backfill: no enabled datasets — flip dataset_sources.enabled "
              "via the admin UI to opt in")
        return

    print(f"backfill: running {len(datasets)} enabled datasets {start}..{end}")
    for ds in datasets:
        await _run_one(ds.dataset_code, None, start, end)


async def _run_symbols(
    dataset_code: str,
    symbols_file: Path,
    start: date,
    end: date,
) -> None:
    """Per-symbol fan-out for bulk historical backfill. One symbol per
    line in `symbols_file`; lines starting with '#' or empty are
    skipped."""
    symbols = [
        line.strip()
        for line in symbols_file.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    print(
        f"backfill: {dataset_code} × {len(symbols)} symbols "
        f"{start}..{end}"
    )
    for sym in symbols:
        await _run_one(dataset_code, sym, start, end)


async def amain() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--dataset", help="Single dataset code")
    parser.add_argument("--symbol", help="Single symbol (with --dataset)")
    parser.add_argument(
        "--symbols-file",
        type=Path,
        help="File with one symbol per line (with --dataset)",
    )
    parser.add_argument(
        "--enabled",
        action="store_true",
        help="Walk every enabled dataset (no per-symbol fan-out)",
    )
    parser.add_argument(
        "--start", help="Range start YYYY-MM-DD (default: --days ago)",
    )
    parser.add_argument(
        "--end", help="Range end YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Days back from --end when --start is omitted (default: 30)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
        force=True,
    )

    end = _parse_date(args.end) if args.end else datetime.now(tz=timezone.utc).date()
    start = (
        _parse_date(args.start)
        if args.start
        else end - timedelta(days=args.days)
    )

    if args.enabled:
        await _run_enabled(start, end)
    elif args.dataset and args.symbols_file:
        await _run_symbols(args.dataset, args.symbols_file, start, end)
    elif args.dataset:
        await _run_one(args.dataset, args.symbol, start, end)
    else:
        parser.error(
            "specify one of: --enabled | --dataset [--symbol|--symbols-file]"
        )

    await finmind_engine.dispose()
    return 0


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    os.chdir(_BACKEND_DIR)
    main()
