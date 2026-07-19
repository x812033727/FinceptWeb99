"""Diagnostic export — bundle FinMind subsystem state into a single
file for support / debugging.

Use case: operator hits a weird issue (cron failing silently,
quota math wrong, etc.) and wants to share state with a maintainer.
Manually gathering "current alembic head + recent errors + last
100 usage events + env config" is fragile. This script does it in
one command + redacts secrets.

Usage (from `backend/`):

    # JSON to stdout
    python -m finmind.scripts.diagnostic

    # JSON to file
    python -m finmind.scripts.diagnostic --output diag.json

    # Skip api_usage_events sample (large export-y datasets blow it
    # up; sometimes you only want the small sections):
    python -m finmind.scripts.diagnostic --skip-usage

Sections:
  - generated_at
  - environment    (FINMIND_DATABASE_URL host, FINMIND_TOKEN
                    presence, DEBUG, key env vars — secrets redacted)
  - status         (collect_status() result — alembic head,
                    catalog seeded, Phase 1 coverage, recent errors)
  - backfill_recent (last 50 backfill_progress rows)
  - usage_recent   (last 100 api_usage_events, status-code histogram)
  - dataset_telemetry (every dataset's last_ingest_at + last_error)

Secrets redacted:
  - DATABASE_URL / FINMIND_DATABASE_URL passwords
  - FINMIND_TOKEN / STRIPE_WEBHOOK_SECRET / FINMIND_ADMIN_API_KEY
    → "<redacted>" if non-empty, "" if empty (presence still useful)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import desc, func, select

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from finmind.db.session import (  # noqa: E402
    FinmindAsyncSessionLocal,
    finmind_engine,
)
from finmind.redaction import redact_exception, redact_secret_text  # noqa: E402


_SECRET_VARS = {
    "FINMIND_TOKEN",
    "STRIPE_WEBHOOK_SECRET",
    "FINMIND_ADMIN_API_KEY",
    "JWT_SECRET_KEY",
}

_URL_VARS = {
    "FINMIND_DATABASE_URL",
    "DATABASE_URL",
    "REDIS_URL",
}


def _redact_url_password(url: str) -> str:
    """`postgresql+asyncpg://user:secret@host/db` →
    `postgresql+asyncpg://user:<redacted>@host/db`.

    Preserves scheme + host + db so the operator can still see
    where the connector is pointing without leaking the password
    through the support channel."""
    return redact_secret_text(url) or ""


def collect_environment() -> dict:
    """Capture env vars relevant to the FinMind subsystem with
    secrets redacted. Presence of a secret matters even when the
    value can't ship — operators rule out "token is empty" without
    seeing the actual token."""
    out: dict = {}
    for k in sorted(_SECRET_VARS | _URL_VARS | {"DEBUG"}):
        raw = os.environ.get(k, "")
        if k in _SECRET_VARS:
            out[k] = "<redacted>" if raw else ""
        elif k in _URL_VARS:
            out[k] = _redact_url_password(raw)
        else:
            out[k] = raw
    return out


async def collect_diagnostic(skip_usage: bool = False) -> dict:
    """Walk every section and assemble the report. Each section is
    wrapped in try/except so a partial failure (e.g. one missing
    table) still produces a useful overall report."""
    out: dict = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(
            timespec="seconds",
        ),
        "environment": collect_environment(),
    }

    # ── Status ────────────────────────────────────────────
    try:
        from dataclasses import asdict
        from finmind.scripts.status import collect_status

        async with FinmindAsyncSessionLocal() as session:
            report = await collect_status(session)
        out["status"] = asdict(report)
    except Exception as exc:
        out["status"] = {
            "error": redact_exception(exc),
        }

    # ── Backfill recent ───────────────────────────────────
    try:
        from finmind.models.backfill_progress import BackfillProgress

        async with FinmindAsyncSessionLocal() as session:
            rows = (
                await session.execute(
                    select(BackfillProgress)
                    .order_by(desc(BackfillProgress.created_at))
                    .limit(50)
                )
            ).scalars().all()
        out["backfill_recent"] = [
            {
                "dataset_code": r.dataset_code,
                "symbol": r.symbol,
                "source": r.source,
                "range_start": r.range_start.isoformat(),
                "range_end": r.range_end.isoformat(),
                "status": r.status,
                "rows_written": r.rows_written,
                "error_message": (
                    safe[:300] if (
                        safe := redact_secret_text(r.error_message)
                    ) else None
                ),
                "started_at": (
                    r.started_at.isoformat() if r.started_at else None
                ),
                "finished_at": (
                    r.finished_at.isoformat() if r.finished_at else None
                ),
            }
            for r in rows
        ]
    except Exception as exc:
        out["backfill_recent"] = {
            "error": redact_exception(exc),
        }

    # ── Usage recent ──────────────────────────────────────
    if skip_usage:
        out["usage_recent"] = "skipped (--skip-usage)"
    else:
        try:
            from finmind.models.billing import ApiUsageEvent

            async with FinmindAsyncSessionLocal() as session:
                rows = (
                    await session.execute(
                        select(ApiUsageEvent)
                        .order_by(desc(ApiUsageEvent.ts))
                        .limit(100)
                    )
                ).scalars().all()

                # Status-code histogram across the same window.
                hist = (
                    await session.execute(
                        select(
                            ApiUsageEvent.status_code,
                            func.count().label("n"),
                        )
                        .group_by(ApiUsageEvent.status_code)
                        .order_by(ApiUsageEvent.status_code)
                    )
                ).all()
            out["usage_recent"] = {
                "sample_size": len(rows),
                "by_status_code": {
                    str(int(s)): int(n) for s, n in hist
                },
                "events": [
                    {
                        "ts": r.ts.isoformat(),
                        "api_key_id": r.api_key_id,
                        "dataset_code": r.dataset_code,
                        "endpoint": r.endpoint,
                        "row_count": r.row_count,
                        "status_code": r.status_code,
                        "latency_ms": r.latency_ms,
                        "error_message": (
                            safe[:200] if (
                                safe := redact_secret_text(r.error_message)
                            ) else None
                        ),
                    }
                    for r in rows
                ],
            }
        except Exception as exc:
            out["usage_recent"] = {
                "error": redact_exception(exc),
            }

    # ── Dataset telemetry ─────────────────────────────────
    try:
        from finmind.models.dataset_source import DatasetSource

        async with FinmindAsyncSessionLocal() as session:
            rows = (
                await session.execute(
                    select(DatasetSource).order_by(
                        DatasetSource.dataset_code
                    )
                )
            ).scalars().all()
        out["dataset_telemetry"] = [
            {
                "dataset_code": r.dataset_code,
                "enabled": r.enabled,
                "active_source": r.active_source,
                "local_table": r.local_table,
                "last_ingest_at": (
                    r.last_ingest_at.isoformat()
                    if r.last_ingest_at
                    else None
                ),
                "last_ingest_rows": r.last_ingest_rows,
                "last_error": (
                    safe[:300] if (
                        safe := redact_secret_text(r.last_error)
                    ) else None
                ),
            }
            for r in rows
        ]
    except Exception as exc:
        out["dataset_telemetry"] = {
            "error": redact_exception(exc),
        }

    return out


async def amain() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n", 1)[0]
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write JSON to this file (default: stdout).",
    )
    parser.add_argument(
        "--skip-usage",
        action="store_true",
        help=(
            "Skip the api_usage_events sample. Useful when the "
            "table is huge or you only want the small sections."
        ),
    )
    parser.add_argument(
        "--indent",
        type=int,
        default=2,
        help="JSON indent (0 for compact). Default: 2.",
    )
    args = parser.parse_args()

    diag = await collect_diagnostic(skip_usage=args.skip_usage)
    payload = json.dumps(
        diag,
        default=str,
        indent=args.indent or None,
        ensure_ascii=False,
    )

    if args.output:
        args.output.write_text(payload, encoding="utf-8")
        print(f"diagnostic: wrote {len(payload)} bytes to {args.output}")
    else:
        print(payload)

    await finmind_engine.dispose()
    return 0


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    os.chdir(_BACKEND_DIR)
    main()
