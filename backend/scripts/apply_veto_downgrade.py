"""Operator tool for the macro-veto downgrade clause (spec Part 1).

Adoption is GATED on the spec's pre-registered criteria — running
--apply is the human act of adoption, after the experiment grades land
in the criteria table. The original text is printed AND written to an
archive directory before any change, so revert is always possible even
without this script.

The archive directory is NOT always `docs/rules-archive` under the
container's own tree: WORKDIR /app there is not writable by `appuser`,
so a bare `docs/rules-archive` fails a PermissionError exactly when
--revert needs it most. `resolve_archive_dir` below picks, in order: an
explicit `VETO_ARCHIVE_DIR` override, the `/host-trigger` bind mount
(container case — mounted from /opt/finceptweb99/var, writable), then
`docs/rules-archive` (host-run case, e.g. run directly on the host
rather than in the container).
"""
from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

# Re-exported for back-compat: the clause's canonical home is
# services.veto_clause (both this script and tasks.monitor_strategy_
# health need it, and a task importing from scripts would be
# backwards) but this script's own tests and callers still do
# `from scripts.apply_veto_downgrade import VETO_DOWNGRADE_CLAUSE`.
from services.veto_clause import VETO_DOWNGRADE_CLAUSE

__all__ = [
    "VETO_DOWNGRADE_CLAUSE",
    "apply_clause",
    "revert_clause",
    "archive_stamp",
    "resolve_archive_dir",
    "main",
]


def apply_clause(rules: str) -> str:
    if VETO_DOWNGRADE_CLAUSE in rules:
        return rules
    return rules + VETO_DOWNGRADE_CLAUSE


def revert_clause(rules: str) -> str:
    return rules.replace(VETO_DOWNGRADE_CLAUSE, "")


def archive_stamp(now: datetime) -> str:
    """Format a datetime as an ISO-like archive timestamp.

    Args:
        now: A timezone-aware datetime (preferably UTC).

    Returns:
        Formatted string like "20260725T120000Z".
    """
    return now.strftime("%Y%m%dT%H%M%SZ")


def _default_probe(path: Path) -> bool:
    """Real writability check: the directory must exist and accept
    writes from this process (os.access, not just an existence check —
    the container mounts plenty of paths read-only)."""
    return path.exists() and os.access(path, os.W_OK)


def resolve_archive_dir(
    env: Mapping[str, str], probe: Callable[[Path], bool],
) -> Path:
    """Pick the archive directory: an explicit override first, then the
    container's writable bind mount, then the host-run fallback.

    `probe` is injected so tests can fake writability without touching
    the real filesystem — the real check (`_default_probe`) hits
    `Path.exists` + `os.access`, neither of which is something a unit
    test should depend on the sandbox actually having set up.
    """
    override = env.get("VETO_ARCHIVE_DIR")
    if override:
        return Path(override)
    host_trigger = Path("/host-trigger")
    if probe(host_trigger):
        return host_trigger / "rules-archive"
    return Path("docs/rules-archive")


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
            present = VETO_DOWNGRADE_CLAUSE in rules
            print(f"config user={cfg.user_id} clause_present={present}")
            if mode == "show":
                continue

            # Compute new_rules first; skip archive for no-ops
            new_rules = (
                apply_clause(rules) if mode == "apply" else revert_clause(rules)
            )
            if new_rules == rules:
                print("no-op (already in desired state)")
                continue

            # Attempt archive before any DB change (only for real mutations)
            stamp = archive_stamp(datetime.now(UTC))
            try:
                archive.mkdir(parents=True, exist_ok=True)
                archive_file = archive / f"rules-{cfg.user_id}-{stamp}.txt"
                archive_file.write_text(rules, encoding="utf-8")
                print(f"--- archived original ({len(rules)} chars) to {archive_file} ---")
            except Exception as e:
                print(f"!!! archive write failed: {e}")
                print(f"--- original text ({len(rules)} chars) ---")
                print(rules)
                print("--- end original text ---")
                if not force_without_archive:
                    print("Pass --force-without-archive to proceed without archive")
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
