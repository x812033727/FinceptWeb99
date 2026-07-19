"""CLI: run every enabled dataset whose `last_ingest_at` is stale.

Designed to be invoked from cron / k8s CronJob / APScheduler hook
every 15 minutes:

    */15 * * * * cd /app/backend && python -m finmind.scripts.run_due

For per-symbol datasets (TaiwanStockPrice, ...) the operator chooses
the universe:

    # Hardcoded list:
    python -m finmind.scripts.run_due --symbols 2330,2317,2454

    # From a file (one symbol per line, # comments ok):
    python -m finmind.scripts.run_due --symbols-file universe.txt

    # Auto-discover from tw_stock_info (preferred for production —
    # picks up new listings + drops delisted symbols automatically,
    # as long as TaiwanStockInfo is also enabled):
    python -m finmind.scripts.run_due --universe-from-tw-stock-info

Without --symbols / --symbols-file / --universe-from-tw-stock-info,
per-symbol datasets are still attempted with symbol=None — most
upstream sources fail in that case and the chunk lands as 'failed'
in `backfill_progress`. Use --skip-per-symbol to filter them out
before any upstream request. Production Taiwan market-wide cron should
also pass --tw-only so crypto and non-Taiwan macro datasets stay out of
that sweep.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[2]
_TAIWAN_DATASET_PREFIXES = ("Taiwan", "taiwan_")
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from finmind.db.session import (  # noqa: E402
    FinmindAsyncSessionLocal,
    finmind_engine,
)
from finmind.scheduler.runner import (  # noqa: E402
    get_crypto_universe,
    get_universe_from_tw_stock_info,
    get_warrant_universe_from_tw_stock_info,
    run_due_now,
)


async def _load_symbols(args, session) -> list[str] | None:
    """Resolve the per-symbol equity universe per the CLI flags.
    Mutually exclusive — checked at parse time. Returns None when no
    source is specified (per-symbol datasets fall through to
    symbol=None and the runner records them as failed unless
    --skip-per-symbol)."""
    if args.symbols:
        return [s.strip() for s in args.symbols.split(",") if s.strip()]
    if args.symbols_file:
        return [
            line.strip()
            for line in Path(args.symbols_file).read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
    if args.universe_from_tw_stock_info:
        symbols = await get_universe_from_tw_stock_info(
            session,
            market=args.universe_market,
            exclude_warrants=not args.include_warrants,
        )
        if not symbols:
            print(
                "run_due: --universe-from-tw-stock-info found 0 rows. "
                "Enable TaiwanStockInfo + run it once before relying on "
                "this flag (or fall back to --symbols-file)."
            )
        return symbols
    return None


async def _load_warrant_symbols(args, session) -> list[str] | None:
    """Resolve the warrant-only universe — independent of the equity
    universe because the two are routed to different dataset codes.
    Returns None when neither flag is set (warrant per-symbol datasets
    record as failed unless skipped)."""
    if args.warrant_symbols_file:
        return [
            line.strip()
            for line in Path(args.warrant_symbols_file).read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
    if args.warrant_universe_from_tw_stock_info:
        return await get_warrant_universe_from_tw_stock_info(session)
    return None


async def _load_crypto_symbols(args, session) -> list[str] | None:
    """Resolve the crypto (Binance-pair) universe for per-symbol crypto
    datasets. Independent of the equity/warrant universes — routed by
    active_source in the dispatcher. Returns None when the flag is off
    (crypto per-symbol datasets then record symbol=None → failed, unless
    --skip-per-symbol)."""
    if args.crypto_universe_from_db:
        return await get_crypto_universe(session)
    return None


async def amain() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--symbols",
        help="Comma-separated symbol universe for per-symbol datasets.",
    )
    parser.add_argument(
        "--symbols-file",
        help="File with one symbol per line (# comments ok).",
    )
    parser.add_argument(
        "--universe-from-tw-stock-info",
        action="store_true",
        help=(
            "Auto-discover the universe from `tw_stock_info`. "
            "Recommended for production — picks up new listings + "
            "drops delisted symbols automatically. Requires "
            "TaiwanStockInfo to be enabled + ingested at least once."
        ),
    )
    parser.add_argument(
        "--universe-market",
        choices=("TWSE", "OTC"),
        default=None,
        help=(
            "Filter --universe-from-tw-stock-info to one market "
            "(default: both TWSE + OTC)."
        ),
    )
    parser.add_argument(
        "--include-warrants",
        action="store_true",
        help=(
            "Include warrants in --universe-from-tw-stock-info "
            "(default: equities only — warrants dwarf the equity "
            "count and are rarely useful for daily backfill)."
        ),
    )
    parser.add_argument(
        "--warrant-universe-from-tw-stock-info",
        action="store_true",
        help=(
            "Auto-discover the warrant universe from `tw_stock_info` "
            "(`is_warrant=true` rows, 6-char codes only). Only datasets "
            "in dispatcher._WARRANT_UNIVERSE_DATASETS use this — equity "
            "per-symbol datasets continue to use the equity universe."
        ),
    )
    parser.add_argument(
        "--warrant-symbols-file",
        help=(
            "File with one warrant code per line, used by datasets in "
            "dispatcher._WARRANT_UNIVERSE_DATASETS. Mutually exclusive "
            "with --warrant-universe-from-tw-stock-info."
        ),
    )
    parser.add_argument(
        "--crypto-universe-from-db",
        action="store_true",
        help=(
            "Auto-discover the crypto universe from `crypto_universe` "
            "(active + Binance-mapped rows) and scope this run to the "
            "crypto category. Required for the crypto cron — per-symbol "
            "crypto datasets (CryptoPrice*) fan out across it. Populate "
            "the table first with "
            "`crypto_universe_refresh`."
        ),
    )
    parser.add_argument(
        "--tw-only",
        action="store_true",
        help=(
            "Scope this run to Taiwan dataset codes (`Taiwan*` and "
            "`taiwan_*`). Production market-wide cron combines this "
            "with --skip-per-symbol."
        ),
    )
    parser.add_argument(
        "--skip-per-symbol",
        action="store_true",
        help=(
            "Drop per-symbol datasets from this run. Useful when no "
            "universe is supplied and you don't want failed chunks "
            "polluting the progress ledger."
        ),
    )
    args = parser.parse_args()

    # Mutually exclusive — checked here rather than via argparse groups
    # so the error message is specific.
    sources = sum(
        1 for f in (args.symbols, args.symbols_file,
                    args.universe_from_tw_stock_info) if f
    )
    if sources > 1:
        parser.error(
            "specify at most one of --symbols / --symbols-file / "
            "--universe-from-tw-stock-info"
        )

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
        force=True,
    )

    if args.warrant_universe_from_tw_stock_info and args.warrant_symbols_file:
        parser.error(
            "specify at most one of --warrant-universe-from-tw-stock-info / "
            "--warrant-symbols-file"
        )
    if args.tw_only and args.crypto_universe_from_db:
        parser.error(
            "--tw-only and --crypto-universe-from-db are mutually exclusive"
        )

    async with FinmindAsyncSessionLocal() as session:
        symbols = await _load_symbols(args, session)
        warrant_symbols = await _load_warrant_symbols(args, session)
        crypto_symbols = await _load_crypto_symbols(args, session)

        outcomes = await run_due_now(
            session,
            symbols=symbols,
            warrant_symbols=warrant_symbols,
            crypto_symbols=crypto_symbols,
            categories=(
                {"crypto"} if args.crypto_universe_from_db else None
            ),
            dataset_code_prefixes=(
                _TAIWAN_DATASET_PREFIXES if args.tw_only else None
            ),
            skip_per_symbol=args.skip_per_symbol,
            now=datetime.now(tz=timezone.utc),
        )

    if not outcomes:
        print("run_due: nothing due — all enabled datasets are fresh.")
    else:
        done = sum(1 for o in outcomes if o.result.status == "done")
        failed = sum(1 for o in outcomes if o.result.status == "failed")
        skipped = sum(1 for o in outcomes if o.result.status == "skipped")
        rows = sum(o.result.rows_written for o in outcomes)
        print(
            f"run_due: {len(outcomes)} chunks — done={done} "
            f"failed={failed} skipped={skipped}, {rows} rows written"
        )
        for o in outcomes:
            sym = f"/{o.chunk.symbol}" if o.chunk.symbol else ""
            line = (
                f"  {o.result.status:8s} {o.chunk.dataset_code}{sym} "
                f"{o.chunk.range_start}..{o.chunk.range_end} "
                f"({o.result.rows_written} rows)"
            )
            if o.result.error:
                line += f" — {o.result.error}"
            print(line)

    await finmind_engine.dispose()
    return 0


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    os.chdir(_BACKEND_DIR)
    main()
