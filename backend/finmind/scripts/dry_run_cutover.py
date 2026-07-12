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

    # Value-level diff for a dataset that has a compare_spec: join both
    # sources on the key columns and check each value column within
    # tolerance (coverage + per-column mismatch rate).
    python -m finmind.scripts.dry_run_cutover --dataset TaiwanStockPrice \\
        --symbol 2330 --values

Default output: per-dataset markdown row with row-count delta + first/
last date overlap. This row-count check stays the fast default because
connectors often differ on column unit conventions and empty-string vs
None, which `mappings.py` row_transform normalises away.

`--values` opts into a stricter, per-column value comparison for
datasets that declare a `compare_spec` in `mappings/_registry.py`
(FinMind raw column names, join keys + tolerances). Use it before
flipping high-value datasets (price / institutional / margin) where a
matching row COUNT isn't enough assurance — you want the numbers to
agree too. Datasets without a compare_spec report as SKIP.

Exit codes:
    0 = every dataset within tolerance (row-count, or value in --values)
    1 = at least one dataset failed or exceeded the tolerance
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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from finmind.ingest.mappings._types import CompareSpec

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


# ── Value-level comparison (--values mode) ───────────────────────


def _coerce_float(v: object) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _values_match(a: object, b: object, kind: str, tol: float) -> bool | None:
    """Compare one cell across the two sources. Returns True/False, or
    None when the pair is *incomparable* (both sides missing the value)
    so the caller can exclude it from the mismatch-rate denominator
    rather than scoring it as a spurious match."""
    if kind == "exact":
        sa = "" if a is None else str(a).strip()
        sb = "" if b is None else str(b).strip()
        if sa == "" and sb == "":
            return None
        return sa == sb

    fa, fb = _coerce_float(a), _coerce_float(b)
    if fa is None and fb is None:
        return None  # both empty — nothing to compare
    if fa is None or fb is None:
        return False  # one side has a value, the other doesn't → mismatch
    if kind == "abs":
        return abs(fa - fb) <= tol
    # "rel": relative error against the larger magnitude; both-zero is a
    # match (0 vs 0 with any tol).
    denom = max(abs(fa), abs(fb))
    if denom == 0:
        return True
    return abs(fa - fb) / denom <= tol


def _key_of(row: dict, key_cols: tuple[str, ...]) -> tuple:
    return tuple(str(row.get(c, "")) for c in key_cols)


@dataclass
class ValueDiff:
    dataset: str
    source: str
    finmind_keys: int
    selfcrawl_keys: int
    common_keys: int
    # column -> (mismatches, comparable_pairs)
    per_col: dict[str, tuple[int, int]]
    samples: list[str]
    error: str | None = None

    @property
    def coverage_pct(self) -> float:
        """Share of FinMind keys also present on the self-crawl side."""
        if self.finmind_keys == 0:
            return 100.0 if self.selfcrawl_keys == 0 else 0.0
        return self.common_keys / self.finmind_keys * 100.0

    def worst_mismatch_pct(self) -> float:
        worst = 0.0
        for mism, comp in self.per_col.values():
            if comp:
                worst = max(worst, mism / comp * 100.0)
        return worst

    def status(self, min_coverage_pct: float, max_mismatch_pct: float) -> str:
        if self.error:
            return "FAIL"
        if self.coverage_pct < min_coverage_pct:
            return "WARN"
        if self.worst_mismatch_pct() > max_mismatch_pct:
            return "WARN"
        return "OK"


def compare_values(
    dataset: str,
    source: str,
    fm_rows: list[dict],
    sc_rows: list[dict],
    spec: "CompareSpec",
    max_samples: int = 5,
) -> ValueDiff:
    """Join FinMind vs self-crawl raw rows on `spec.key_cols` and diff
    each `spec.value_cols` column within its tolerance. Pure function —
    no I/O — so it's directly unit-testable."""
    fm_idx = {_key_of(r, spec.key_cols): r for r in fm_rows}
    sc_idx = {_key_of(r, spec.key_cols): r for r in sc_rows}
    common = sorted(set(fm_idx) & set(sc_idx))

    per_col: dict[str, tuple[int, int]] = {
        col: (0, 0) for col, _, _ in spec.value_cols
    }
    samples: list[str] = []
    for key in common:
        fm_row, sc_row = fm_idx[key], sc_idx[key]
        for col, kind, tol in spec.value_cols:
            verdict = _values_match(
                fm_row.get(col), sc_row.get(col), kind, tol,
            )
            if verdict is None:
                continue
            mism, comp = per_col[col]
            per_col[col] = (mism + (0 if verdict else 1), comp + 1)
            if not verdict and len(samples) < max_samples:
                samples.append(
                    f"{key} {col}: finmind={fm_row.get(col)!r} "
                    f"selfcrawl={sc_row.get(col)!r}"
                )

    return ValueDiff(
        dataset=dataset,
        source=source,
        finmind_keys=len(fm_idx),
        selfcrawl_keys=len(sc_idx),
        common_keys=len(common),
        per_col=per_col,
        samples=samples,
    )


async def _fetch_rows(source: str, dataset: str, symbol: str | None,
                      start: date, end: date,
                      ) -> tuple[list[dict] | None, str | None]:
    """Returns (rows, error_str). Either field is None when the other is
    set. The row-count path wraps this; the `--values` path needs the
    rows themselves to diff column values."""
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

    return rows, None


async def _fetch(source: str, dataset: str, symbol: str | None,
                 start: date, end: date) -> tuple[int | None, str | None]:
    """Returns (row_count, error_str). Either field is None when the
    other is set."""
    rows, err = await _fetch_rows(source, dataset, symbol, start, end)
    if err is not None:
        return None, err
    return len(rows or []), None


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


def _resolve_compare_spec(dataset: str) -> "CompareSpec | None":
    """Look up the dataset's value-comparison recipe from the ingest
    mappings registry. Returns None when the dataset has no mapping or
    the mapping declares no `compare_spec` — in which case `--values`
    reports the dataset as SKIP (author a spec before trusting a value
    diff)."""
    from finmind.ingest.mappings._registry import MAPPINGS

    mapping = MAPPINGS.get(dataset)
    return mapping.compare_spec if mapping else None


async def _diff_values_one(
    dataset: str, source: str, symbol: str | None,
    start: date, end: date,
) -> "ValueDiff":
    spec = _resolve_compare_spec(dataset)
    if spec is None:
        return ValueDiff(
            dataset=dataset, source=source, finmind_keys=0,
            selfcrawl_keys=0, common_keys=0, per_col={}, samples=[],
            error="no compare_spec — author one in mappings/_registry.py",
        )

    fm_rows, fm_err = await _fetch_rows("finmind", dataset, symbol, start, end)
    sc_rows, sc_err = await _fetch_rows(source, dataset, symbol, start, end)
    if sc_err is not None:
        return ValueDiff(
            dataset=dataset, source=source, finmind_keys=0,
            selfcrawl_keys=0, common_keys=0, per_col={}, samples=[],
            error=f"self-crawl: {sc_err}",
        )
    if fm_err is not None:
        return ValueDiff(
            dataset=dataset, source=source, finmind_keys=0,
            selfcrawl_keys=len(sc_rows or []), common_keys=0,
            per_col={}, samples=[], error=f"finmind: {fm_err}",
        )

    return compare_values(dataset, source, fm_rows or [], sc_rows or [], spec)


def _print_values_report(
    diffs: list["ValueDiff"], min_coverage: float, max_mismatch: float,
) -> int:
    print(
        "| status | dataset | source | fm keys | sc keys | coverage | "
        "worst col mismatch |\n"
        "|--------|---------|--------|---------|---------|----------|"
        "--------------------|"
    )
    bad = 0
    detail_blocks: list[str] = []
    for d in diffs:
        st = d.status(min_coverage, max_mismatch)
        if st != "OK":
            bad += 1
        if d.error:
            print(
                f"| {st} | {d.dataset} | {d.source} | — | — | — | "
                f"{d.error[:40]} |"
            )
            continue
        print(
            f"| {st} | {d.dataset} | {d.source} | {d.finmind_keys} | "
            f"{d.selfcrawl_keys} | {d.coverage_pct:.1f}% | "
            f"{d.worst_mismatch_pct():.2f}% |"
        )
        if st != "OK" and (d.samples or d.per_col):
            lines = [f"\n### {d.dataset} ({d.source}) — mismatch detail"]
            for col, (mism, comp) in d.per_col.items():
                if comp:
                    lines.append(
                        f"- {col}: {mism}/{comp} mismatched "
                        f"({mism / comp * 100:.2f}%)"
                    )
            for s in d.samples:
                lines.append(f"  · {s}")
            detail_blocks.append("\n".join(lines))

    for block in detail_blocks:
        print(block)
    print(
        f"\ndry_run_cutover --values: {len(diffs) - bad} OK / {bad} "
        f"non-OK out of {len(diffs)} datasets."
    )
    return 0 if bad == 0 else 1


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
    parser.add_argument(
        "--values", action="store_true",
        help=(
            "Value-level diff instead of row-count only: join both "
            "sources on the dataset's compare_spec key columns and "
            "check each value column within tolerance. Datasets without "
            "a compare_spec are reported as SKIP."
        ),
    )
    parser.add_argument(
        "--min-coverage", type=float, default=98.0,
        help=(
            "With --values: minimum %% of FinMind keys that must also "
            "appear on the self-crawl side. Below this → WARN. "
            "Default: 98%%."
        ),
    )
    parser.add_argument(
        "--max-mismatch", type=float, default=1.0,
        help=(
            "With --values: maximum per-column value mismatch rate "
            "before WARN. Default: 1%%."
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

    if args.values:
        print(
            f"# Phase B cutover dry-run (values) — window {start}..{end}\n"
        )
        diffs: list[ValueDiff] = []
        for dataset, source in targets:
            diffs.append(
                await _diff_values_one(
                    dataset, source, args.symbol, start, end,
                )
            )
            if (
                diffs[-1].status(args.min_coverage, args.max_mismatch) != "OK"
                and args.all and not args.keep_going
            ):
                _print_values_report(diffs, args.min_coverage, args.max_mismatch)
                print(
                    f"\ndry_run_cutover: stopping at first non-OK "
                    f"({dataset}). Pass --keep-going to continue.",
                    file=sys.stderr,
                )
                return 1
        return _print_values_report(
            diffs, args.min_coverage, args.max_mismatch,
        )

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
