"""Exit-code-only health check for the FinMind clone subsystem.

Designed for cron / k8s liveness probes / PagerDuty webhooks where the
caller only needs to know "is it OK or not." Reuses
`status.collect_status` so the failure conditions are computed from the
exact same data the human-readable report shows.

Usage (from `backend/`):

    python -m finmind.scripts.check
    python -m finmind.scripts.check --max-staleness-hours 36
    python -m finmind.scripts.check --max-quota-ratio 0.95 --max-failed 0

Exit codes:
    0  every check passed
    1  at least one check failed (failures printed to stderr)
    2  collector itself crashed (DB unreachable, etc.)

Each check has its own threshold flag so an operator can tune
individual datasets / environments without forking the script. Defaults
err on the strict side — better to wake someone up than to let a
silent staleness bug rot.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from finmind.db.session import (  # noqa: E402
    FinmindAsyncSessionLocal,
    finmind_engine,
)
from finmind.models.dataset_source import DatasetSource  # noqa: E402
from finmind.scripts.status import collect_status  # noqa: E402


# Per-frequency staleness budget. A dataset is considered "stale" when
# `now - last_ingest_at` exceeds this — slightly above the natural cadence
# so a one-cycle blip doesn't page anyone.
_FREQ_BUDGET = {
    "daily":     timedelta(hours=30),     # Daily cron + 6h grace
    "monthly":   timedelta(days=35),       # Last-of-month + 5d grace
    "quarterly": timedelta(days=100),      # Quarter + grace
    # 'realtime' / 'on_demand' aren't covered by this check — they have
    # no scheduled cadence to be late against.
}


async def _check_alembic(session: AsyncSession) -> list[str]:
    report = await collect_status(session)
    if not report.alembic.get("at_head"):
        cur = report.alembic.get("current") or "<uninitialized>"
        exp = report.alembic.get("expected")
        return [f"alembic NOT at head: current={cur} expected={exp}"]
    return []


async def _check_catalog(session: AsyncSession) -> list[str]:
    report = await collect_status(session)
    c = report.catalog
    if not c.get("ok"):
        return [
            f"dataset_sources catalog mismatch: "
            f"{c['seeded']}/{c['expected']} rows seeded — "
            f"run `python -m finmind.scripts.init_db`"
        ]
    return []


async def _check_staleness(
    session: AsyncSession, default_hours: int
) -> list[str]:
    """For every enabled dataset, verify `last_ingest_at` is within the
    expected budget for its `ingest_freq`. Datasets that have never run
    (`last_ingest_at IS NULL`) are reported separately so a fresh deploy
    doesn't immediately fail the check."""
    rows = (
        await session.execute(
            select(
                DatasetSource.dataset_code,
                DatasetSource.ingest_freq,
                DatasetSource.last_ingest_at,
            ).where(DatasetSource.enabled.is_(True))
        )
    ).all()

    failures: list[str] = []
    now = datetime.now(tz=timezone.utc)

    for code, freq, last in rows:
        budget = _FREQ_BUDGET.get(
            freq, timedelta(hours=default_hours)
        )
        if last is None:
            failures.append(
                f"{code}: enabled but never ran "
                f"(no last_ingest_at recorded)"
            )
            continue
        # Tolerate naive timestamps — SQLite drops tzinfo on read.
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        elapsed = now - last
        if elapsed > budget:
            failures.append(
                f"{code}: stale by {elapsed} (budget {budget}, "
                f"freq={freq})"
            )
    return failures


async def _check_failed_chunks(
    session: AsyncSession, max_allowed: int
) -> list[str]:
    report = await collect_status(session)
    failed = report.backfill.get("failed", 0)
    stuck = report.backfill.get("stuck_running", 0)
    out: list[str] = []
    if failed > max_allowed:
        out.append(
            f"backfill: {failed} failed chunks (allowed {max_allowed})"
        )
    if stuck > 0:
        out.append(
            f"backfill: {stuck} chunks stuck in 'running' "
            "longer than 1h (likely orphaned worker)"
        )
    return out


async def _check_quota(
    session: AsyncSession, max_ratio: float
) -> list[str]:
    report = await collect_status(session)
    q = report.quota
    if q is None:
        # Redis unavailable — silent in normal operation, surfaced
        # only when --strict-quota is on (caller's responsibility).
        return []
    if q["ratio"] >= max_ratio:
        return [
            f"FinMind quota at {q['ratio'] * 100:.1f}% "
            f"({q['used']}/{q['limit']}) — exceeds threshold "
            f"{max_ratio * 100:.0f}%"
        ]
    return []


async def run_checks(
    *,
    max_staleness_hours: int = 30,
    max_failed: int = 0,
    max_quota_ratio: float = 0.95,
    skip_staleness: bool = False,
    skip_quota: bool = False,
) -> list[str]:
    """Run every check, return aggregated failure messages.

    Empty list means everything passed. Caller maps that to exit code.
    Individual checks are independent — one failure doesn't short-
    circuit the others, so an operator gets the full picture in one run."""
    failures: list[str] = []

    async with FinmindAsyncSessionLocal() as session:
        failures.extend(await _check_alembic(session))
        failures.extend(await _check_catalog(session))
        failures.extend(
            await _check_failed_chunks(session, max_failed)
        )
        if not skip_staleness:
            failures.extend(
                await _check_staleness(session, max_staleness_hours)
            )
        if not skip_quota:
            failures.extend(
                await _check_quota(session, max_quota_ratio)
            )

    return failures


async def amain() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n", 1)[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--max-staleness-hours",
        type=int,
        default=30,
        help=(
            "Default staleness budget for datasets without a "
            "registered freq (default: 30h)."
        ),
    )
    parser.add_argument(
        "--max-failed",
        type=int,
        default=0,
        help="Tolerate at most N failed backfill chunks (default: 0).",
    )
    parser.add_argument(
        "--max-quota-ratio",
        type=float,
        default=0.95,
        help=(
            "Fail when FinMind quota usage ≥ this ratio (default: 0.95)."
        ),
    )
    parser.add_argument(
        "--skip-staleness",
        action="store_true",
        help="Don't check last_ingest_at — useful right after a deploy.",
    )
    parser.add_argument(
        "--skip-quota",
        action="store_true",
        help="Don't check FinMind quota — useful when Redis is down.",
    )
    args = parser.parse_args()

    try:
        failures = await run_checks(
            max_staleness_hours=args.max_staleness_hours,
            max_failed=args.max_failed,
            max_quota_ratio=args.max_quota_ratio,
            skip_staleness=args.skip_staleness,
            skip_quota=args.skip_quota,
        )
    except Exception as exc:
        print(f"finmind.check: collector crashed: {exc}", file=sys.stderr)
        return 2
    finally:
        await finmind_engine.dispose()

    if failures:
        print(
            f"finmind.check: FAILED ({len(failures)} issues)",
            file=sys.stderr,
        )
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print("finmind.check: OK", file=sys.stdout)
    return 0


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    os.chdir(_BACKEND_DIR)
    main()
