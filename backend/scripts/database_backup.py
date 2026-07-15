"""Create, verify, checksum and retain main PostgreSQL backups."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import text

from config import settings
from db.session import AsyncSessionLocal, engine
from finmind.scripts.backup import parse_connection, run_pg_dump, verify_archive


async def _head() -> str:
    async with AsyncSessionLocal() as session:
        result = await session.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))
        return result.scalar_one_or_none() or "unknown"


def _prune(directory: Path, retention_days: int) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    removed = 0
    for archive in directory.glob("fincept_*.dump"):
        if datetime.fromtimestamp(archive.stat().st_mtime, timezone.utc) < cutoff:
            archive.unlink()
            archive.with_suffix(".json").unlink(missing_ok=True)
            removed += 1
    return removed


async def amain() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, default=Path("/backups"))
    parser.add_argument("--retention-days", type=int, default=int(os.getenv("BACKUP_RETENTION_DAYS", "35")))
    parser.add_argument("--compress", type=int, default=6)
    args = parser.parse_args()
    if args.retention_days < 2:
        parser.error("--retention-days must be at least 2")

    args.directory.mkdir(parents=True, exist_ok=True)
    head = await _head()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    output = args.directory / f"fincept_{stamp}_{head}.dump"
    conn = parse_connection(settings.DATABASE_URL)
    try:
        run_pg_dump(conn, output, compress=args.compress)
        objects = verify_archive(output)
        if objects <= 0:
            raise RuntimeError("backup archive contains no objects")
        digest = hashlib.sha256(output.read_bytes()).hexdigest()
        manifest = {
            "archive": output.name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "alembic_head": head,
            "sha256": digest,
            "bytes": output.stat().st_size,
            "toc_entries": objects,
        }
        output.with_suffix(".json").write_text(json.dumps(manifest, indent=2) + "\n")
        removed = _prune(args.directory, args.retention_days)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        output.unlink(missing_ok=True)
        print(f"database-backup: failed: {exc}")
        return 1
    finally:
        await engine.dispose()
    print(f"database-backup: verified {output} ({objects} objects, pruned={removed})")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
