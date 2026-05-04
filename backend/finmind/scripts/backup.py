"""Backup the FinMind clone DB via `pg_dump`.

Wraps the pg_dump binary with three things you'd otherwise hand-roll
in a shell loop:

  1. Connection-string parsing — pulls user/password/host/port/db
     from FINMIND_DATABASE_URL so the operator doesn't double-
     specify them.
  2. Auto-naming — `finmind_<YYYYMMDDhhmmss>_<alembic_head>.dump`.
     Embedding the alembic head in the filename means restore can
     refuse to load a backup taken at a different schema version
     (which would otherwise silently leave orphan tables).
  3. Verify on disk — runs `pg_restore --list` against the freshly-
     written file to confirm it's a valid archive before reporting
     success. Catches "pg_dump exited 0 but wrote a corrupt file"
     class of failures.

Custom format (-Fc) so pg_restore can be selective on restore (skip
tables, parallel restore, etc.). Smaller than plain SQL too.

Usage (from `backend/`):

    # Default: write to ./backups/finmind_<ts>_<head>.dump
    python -m finmind.scripts.backup

    # Custom output path
    python -m finmind.scripts.backup --output /backups/finmind.dump

    # Compress harder (default 6, max 9)
    python -m finmind.scripts.backup --compress 9
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
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from finmind.db.session import (  # noqa: E402
    FinmindAsyncSessionLocal,
    finmind_engine,
)

log = logging.getLogger("finmind.backup")


def parse_connection(url: str) -> dict[str, str]:
    """`postgresql+asyncpg://user:pass@host:5432/db` → dict for
    pg_dump argv. Handles the `+driver` suffix asyncpg adds (pg_dump
    doesn't recognize it)."""
    if not url:
        raise ValueError("FINMIND_DATABASE_URL is empty")
    # Strip the SQLAlchemy `+driver` suffix — pg_dump only knows
    # `postgresql://`, not `postgresql+asyncpg://`.
    cleaned = re.sub(r"^([a-z]+)\+[a-z]+://", r"\1://", url)
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("postgres", "postgresql"):
        raise ValueError(
            f"only postgres URLs supported for pg_dump; got "
            f"scheme={parsed.scheme!r}"
        )
    if not parsed.path or parsed.path == "/":
        raise ValueError(f"no database in URL: {url}")
    return {
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "host": parsed.hostname or "localhost",
        "port": str(parsed.port or 5432),
        "database": parsed.path.lstrip("/"),
    }


async def get_alembic_head(session) -> str:
    """Read the current head from `alembic_version_finmind`. Embedded
    in the filename so restore can sanity-check version match."""
    from sqlalchemy import text

    try:
        result = await session.execute(
            text("SELECT version_num FROM alembic_version_finmind LIMIT 1")
        )
        return result.scalar_one_or_none() or "unknown"
    except Exception:
        return "unknown"


def default_output_path(alembic_head: str) -> Path:
    """./backups/finmind_<ts>_<head>.dump under the current working
    directory. Created automatically — operator doesn't have to
    `mkdir -p backups` first."""
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d%H%M%S")
    out_dir = Path("backups")
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"finmind_{ts}_{alembic_head}.dump"


def run_pg_dump(
    conn: dict[str, str],
    output: Path,
    compress: int = 6,
) -> None:
    """Spawn pg_dump with `-Fc` (custom format). Password passed via
    PGPASSWORD env so it doesn't appear in `ps` output."""
    if shutil.which("pg_dump") is None:
        raise FileNotFoundError(
            "pg_dump not on PATH — install postgresql-client (Debian: "
            "`apt-get install postgresql-client`; Alpine: "
            "`apk add postgresql-client`)."
        )

    argv = [
        "pg_dump",
        "--host", conn["host"],
        "--port", conn["port"],
        "--username", conn["user"],
        "--dbname", conn["database"],
        "--format", "custom",
        "--compress", str(compress),
        "--no-owner",       # restore should land in caller's user, not
                            # the original. Survives user renames.
        "--no-privileges",  # same — drop GRANT/REVOKE so the dump is
                            # portable across deployments.
        "--file", str(output),
    ]
    env = {**os.environ, "PGPASSWORD": conn["password"]}
    log.info("running pg_dump → %s", output)
    subprocess.run(argv, env=env, check=True)


def verify_archive(output: Path) -> int:
    """`pg_restore --list` parses the dump and prints its TOC. Used
    here as a cheap "is this file a valid archive?" check; returns
    the table-of-contents entry count so the caller can sanity-check
    "did we actually dump anything?"."""
    if shutil.which("pg_restore") is None:
        # Skip verification rather than failing the whole backup —
        # operator can verify manually later.
        log.warning(
            "pg_restore not on PATH — skipping post-write verify"
        )
        return -1
    result = subprocess.run(
        ["pg_restore", "--list", str(output)],
        capture_output=True,
        text=True,
        check=True,
    )
    # pg_restore --list emits one TOC line per object; count
    # non-comment lines.
    return sum(
        1
        for line in result.stdout.splitlines()
        if line.strip() and not line.startswith(";")
    )


async def amain() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--output",
        type=Path,
        help="Output path (default: ./backups/finmind_<ts>_<head>.dump).",
    )
    parser.add_argument(
        "--compress",
        type=int,
        default=6,
        help="zlib level for pg_dump custom-format (1-9, default: 6).",
    )
    parser.add_argument(
        "--skip-verify",
        action="store_true",
        help="Don't run pg_restore --list after dump (faster, less safe).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
        force=True,
    )

    from finmind.config import finmind_settings

    try:
        conn = parse_connection(finmind_settings.FINMIND_DATABASE_URL)
    except ValueError as exc:
        print(f"backup: {exc}", file=sys.stderr)
        return 2

    async with FinmindAsyncSessionLocal() as session:
        head = await get_alembic_head(session)

    output = args.output or default_output_path(head)

    try:
        run_pg_dump(conn, output, compress=args.compress)
    except FileNotFoundError as exc:
        print(f"backup: {exc}", file=sys.stderr)
        return 3
    except subprocess.CalledProcessError as exc:
        print(
            f"backup: pg_dump exited {exc.returncode}",
            file=sys.stderr,
        )
        return exc.returncode

    size = output.stat().st_size
    if not args.skip_verify:
        try:
            n_objects = verify_archive(output)
            verify_msg = f", {n_objects} TOC entries" if n_objects > 0 else ""
        except subprocess.CalledProcessError as exc:
            print(
                f"backup: pg_restore --list failed (exit "
                f"{exc.returncode}) — dump file may be corrupt",
                file=sys.stderr,
            )
            return 4
    else:
        verify_msg = " (verify skipped)"

    print(
        f"backup: wrote {output} ({size:,} bytes; alembic head "
        f"{head}{verify_msg})"
    )

    await finmind_engine.dispose()
    return 0


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    os.chdir(_BACKEND_DIR)
    main()
