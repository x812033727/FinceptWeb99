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
that sweep. Exit status is 0 when no chunk fails (including nothing due),
1 when one or more chunks fail, and 2 when the runner itself crashes.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[2]
_TAIWAN_DATASET_PREFIXES = ("Taiwan", "taiwan_")
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from finmind.db.session import (  # noqa: E402
    FinmindAsyncSessionLocal,
    finmind_engine,
)
from finmind.redaction import redact_exception, redact_secret_text  # noqa: E402
from finmind.scheduler.runner import (  # noqa: E402
    get_crypto_universe,
    get_universe_from_tw_stock_info,
    get_warrant_universe_from_tw_stock_info,
    run_due_now,
)
from services.ingest.repository import record_health  # noqa: E402
from services.ingest_schedules import (  # noqa: E402
    FINMIND_TW_MARKETWIDE_JOB_ID as TW_MARKETWIDE_JOB_ID,
)

EXIT_OK = 0
EXIT_CHUNK_FAILURE = 1
EXIT_RUNNER_CRASH = 2
_MAX_FAILED_DATASET_CODES = 5
_MAX_HEALTH_ERROR_LENGTH = 1000


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


def _failed_health_summary(outcomes) -> str | None:
    """Build a bounded, secret-safe summary for the Admin health row."""
    failed = [o for o in outcomes if o.result.status == "failed"]
    if not failed:
        return None

    dataset_codes = list(dict.fromkeys(o.chunk.dataset_code for o in failed))
    visible_codes = dataset_codes[:_MAX_FAILED_DATASET_CODES]
    hidden_count = len(dataset_codes) - len(visible_codes)
    code_summary = ", ".join(visible_codes)
    if hidden_count:
        code_summary += f" (+{hidden_count} more)"

    first_error = redact_secret_text(failed[0].result.error) or "unknown error"
    summary = (
        f"{len(failed)} failed chunk(s): {code_summary}; "
        f"first error: {first_error}"
    )
    return summary[:_MAX_HEALTH_ERROR_LENGTH]


def classify_run_outcome(outcomes) -> tuple[bool, str | None]:
    """(ok, error_summary) for the market-wide health record.

    The naive rule `ok = no failed chunks` inverts the signal at both
    ends: a run writing 15k rows with one timed-out chunk of ~30 read
    as `failed`, while an idle run (nothing due) read as a clean `ok`
    indistinguishable from real work. Partial success is success —
    with the failure summary carried in `error` so the dashboard still
    shows what went wrong — and idleness is labeled so a
    zero-row `ok` can never masquerade as a productive run.
    """
    if not outcomes:
        return True, "idle: nothing due"
    summary = _failed_health_summary(outcomes)
    if summary is None:
        return True, None
    rows = sum(o.result.rows_written for o in outcomes)
    if rows > 0:
        return True, f"partial: {summary}"
    return False, summary


async def _record_tw_marketwide_health(
    *, ok: bool, row_count: int, error: str | None = None,
) -> None:
    """Best-effort bridge from the host cron into Admin Ingest Health."""
    try:
        await record_health(
            TW_MARKETWIDE_JOB_ID,
            ok=ok,
            row_count=row_count,
            error=error,
        )
    except Exception as exc:
        logging.getLogger(__name__).error(
            "run_due: could not record Taiwan market-wide health: %s",
            redact_exception(exc),
        )


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

    outcomes = None
    runner_error: BaseException | None = None
    try:
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
                now=datetime.now(tz=UTC),
            )
    except Exception as exc:
        runner_error = exc
    finally:
        try:
            await finmind_engine.dispose()
        except Exception as exc:
            if runner_error is None:
                runner_error = exc

    if runner_error is not None:
        safe_error = redact_exception(runner_error)
        error_summary = f"runner crash: {safe_error}"[:_MAX_HEALTH_ERROR_LENGTH]
        if args.tw_only:
            await _record_tw_marketwide_health(
                ok=False,
                row_count=0,
                error=error_summary,
            )
        print(f"run_due: {error_summary}", file=sys.stderr)
        return EXIT_RUNNER_CRASH

    assert outcomes is not None

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
                safe_error = redact_secret_text(o.result.error) or "unknown error"
                line += f" — {safe_error}"
            print(line)

    rows = sum(o.result.rows_written for o in outcomes)
    ok, health_error = classify_run_outcome(outcomes)
    if args.tw_only:
        await _record_tw_marketwide_health(
            ok=ok,
            row_count=rows,
            error=health_error,
        )
    return EXIT_OK if ok else EXIT_CHUNK_FAILURE


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    os.chdir(_BACKEND_DIR)
    main()
