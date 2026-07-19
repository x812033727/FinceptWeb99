"""Probe a Phase B self-crawl connector without writing to the DB.

Each new `selfcrawl/{source}.py` handler must produce dicts with the
same keys FinMind returns so the existing
`mappings.py:column_map / row_transform / batch_transform` pipeline
still works after a Phase A → B flip. This CLI calls a connector's
`fetch()` directly, prints the row count + first/last sample, and
exits non-zero on `NotImplementedError` / blank result — making it
the standard "ready-to-merge" check before opening a flip PR.

Usage (from `backend/`):

    # Probe TWSE for a single symbol-month
    python -m finmind.scripts.probe_source \\
        --dataset TaiwanStockPrice --source twse \\
        --symbol 2330 --start 2024-01-01 --end 2024-01-31

    # Default source = catalog's fallback_source (Phase B target)
    python -m finmind.scripts.probe_source \\
        --dataset TaiwanStockMonthRevenue --symbol 2330 \\
        --start 2024-01-01 --end 2024-12-31

Exit codes:
    0 = fetched ≥ 1 row
    1 = handler raised (typically NotImplementedError or HTTP error)
    2 = handler returned empty list (no rows in window — usually a
        weekend/holiday or wrong symbol; not always a bug, but worth
        a non-zero so CI can catch obvious typos)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from finmind.redaction import redact_exception  # noqa: E402


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _resolve_default_source(dataset_code: str) -> str | None:
    """When --source is omitted, pick the catalog's `fallback_source`
    so probing is "what would happen after the FinMind cutover" by
    default. Returns None for datasets without a Phase B target."""
    from finmind.dataset_catalog import all_entries

    for _, entry in all_entries():
        if entry.dataset_code == dataset_code:
            return entry.fallback_source
    return None


def _summarize_row(row: dict) -> str:
    """One-line preview — keep keys sorted for diffability across runs.
    Truncate long string values so a news headline doesn't blow out
    the terminal."""
    parts: list[str] = []
    for k in sorted(row.keys()):
        v = row[k]
        s = str(v)
        if len(s) > 60:
            s = s[:57] + "..."
        parts.append(f"{k}={s}")
    return ", ".join(parts)


async def amain() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--dataset", required=True,
        help="FinMind dataset_code (e.g. TaiwanStockPrice)",
    )
    parser.add_argument(
        "--source", default=None,
        help=(
            "Source to probe (twse|tpex|taifex|mops|tdcc|finmind). "
            "Defaults to the catalog's fallback_source so probing "
            "matches the Phase B cutover target."
        ),
    )
    parser.add_argument(
        "--symbol", default=None,
        help=(
            "Symbol for per-symbol datasets (e.g. 2330). Omit for "
            "market-wide datasets like TaiwanStockTotalInstitutional"
            "Investors."
        ),
    )
    parser.add_argument(
        "--start", default=None,
        help="Range start YYYY-MM-DD (default: --days ago)",
    )
    parser.add_argument(
        "--end", default=None,
        help="Range end YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--days", type=int, default=30,
        help="Days back from --end when --start is omitted (default: 30)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Dump every row as JSON Lines instead of head/tail summary",
    )
    parser.add_argument(
        "--quiet", "-q", action="store_true",
        help="Suppress logging output (only print summary + exit code)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
        force=True,
    )

    end = _parse_date(args.end) if args.end else datetime.now(tz=timezone.utc).date()
    start = (
        _parse_date(args.start)
        if args.start
        else end - timedelta(days=args.days)
    )

    source = args.source or _resolve_default_source(args.dataset)
    if source is None:
        print(
            f"probe: dataset {args.dataset} has no fallback_source in "
            f"the catalog — pass --source explicitly (e.g. --source "
            f"finmind to compare against the paid path).",
            file=sys.stderr,
        )
        return 1

    from finmind.ingest.selfcrawl import resolve_client, supported_datasets

    if source != "finmind" and args.dataset not in supported_datasets(source):
        print(
            f"probe: source='{source}' has no handler for dataset "
            f"'{args.dataset}'. Wire it up in finmind/ingest/selfcrawl/"
            f"{source}.py first, OR pass --source finmind to test the "
            f"paid path.",
            file=sys.stderr,
        )
        return 1

    try:
        client = resolve_client(source)
    except KeyError as exc:
        print(f"probe: {exc}", file=sys.stderr)
        return 1

    print(
        f"probe: source={source} dataset={args.dataset} "
        f"symbol={args.symbol or '-'} window={start}..{end}"
    )

    try:
        rows = await client.fetch(
            args.dataset, args.symbol, start, end,
        )
    except NotImplementedError as exc:
        print(f"probe: {redact_exception(exc)}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"probe: {redact_exception(exc)}", file=sys.stderr)
        return 1

    if not rows:
        print(
            "probe: handler returned 0 rows. Double-check the window "
            "(weekends / holidays return empty) or the symbol.",
            file=sys.stderr,
        )
        return 2

    print(f"probe: fetched {len(rows)} row(s)")

    if args.json:
        for r in rows:
            print(json.dumps(r, ensure_ascii=False, default=str))
    else:
        # head + tail preview keeps the terminal output bounded for
        # market-wide datasets (TaiwanStockInfo returns ~1700 rows).
        head = rows[:3]
        tail = rows[-3:] if len(rows) > 6 else []
        for i, r in enumerate(head):
            print(f"  [{i}] {_summarize_row(r)}")
        if tail:
            print(f"  ... ({len(rows) - len(head) - len(tail)} more) ...")
            for i, r in enumerate(tail, start=len(rows) - len(tail)):
                print(f"  [{i}] {_summarize_row(r)}")

    return 0


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    os.chdir(_BACKEND_DIR)
    main()
