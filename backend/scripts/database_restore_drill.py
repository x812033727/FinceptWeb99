"""Restore an archive into a guarded disposable database and verify it."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

from finmind.scripts.backup import parse_connection, verify_archive


def _run(argv: list[str], conn: dict[str, str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            env={**os.environ, "PGPASSWORD": conn["password"]},
            text=True,
            capture_output=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "no command output").strip()
        raise RuntimeError(f"{argv[0]} failed: {detail}") from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--target-url", required=True)
    parser.add_argument("--keep-database", action="store_true")
    args = parser.parse_args()
    conn = parse_connection(args.target_url)
    if not conn["database"].startswith("fincept_restore_drill_"):
        parser.error("target database must start with fincept_restore_drill_")
    if not args.input.is_file():
        parser.error("backup archive does not exist")

    manifest_path = args.input.with_suffix(".json")
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text())
        if hashlib.sha256(args.input.read_bytes()).hexdigest() != manifest.get("sha256"):
            raise SystemExit("restore-drill: checksum mismatch")
    verify_archive(args.input)

    common = ["--host", conn["host"], "--port", conn["port"], "--username", conn["user"]]
    try:
        _run(["dropdb", *common, "--if-exists", conn["database"]], conn)
        _run(["createdb", *common, conn["database"]], conn)
        _run([
            "pg_restore", *common, "--dbname", conn["database"],
            "--no-owner", "--no-privileges", "--single-transaction", str(args.input),
        ], conn)
        head = _run([
            "psql", *common, "--dbname", conn["database"], "--tuples-only", "--no-align",
            "--command", "SELECT version_num FROM alembic_version LIMIT 1",
        ], conn).stdout.strip()
        if not head:
            raise RuntimeError("restored database has no alembic head")
        print(f"restore-drill: verified archive at alembic head {head}")
    finally:
        if not args.keep_database:
            _run(["dropdb", *common, "--if-exists", conn["database"]], conn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
