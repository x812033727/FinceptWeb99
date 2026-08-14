"""Operator tool for the event-window handling clause (2026-08 miss
review).

Same archive-then-mutate contract as `apply_veto_downgrade`: the
original rules text is written to the archive directory before any DB
change, so revert is always possible even without this script. The
archive-dir resolution and stamp helpers are imported from that script
rather than duplicated — one behaviour, one implementation.

Usage (inside the backend container or a host venv with DB access):

    python -m scripts.apply_event_window_clause            # show
    python -m scripts.apply_event_window_clause --apply
    python -m scripts.apply_event_window_clause --revert
"""
from __future__ import annotations

import argparse
import asyncio
import os
from datetime import UTC, datetime

from sqlalchemy import select

from scripts.apply_veto_downgrade import (
    _default_probe,
    archive_stamp,
    resolve_archive_dir,
)
from services.event_window_clause import EVENT_WINDOW_CLAUSE

__all__ = [
    "EVENT_WINDOW_CLAUSE",
    "apply_clause",
    "revert_clause",
    "main",
]


def apply_clause(rules: str) -> str:
    if EVENT_WINDOW_CLAUSE in rules:
        return rules
    return rules + EVENT_WINDOW_CLAUSE


def revert_clause(rules: str) -> str:
    return rules.replace(EVENT_WINDOW_CLAUSE, "")


async def main(mode: str, force_without_archive: bool) -> None:
    from db.session import AsyncSessionLocal
    from models.discussion_auto_run_config import DiscussionAutoRunConfig

    archive = resolve_archive_dir(os.environ, _default_probe)
    if mode != "show":
        print(f"archive dir resolved to: {archive}")

    async with AsyncSessionLocal() as db:
        cfgs = (await db.scalars(select(DiscussionAutoRunConfig))).all()
        for cfg in cfgs:
            rules = cfg.rules or ""
            present = EVENT_WINDOW_CLAUSE in rules
            print(f"config user={cfg.user_id} clause_present={present}")
            if mode == "show":
                continue

            new_rules = (
                apply_clause(rules) if mode == "apply" else revert_clause(rules)
            )
            if new_rules == rules:
                print("no-op (already in desired state)")
                continue

            stamp = archive_stamp(datetime.now(UTC))
            try:
                archive.mkdir(parents=True, exist_ok=True)
                archive_file = archive / f"rules-{cfg.user_id}-{stamp}.txt"
                archive_file.write_text(rules, encoding="utf-8")
                print(
                    f"--- archived original ({len(rules)} chars) "
                    f"to {archive_file} ---"
                )
            except Exception as e:
                print(f"!!! archive write failed: {e}")
                print(f"--- original text ({len(rules)} chars) ---")
                print(rules)
                print("--- end original text ---")
                if not force_without_archive:
                    print(
                        "Pass --force-without-archive to proceed "
                        "without archive"
                    )
                    continue

            cfg.rules = new_rules
            await db.commit()
            print(f"{mode} done for user={cfg.user_id}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--apply", action="store_true")
    g.add_argument("--revert", action="store_true")
    ap.add_argument("--force-without-archive", action="store_true",
                    help="proceed with DB update even if archive write fails")
    args = ap.parse_args()
    mode = "apply" if args.apply else "revert" if args.revert else "show"
    asyncio.run(main(mode, args.force_without_archive))
