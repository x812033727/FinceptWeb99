"""Rank FinMind datasets by upstream API spend.

Reads the per-day `finmind:upstream:usage:{YYYYMMDD}` Redis hashes that
`data.tw.finmind_connector` writes (one field per dataset_code, value =
outbound calls that day) and aggregates the last N days into a ranked
table. This is the W0 input that drives the self-crawl cutover priority
order: the datasets burning the most of the 6000/hr FinMind budget get
their Phase B connectors wired first.

Usage (from `backend/`):

    python -m finmind.scripts.usage_report                # last 14 days
    python -m finmind.scripts.usage_report --days 7       # last 7 days
    python -m finmind.scripts.usage_report --json         # machine-readable

Read-only. Only touches Redis (via `cache.redis_cache`) — no DB session,
no FinMind calls. Safe to run against production at any time.

Note: the counters start accumulating only once the instrumented
connector is deployed, so the first useful report is ~1-2 weeks out.
An empty report means either no FinMind traffic in the window or the
instrumentation hasn't been running long enough yet.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from cache.redis_cache import (  # noqa: E402
    cache_hgetall,
    key_finmind_dataset_usage,
)


def _day_keys(days: int, today: datetime) -> list[str]:
    """UTC `YYYYMMDD` stamps for the inclusive `[today-days+1, today]`
    window, newest first. `today` is injected so the caller controls
    the clock (and tests stay deterministic)."""
    return [
        (today - timedelta(days=n)).strftime("%Y%m%d")
        for n in range(max(days, 1))
    ]


async def collect_usage(days: int, today: datetime) -> dict[str, int]:
    """Sum per-dataset call counts across the last `days` day-hashes.
    Returns `{dataset_code: total_calls}`. Malformed field values (a
    hash that somehow holds a non-int) are skipped rather than crashing
    the whole report."""
    totals: dict[str, int] = {}
    for day in _day_keys(days, today):
        hashed = await cache_hgetall(key_finmind_dataset_usage(day))
        for dataset, raw in hashed.items():
            try:
                totals[dataset] = totals.get(dataset, 0) + int(raw)
            except (TypeError, ValueError):
                continue
    return totals


def _render_table(totals: dict[str, int], days: int) -> str:
    if not totals:
        return (
            f"# FinMind upstream usage — last {days} day(s)\n\n"
            "(no usage recorded — instrumentation may not have been "
            "running long enough, or there was no FinMind traffic)"
        )
    grand = sum(totals.values()) or 1
    ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    lines = [
        f"# FinMind upstream usage — last {days} day(s)",
        f"# total calls: {grand}  |  distinct datasets: {len(totals)}",
        "",
        "| rank | dataset | calls | % of total |",
        "|------|---------|-------|------------|",
    ]
    for i, (dataset, calls) in enumerate(ranked, start=1):
        lines.append(
            f"| {i} | {dataset} | {calls} | {calls / grand * 100:.1f}% |"
        )
    return "\n".join(lines)


async def amain() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n", 1)[0],
    )
    parser.add_argument(
        "--days", type=int, default=14,
        help="Number of trailing UTC days to aggregate (default: 14).",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit machine-readable JSON instead of the markdown table.",
    )
    args = parser.parse_args()

    today = datetime.now(tz=timezone.utc)
    totals = await collect_usage(args.days, today)

    if args.json:
        ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
        grand = sum(totals.values())
        print(json.dumps({
            "days": args.days,
            "total_calls": grand,
            "datasets": [
                {
                    "dataset": d,
                    "calls": c,
                    "pct": round(c / grand * 100, 2) if grand else 0.0,
                }
                for d, c in ranked
            ],
        }, indent=2))
    else:
        print(_render_table(totals, args.days))
    return 0


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
