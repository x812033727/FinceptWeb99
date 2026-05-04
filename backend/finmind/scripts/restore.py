"""Restore the FinMind clone DB from a `pg_dump` archive.

Three safety rails distinguish this from a raw `pg_restore`:

  1. Refuse to overwrite data — by default, fails fast if the
     target DB already has rows in `dataset_sources` (the always-
     populated catalog table). Operator passes `--force` to
     accept the data loss.
  2. Version mismatch warning — parses the alembic head from the
     backup filename (`finmind_<ts>_<head>.dump` per backup.py)
     and warns when it differs from the target's current head.
     Restore still proceeds with `--force-version`; otherwise
     aborts so the operator can `alembic upgrade` first.
  3. Pre-flight `pg_restore --list` — confirms the dump is a valid
     archive before touching the target. Catches truncated /
     corrupt files before they damage the destination.

Usage (from `backend/`):

    # Restore latest backup into the configured FINMIND_DATABASE_URL
    python -m finmind.scripts.restore --input backups/finmind_<ts>_<head>.dump

    # Overwrite an existing populated DB (DESTRUCTIVE)
    python -m finmind.scripts.restore --input ... --force

    # Skip the version check (operator knows what they're doing)
    python -m finmind.scripts.restore --input ... --force-version
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from finmind.db.session import (  # noqa: E402
    FinmindAsyncSessionLocal,
    finmind_engine,
)
from finmind.scripts.backup import (  # noqa: E402
    get_alembic_head,
    parse_connection,
)

log = logging.getLogger("finmind.restore")


def parse_alembic_head_from_filename(path: Path) -> str | None:
    """`finmind_<YYYYMMDDhhmmss>_<head>.dump` → head, or None when
    the filename doesn't match the convention. Robust to operators
    renaming files manually — `--force-version` is the escape hatch."""
    m = re.match(r"finmind_\d{14}_([^.]+)\.dump$", path.name)
    return m.group(1) if m else None


async def is_db_populated(session) -> bool:
    """Catalog `dataset_sources` is the canonical "do we have data?"
    test — `init_db` always seeds it to 80 rows after migrations,
    and no other workflow touches it. Empty (or table missing) =
    safe to restore over."""
    from sqlalchemy import text

    try:
        result = await session.execute(
            text("SELECT COUNT(*) FROM dataset_sources")
        )
        return (result.scalar_one() or 0) > 0
    except Exception:
        return False


def verify_archive_listing(input_path: Path) -> int:
    """Same check as backup.py — `pg_restore --list` returns 0 on
    valid archives, non-zero on corrupt ones. Returns the TOC entry
    count on success."""
    if shutil.which("pg_restore") is None:
        raise FileNotFoundError(
            "pg_restore not on PATH — install postgresql-client."
        )
    result = subprocess.run(
        ["pg_restore", "--list", str(input_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return sum(
        1
        for line in result.stdout.splitlines()
        if line.strip() and not line.startswith(";")
    )


def run_pg_restore(
    conn: dict[str, str],
    input_path: Path,
    *,
    clean: bool = False,
) -> None:
    """Spawn pg_restore. `--clean` drops + recreates each object
    before loading — required when restoring over a populated DB.
    Without it, restore fails on existing tables / constraints."""
    if shutil.which("pg_restore") is None:
        raise FileNotFoundError(
            "pg_restore not on PATH — install postgresql-client."
        )

    argv = [
        "pg_restore",
        "--host", conn["host"],
        "--port", conn["port"],
        "--username", conn["user"],
        "--dbname", conn["database"],
        "--no-owner",
        "--no-privileges",
        "--single-transaction",  # all-or-nothing — partial restore
                                 # is worse than no restore.
    ]
    if clean:
        argv += ["--clean", "--if-exists"]
    argv += [str(input_path)]

    env = {**os.environ, "PGPASSWORD": conn["password"]}
    log.info(
        "running pg_restore from %s%s",
        input_path,
        " (clean)" if clean else "",
    )
    subprocess.run(argv, env=env, check=True)


async def amain() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="Path to a backup file (custom format, .dump).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Overwrite an already-populated target DB. Equivalent to "
            "pg_restore --clean --if-exists. DESTRUCTIVE."
        ),
    )
    parser.add_argument(
        "--force-version",
        action="store_true",
        help=(
            "Restore even when backup's alembic head differs from "
            "target's current head. Default behavior is to abort + "
            "ask the operator to alembic-upgrade first."
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
        force=True,
    )

    if not args.input.exists():
        print(f"restore: input file not found: {args.input}", file=sys.stderr)
        return 2

    # ── Pre-flight: dump file is valid + version matches ────
    try:
        n_objects = verify_archive_listing(args.input)
    except FileNotFoundError as exc:
        print(f"restore: {exc}", file=sys.stderr)
        return 3
    except subprocess.CalledProcessError as exc:
        print(
            f"restore: input file isn't a valid pg_restore archive "
            f"(exit {exc.returncode})",
            file=sys.stderr,
        )
        return 4
    log.info("pre-flight: archive valid, %d TOC entries", n_objects)

    backup_head = parse_alembic_head_from_filename(args.input)

    from finmind.config import finmind_settings

    try:
        conn = parse_connection(finmind_settings.FINMIND_DATABASE_URL)
    except ValueError as exc:
        print(f"restore: {exc}", file=sys.stderr)
        return 2

    async with FinmindAsyncSessionLocal() as session:
        target_head = await get_alembic_head(session)
        target_populated = await is_db_populated(session)

    if backup_head and backup_head != "unknown":
        if backup_head != target_head and not args.force_version:
            print(
                f"restore: ABORTED. Backup was taken at alembic "
                f"head {backup_head!r} but target is at "
                f"{target_head!r}. Run `alembic upgrade head` (or "
                f"downgrade) on the target first, OR pass "
                f"--force-version to ignore this check.",
                file=sys.stderr,
            )
            return 5

    if target_populated and not args.force:
        print(
            "restore: ABORTED. Target DB already populated "
            "(dataset_sources has rows). Pass --force to "
            "overwrite — DESTRUCTIVE.",
            file=sys.stderr,
        )
        return 6

    try:
        run_pg_restore(conn, args.input, clean=args.force)
    except subprocess.CalledProcessError as exc:
        print(
            f"restore: pg_restore exited {exc.returncode}",
            file=sys.stderr,
        )
        return exc.returncode

    print(
        f"restore: complete. Loaded {n_objects} TOC entries from "
        f"{args.input} into {conn['database']}@{conn['host']}."
    )

    await finmind_engine.dispose()
    return 0


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    os.chdir(_BACKEND_DIR)
    main()
