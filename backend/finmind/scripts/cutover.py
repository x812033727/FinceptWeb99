"""Phase A' cutover — flip Taiwan datasets from FinMind to direct
self-crawl and enable them, safely and idempotently.

Goal G2 ("完全取代 FinMind"): instead of paying the FinMind subscription,
fetch every dataset that has a working self-crawl connector straight
from the upstream (TWSE / MOPS / TAIFEX / TDCC / FRED). This script is
the one guarded command that performs the routing flip on
`dataset_sources`:

    for each dataset whose catalog `fallback_source` has a real handler
    (`covers_dataset(fallback_source, code)`):
        active_source ← fallback_source
        enabled       ← True

It is the operator entry point for the live cutover; the actual history
backfill (`deep_backfill`) and the daily `run_due` cron are separate
steps (see docs/finmind-cutover-runbook.md).

Usage (from `backend/`):

    python -m finmind.scripts.cutover                 # dry-run (default)
    python -m finmind.scripts.cutover --commit        # apply
    python -m finmind.scripts.cutover --source twse   # one source only
    python -m finmind.scripts.cutover --dataset TaiwanStockPrice --commit
    python -m finmind.scripts.cutover --json           # machine-readable plan

SAFETY:
  - Dry-run by default; nothing is written without --commit.
  - Every flip is re-checked with `covers_dataset(target, code)` right
    before the write — a dataset whose connector can't serve it is
    skipped (never flipped into a runtime NotImplementedError), matching
    the AdminPage flip gatekeeper.
  - Idempotent: a dataset already on its target source + enabled is
    reported as "unchanged" and re-running is a no-op.
  - Only datasets with a built destination table (`local_table != ""`)
    are eligible — a flip with nowhere to persist is pointless.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from finmind.dataset_catalog import all_entries  # noqa: E402
from finmind.db.session import (  # noqa: E402
    FinmindAsyncSessionLocal,
    finmind_engine,
)
from finmind.ingest.selfcrawl import covers_dataset  # noqa: E402
from finmind.models.dataset_source import DatasetSource  # noqa: E402


@dataclass
class CutoverItem:
    """One dataset's cutover decision."""

    dataset_code: str
    category: str
    target_source: str          # = catalog fallback_source
    local_table: str
    current_source: str         # active_source in the DB now
    currently_enabled: bool
    action: str                 # "flip" | "unchanged" | "skip"
    reason: str = ""            # populated for "skip"


def _eligible_targets() -> list[tuple[str, str, str, str]]:
    """(category, dataset_code, target_source, local_table) for every
    catalog entry that has a working self-crawl connector for its
    declared `fallback_source` and a built destination table.

    This is exactly the set the AdminPage flip gate would accept — we
    reuse `covers_dataset` so the two never disagree."""
    out: list[tuple[str, str, str, str]] = []
    for cat, e in all_entries():
        if not e.fallback_source:
            continue
        if not e.local_table:
            continue
        if not covers_dataset(e.fallback_source, e.dataset_code):
            continue
        out.append((cat, e.dataset_code, e.fallback_source, e.local_table))
    return out


async def plan_cutover(
    session: AsyncSession,
    *,
    only_dataset: str | None = None,
    only_source: str | None = None,
) -> list[CutoverItem]:
    """Compute the per-dataset cutover plan without writing anything."""
    targets = _eligible_targets()
    if only_source:
        targets = [t for t in targets if t[2] == only_source]
    if only_dataset:
        targets = [t for t in targets if t[1] == only_dataset]

    # One query for the current rows we care about.
    codes = [code for _, code, _, _ in targets]
    rows = {
        r.dataset_code: r
        for r in (
            await session.execute(
                select(DatasetSource).where(
                    DatasetSource.dataset_code.in_(codes)
                )
            )
        ).scalars()
    }

    items: list[CutoverItem] = []
    for cat, code, target, table in targets:
        row = rows.get(code)
        if row is None:
            items.append(
                CutoverItem(
                    dataset_code=code,
                    category=cat,
                    target_source=target,
                    local_table=table,
                    current_source="<missing>",
                    currently_enabled=False,
                    action="skip",
                    reason="not seeded in dataset_sources (run init_db)",
                )
            )
            continue

        # Defence in depth: re-verify coverage at plan time. `targets`
        # already filtered on it, but keep the guard explicit so a future
        # refactor can't silently drop it.
        if not covers_dataset(target, code):
            items.append(
                CutoverItem(
                    dataset_code=code,
                    category=cat,
                    target_source=target,
                    local_table=table,
                    current_source=row.active_source,
                    currently_enabled=bool(row.enabled),
                    action="skip",
                    reason=f"{target} has no handler for {code}",
                )
            )
            continue

        already = row.active_source == target and bool(row.enabled)
        items.append(
            CutoverItem(
                dataset_code=code,
                category=cat,
                target_source=target,
                local_table=table,
                current_source=row.active_source,
                currently_enabled=bool(row.enabled),
                action="unchanged" if already else "flip",
            )
        )
    return items


async def apply_cutover(
    session: AsyncSession, items: list[CutoverItem]
) -> int:
    """Apply the `flip` items to the DB. Returns the number of rows
    changed. Caller is responsible for committing."""
    changed = 0
    for item in items:
        if item.action != "flip":
            continue
        row = await session.get(DatasetSource, item.dataset_code)
        if row is None:  # raced with a delete — skip
            continue
        row.active_source = item.target_source
        row.enabled = True
        changed += 1
    return changed


def _render(items: list[CutoverItem]) -> str:
    flips = [i for i in items if i.action == "flip"]
    unchanged = [i for i in items if i.action == "unchanged"]
    skips = [i for i in items if i.action == "skip"]

    lines: list[str] = []
    lines.append("═══ FinMind Phase A' Cutover Plan ═══")
    lines.append(
        f"eligible={len(items)}  flip={len(flips)}  "
        f"unchanged={len(unchanged)}  skip={len(skips)}"
    )
    lines.append("")
    if flips:
        lines.append(f"WILL FLIP ({len(flips)}):")
        for i in sorted(flips, key=lambda x: (x.target_source, x.dataset_code)):
            lines.append(
                f"  {i.target_source:9} {i.dataset_code:50} "
                f"{i.current_source} → {i.target_source} "
                f"(enabled {i.currently_enabled}→True)"
            )
        lines.append("")
    if unchanged:
        lines.append(f"unchanged ({len(unchanged)}): already on target + enabled")
        lines.append("")
    if skips:
        lines.append(f"SKIPPED ({len(skips)}):")
        for i in skips:
            lines.append(f"  {i.dataset_code:50} {i.reason}")
        lines.append("")
    return "\n".join(lines)


async def amain() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Apply the flips. Without this the script is a dry-run.",
    )
    parser.add_argument(
        "--dataset",
        default=None,
        help="Only consider this dataset_code.",
    )
    parser.add_argument(
        "--source",
        default=None,
        help="Only consider datasets whose target source is this "
        "(twse/mops/taifex/tdcc/fred).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the plan as JSON instead of human-readable text.",
    )
    args = parser.parse_args()

    async with FinmindAsyncSessionLocal() as session:
        items = await plan_cutover(
            session,
            only_dataset=args.dataset,
            only_source=args.source,
        )

        if args.json:
            print(json.dumps([asdict(i) for i in items], ensure_ascii=False, indent=2))
        else:
            print(_render(items))

        flips = [i for i in items if i.action == "flip"]
        if args.commit and flips:
            changed = await apply_cutover(session, items)
            await session.commit()
            if not args.json:
                print(f"✓ committed — {changed} dataset(s) flipped + enabled.")
        elif flips and not args.json:
            print(
                f"(dry-run — pass --commit to flip {len(flips)} dataset(s). "
                f"Nothing written.)"
            )

    await finmind_engine.dispose()
    return 0


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    os.chdir(_BACKEND_DIR)
    main()
