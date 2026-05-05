"""Diff a Phase B self-crawl connector against the live FinMind path.

Pre-cutover sanity check: BEFORE flipping `dataset_sources.active_source`
on a dataset, run this to confirm that the self-crawl connector returns
"close enough" data. Calls `resolve_client('finmind').fetch(...)` and
`resolve_client(<target>).fetch(...)` over the same window, then
reports a side-by-side summary.

Usage (from `backend/`):

    # One dataset, default 30-day window, default fallback source
    python -m finmind.scripts.dry_run_cutover --dataset TaiwanStockPrice \\
        --symbol 2330

    # Specific window + target source
    python -m finmind.scripts.dry_run_cutover --dataset TaiwanStockPER \\
        --source twse --start 2025-01-01 --end 2025-01-31

    # Walk every dataset whose fallback_source has a handler — full
    # cutover rehearsal. Stops on first hard failure unless --keep-going.
    python -m finmind.scripts.dry_run_cutover --all --days 7

Output: per-dataset markdown row with row-count delta + first/last
date overlap. Row-level value diff is intentionally NOT done here
(connectors mostly differ on column unit conventions and empty-string
vs None — that's exactly what `mappings.py` row_transform already
handles, so a value-level diff would be noisy and not actionable).

Exit codes:
    0 = every dataset's row-count delta is within tolerance
    1 = at least one dataset failed or exceeded the delta tolerance
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


@dataclass
class DiffOutcome:
    dataset: str
    source: str
    finmind_rows: int | None
    selfcrawl_rows: int | None
    finmind_error: str | None
    selfcrawl_error: str | None
    delta_pct: float | None
    note: str

    def status(self, tolerance_pct: float) -> str:
        """Markdown-friendly status letter for the report.

        FAIL = self-crawl side errored out (the only thing the operator
        actually needs to fix before flipping). WARN = anything that
        deserves a second look but doesn't block: FinMind side errored
        (paywall expired? quota burned?), no overlap to compare, or
        row-count delta exceeded `tolerance_pct`. OK = within tolerance.
        """
        if self.selfcrawl_error:
            return "FAIL"
        if self.finmind_error:
            return "WARN"
        if self.delta_pct is None:
            return "WARN"
        if abs(self.delta_pct) > tolerance_pct:
            return "WARN"
        return "OK"


async def _fetch(source: str, dataset: str, symbol: str | None,
                 start: date, end: date) -> tuple[int | None, str | None]:
    """Returns (row_count, error_str). Either field is None when the
    other is set."""
    from finmind.ingest.selfcrawl import resolve_client

    try:
        client = resolve_client(source)
    except KeyError as exc:
        return None, f"KeyError: {exc}"

    try:
        rows = await client.fetch(dataset, symbol, start, end)
    except NotImplementedError as exc:
        return None, f"NotImplementedError: {exc}"
    except Exception as exc:
        return None, f"{exc.__class__.__name__}: {exc}"

    return len(rows), None


async def _diff_one(
    dataset: str, source: str, symbol: str | None,
    start: date, end: date,
) -> DiffOutcome:
    fm_rows, fm_err = await _fetch("finmind", dataset, symbol, start, end)
    sc_rows, sc_err = await _fetch(source,  dataset, symbol, start, end)

    delta: float | None = None
    if fm_rows is not None and sc_rows is not None and fm_rows > 0:
        delta = (sc_rows - fm_rows) / fm_rows * 100.0
    elif fm_rows == 0 and sc_rows == 0:
        delta = 0.0  # Both empty — no Phase B regression.

    note = ""
    if fm_rows == 0 and (sc_rows or 0) > 0:
        note = "FinMind empty, self-crawl populated — possible quota/paywall recovery"
    elif (fm_rows or 0) > 0 and sc_rows == 0:
        note = "self-crawl returned 0 rows — investigate before flipping"

    return DiffOutcome(
        dataset=dataset,
        source=source,
        finmind_rows=fm_rows,
        selfcrawl_rows=sc_rows,
        finmind_error=fm_err,
        selfcrawl_error=sc_err,
        delta_pct=delta,
        note=note,
    )


def _resolve_target_source(dataset: str) -> str | None:
    from finmind.dataset_catalog import all_entries

    for _, entry in all_entries():
        if entry.dataset_code == dataset:
            return entry.fallback_source
    return None


def _enumerate_all_datasets() -> list[tuple[str, str]]:
    """(dataset_code, source) for every dataset whose fallback_source
    has a registered self-crawl handler today. Stub sources are
    skipped — diffing FinMind vs `_NotWiredYetClient` would only ever
    report FAIL, which adds no signal."""
    from finmind.dataset_catalog import all_entries
    from finmind.ingest.selfcrawl import supported_datasets

    out: list[tuple[str, str]] = []
    for _, entry in all_entries():
        src = entry.fallback_source
        if not src:
            continue
        if entry.dataset_code in supported_datasets(src):
            out.append((entry.dataset_code, src))
    return out


def _print_header() -> None:
    print(
        "| status | dataset | source | finmind | self-crawl | Δ% | note |\n"
        "|--------|---------|--------|---------|------------|------|------|"
    )


def _print_row(o: DiffOutcome, tolerance_pct: float) -> None:
    fm = (
        o.finmind_error[:40] + "…"
        if o.finmind_error and len(o.finmind_error) > 41
        else (o.finmind_error or str(o.finmind_rows))
    )
    sc = (
        o.selfcrawl_error[:40] + "…"
        if o.selfcrawl_error and len(o.selfcrawl_error) > 41
        else (o.selfcrawl_error or str(o.selfcrawl_rows))
    )
    delta = "—" if o.delta_pct is None else f"{o.delta_pct:+.1f}%"
    print(
        f"| {o.status(tolerance_pct)} | {o.dataset} | {o.source} "
        f"| {fm} | {sc} | {delta} | {o.note} |"
    )


async def amain() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--dataset", help="Single dataset code")
    parser.add_argument("--source", default=None,
                        help="Override target source (default: catalog fallback)")
    parser.add_argument("--symbol", default=None,
                        help="Symbol for per-symbol datasets")
    parser.add_argument("--start", default=None,
                        help="Range start YYYY-MM-DD (default: --days ago)")
    parser.add_argument("--end", default=None,
                        help="Range end YYYY-MM-DD (default: today)")
    parser.add_argument("--days", type=int, default=30,
                        help="Days back from --end when --start omitted (default: 30)")
    parser.add_argument(
        "--all", action="store_true",
        help=(
            "Walk every dataset whose fallback_source has a handler. "
            "Mutually exclusive with --dataset."
        ),
    )
    parser.add_argument(
        "--keep-going", action="store_true",
        help=(
            "With --all, keep diffing even when one dataset fails. "
            "Default is to stop on first hard error so a broken "
            "connector doesn't waste API quota for the rest."
        ),
    )
    parser.add_argument(
        "--tolerance", type=float, default=5.0,
        help=(
            "Acceptable |Δ%%| in row count between FinMind and self-"
            "crawl. Datasets exceeding this are reported as WARN and "
            "the script exits non-zero. Default: 5%%."
        ),
    )
    args = parser.parse_args()

    if args.all and args.dataset:
        parser.error("--all and --dataset are mutually exclusive")
    if not args.all and not args.dataset:
        parser.error("specify --dataset X or --all")

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
        force=True,
    )

    end = _parse_date(args.end) if args.end else datetime.now(tz=timezone.utc).date()
    start = (
        _parse_date(args.start)
        if args.start
        else end - timedelta(days=args.days)
    )

    if args.all:
        targets = _enumerate_all_datasets()
        if not targets:
            print(
                "dry_run_cutover: no datasets have a wired-up fallback "
                "source. Wire up Phase B handlers first.",
                file=sys.stderr,
            )
            return 1
    else:
        source = args.source or _resolve_target_source(args.dataset)
        if source is None:
            print(
                f"dry_run_cutover: dataset {args.dataset} has no "
                f"fallback_source — pass --source X explicitly.",
                file=sys.stderr,
            )
            return 1
        targets = [(args.dataset, source)]

    print(f"# Phase B cutover dry-run — window {start}..{end}\n")
    _print_header()

    bad = 0
    for dataset, source in targets:
        outcome = await _diff_one(
            dataset, source, args.symbol, start, end,
        )
        _print_row(outcome, args.tolerance)
        if outcome.status(args.tolerance) != "OK":
            bad += 1
            if args.all and not args.keep_going:
                print(
                    f"\ndry_run_cutover: stopping at first non-OK "
                    f"({dataset}). Pass --keep-going to continue.",
                    file=sys.stderr,
                )
                return 1

    print(
        f"\ndry_run_cutover: {len(targets) - bad} OK / {bad} non-OK "
        f"out of {len(targets)} datasets."
    )
    return 0 if bad == 0 else 1


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    os.chdir(_BACKEND_DIR)
    main()
