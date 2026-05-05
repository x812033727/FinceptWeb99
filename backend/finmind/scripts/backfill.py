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


async def _reset_stuck_running(
    dataset_code: str | None, stuck_after_minutes: int
) -> int:
    """Flip `running` chunks older than the threshold back to `pending`
    so the next claim re-fetches them. Returns the number reset.

    Used by `--reset-stuck` and as a precondition for `--retry-failed`
    so a stale `running` chunk doesn't get filtered out from retry just
    because its status hasn't been written back as `failed` yet."""
    from sqlalchemy import text

    where_dataset = (
        " AND dataset_code = :dataset"
        if dataset_code else ""
    )
    sql = text(
        f"UPDATE backfill_progress "
        f"   SET status='pending', error_message='reset by --reset-stuck' "
        f" WHERE status='running' "
        f"   AND started_at < (now() at time zone 'utc') - "
        f"       (':mins minutes')::interval"
        f"   {where_dataset}".replace(":mins", str(int(stuck_after_minutes)))
    )
    params: dict = {}
    if dataset_code:
        params["dataset"] = dataset_code
    async with FinmindAsyncSessionLocal() as session:
        result = await session.execute(sql, params)
        await session.commit()
    return int(result.rowcount or 0)


async def _retry_failed(
    dataset_code: str, start: date, end: date,
) -> None:
    """Read pending + failed chunks for `dataset_code` from the ledger
    and re-run them. Bypasses the equity universe entirely — only the
    symbols that actually need a retry get touched, instead of the
    full fan-out wasting API calls on already-done chunks.

    Includes `pending` because `--reset-stuck` flips stale `running`
    chunks to `pending`, and an operator chaining `--reset-stuck
    --retry-failed` should pick those up too."""
    from sqlalchemy import text

    async with FinmindAsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT DISTINCT symbol FROM backfill_progress "
                " WHERE dataset_code = :dataset "
                "   AND status IN ('failed', 'pending') "
                "   AND symbol IS NOT NULL "
                " ORDER BY symbol"
            ),
            {"dataset": dataset_code},
        )
        symbols = [row[0] for row in result]

    print(
        f"backfill: --retry-failed {dataset_code} × {len(symbols)} symbols "
        f"({start}..{end})"
    )
    if not symbols:
        # Either the dataset is healthy or it's market-wide. Try the
        # market-wide path so a missing-symbols ledger row also gets
        # retried — better than silently doing nothing.
        await _run_one(dataset_code, None, start, end)
        return
    for sym in symbols:
        await _run_one(dataset_code, sym, start, end)


async def _run_warrant(args, start: date, end: date) -> None:
    """Mirror of `_run_symbols` but for warrant-indexed datasets. Picks
    the warrant universe from either `--warrant-symbols-file` or
    `--warrant-universe-from-tw-stock-info`. Used by datasets in
    `dispatcher._WARRANT_UNIVERSE_DATASETS` so a fresh deploy can
    backfill warrant trading reports without juggling a separate
    pre-built symbols file."""
    if args.warrant_symbols_file:
        symbols = [
            line.strip()
            for line in args.warrant_symbols_file.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
    else:
        from finmind.scheduler.runner import (
            get_warrant_universe_from_tw_stock_info,
        )
        async with FinmindAsyncSessionLocal() as session:
            symbols = await get_warrant_universe_from_tw_stock_info(session)

    print(
        f"backfill: {args.dataset} × {len(symbols)} warrant codes "
        f"{start}..{end}"
    )
    for sym in symbols:
        await _run_one(args.dataset, sym, start, end)


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
        "--warrant-symbols-file",
        type=Path,
        help=(
            "File with one warrant code per line, used when --dataset is "
            "in dispatcher._WARRANT_UNIVERSE_DATASETS (e.g. "
            "TaiwanStockWarrantTradingDailyReport). Mutually exclusive "
            "with --warrant-universe-from-tw-stock-info."
        ),
    )
    parser.add_argument(
        "--warrant-universe-from-tw-stock-info",
        action="store_true",
        help=(
            "Auto-discover the warrant universe from `tw_stock_info` "
            "(`is_warrant=true` rows, 6-char codes). Used in place of "
            "--symbols-file when --dataset is a warrant-indexed dataset. "
            "Mirrors the same flag in `run_due.py`."
        ),
    )
    parser.add_argument(
        "--enabled",
        action="store_true",
        help="Walk every enabled dataset (no per-symbol fan-out)",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help=(
            "With --dataset X, retry only the symbols whose ledger "
            "status is 'failed' or 'pending'. Skips already-done "
            "symbols entirely so a flaky-network recovery doesn't "
            "burn quota on UPSERTs that would no-op. For market-wide "
            "datasets (no symbol axis), retries the single chunk."
        ),
    )
    parser.add_argument(
        "--reset-stuck",
        action="store_true",
        help=(
            "Before running, flip ledger rows in `status='running'` "
            "older than --stuck-after minutes back to 'pending'. "
            "Used to recover from a backfill killed mid-flight (e.g. "
            "deploy or SIGTERM). Pairs naturally with --retry-failed."
        ),
    )
    parser.add_argument(
        "--stuck-after",
        type=int,
        default=5,
        metavar="MINS",
        help=(
            "Stuck-running threshold in minutes for --reset-stuck "
            "(default: 5). Smaller = more aggressive; larger = "
            "tolerates longer in-flight chunks."
        ),
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

    if args.warrant_universe_from_tw_stock_info and args.warrant_symbols_file:
        parser.error(
            "specify at most one of --warrant-universe-from-tw-stock-info "
            "/ --warrant-symbols-file"
        )

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

    # Decide the per-symbol path. When --dataset is in
    # _WARRANT_UNIVERSE_DATASETS and warrant flags are supplied, the
    # warrant universe takes precedence over --symbols-file so a single
    # invocation can target a warrant-indexed dataset cleanly.
    from finmind.scheduler.dispatcher import _WARRANT_UNIVERSE_DATASETS
    is_warrant_dataset = (
        args.dataset is not None
        and args.dataset in _WARRANT_UNIVERSE_DATASETS
    )
    has_warrant_flag = (
        args.warrant_universe_from_tw_stock_info
        or args.warrant_symbols_file is not None
    )

    # Optional pre-step: reset stuck-running chunks first so the
    # subsequent --retry-failed pass picks them up. Useful after a
    # deploy / SIGTERM that left chunks half-done in the ledger.
    if args.reset_stuck:
        n = await _reset_stuck_running(args.dataset, args.stuck_after)
        scope = f"in {args.dataset}" if args.dataset else "(all datasets)"
        print(
            f"backfill: --reset-stuck flipped {n} chunks "
            f"running→pending {scope} (older than {args.stuck_after}min)"
        )

    if args.enabled:
        await _run_enabled(start, end)
    elif args.dataset and args.retry_failed:
        await _retry_failed(args.dataset, start, end)
    elif args.dataset and is_warrant_dataset and has_warrant_flag:
        await _run_warrant(args, start, end)
    elif args.dataset and args.symbols_file:
        await _run_symbols(args.dataset, args.symbols_file, start, end)
    elif args.dataset:
        await _run_one(args.dataset, args.symbol, start, end)
    elif args.reset_stuck:
        # `--reset-stuck` alone (no --dataset, no other action) is a
        # legitimate use case: just clean up stale running chunks.
        return 0
    else:
        parser.error(
            "specify one of: --enabled | --dataset [--symbol|--symbols-file"
            "|--warrant-symbols-file|--warrant-universe-from-tw-stock-info"
            "|--retry-failed] | --reset-stuck"
        )

    await finmind_engine.dispose()
    return 0


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    os.chdir(_BACKEND_DIR)
    main()
