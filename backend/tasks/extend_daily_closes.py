"""Extend stored close arrays to D10 for the scoreboard's D10 lens.

The D1-D10 excess curve over the graded experiment sessions rises
monotonically (+3.53pp at D1 → +10.28 at D5 → +12.55 at D10 — no
peak-and-fade at D5), so the user pre-committed (2026-07-25 grill
round 2) to a PARALLEL D10 observation lens. D5 stays the primary
verdict window; this task only lengthens the already-persisted
``daily_close_prices`` arrays so the scoreboard can report the D10
lens without an archive fan-out at build time.

Append-only by contract: existing entries (including unresolved
``None`` slots — those belong to `score_discussion_outcomes`) are
never rewritten, and a row is only touched when the archive already
holds the full 10 sessions from its anchor — a partial extension would
just be re-extended tomorrow anyway.

Daily at 12:10 UTC — after the TW EOD ingest (06:30 UTC) and the D5
scorer (09:30 UTC), before nothing critical.
"""
from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select, update

from cache.redis_cache import acquire_lock, release_lock
from db.session import AsyncSessionLocal
from models.discussion import Discussion
from models.ohlcv_daily import OhlcvDaily
from services.ingest.repository import (
    backoff_remaining_seconds,
    clear_failures,
    get_failure_count,
    get_health,
    record_failure,
    record_health,
)
from services.tw_trading_calendar import to_tw_date
from tasks._runner import TaskOutcome, run_ingest_task

log = logging.getLogger(__name__)

JOB_ID = "extend_daily_closes"
_LOCK_KEY = "lock:extend_daily_closes"
_LOCK_TTL = 10 * 60

TARGET_DAYS = 10
# Same scan floor as `score_discussion_outcomes`: older rows' windows
# closed long ago and rescanning them daily is a growing table walk.
_SCAN_WINDOW_DAYS = 60

# Mirrors `daily_scoreboard_service._GRADED_VERDICTS` without importing
# the service (this module must stay cheap to import for the scheduler).
_DECIDED_VERDICTS = ("win", "big_win", "loss", "big_loss")


def extend_arrays(
    daily: dict[str, list[float | None]],
    closes_by_symbol: dict[str, list[float]],
) -> dict[str, list[float | None]] | None:
    """Pure append step. Returns the extended mapping, or None when
    nothing changes (already at TARGET_DAYS, or the archive lacks the
    full 10 sessions for every extendable symbol).

    Existing entries are copied verbatim — this function only appends
    positions ``len(existing)..TARGET_DAYS`` and only when the
    symbol's archive series covers all TARGET_DAYS sessions.
    """
    out: dict[str, list[float | None]] = {}
    changed = False
    for sym, existing in daily.items():
        existing = list(existing or [])
        closes = closes_by_symbol.get(sym) or []
        if len(existing) < TARGET_DAYS and len(closes) >= TARGET_DAYS:
            existing = existing + [
                float(c) for c in closes[len(existing):TARGET_DAYS]
            ]
            changed = True
        out[sym] = existing
    return out if changed else None


def _anchor(d: Discussion) -> date:
    return d.as_of_date or to_tw_date(d.created_at)


async def _do_run() -> int:
    extended = 0
    scan_floor = datetime.now(UTC) - timedelta(days=_SCAN_WINDOW_DAYS)
    async with AsyncSessionLocal() as db:
        rows = list((await db.scalars(
            select(Discussion).where(
                Discussion.created_at >= scan_floor,
                Discussion.verdict.in_(_DECIDED_VERDICTS),
                Discussion.daily_close_prices.is_not(None),
            ).order_by(Discussion.created_at)
        )).all())

        for row in rows:
            daily = row.daily_close_prices or {}
            if not isinstance(daily, dict) or not daily:
                continue
            if all(len(v or []) >= TARGET_DAYS for v in daily.values()):
                continue  # idempotent skip — already extended
            anchor = _anchor(row)
            closes_by_symbol: dict[str, list[float]] = {}
            for sym in daily:
                series = (await db.execute(
                    select(OhlcvDaily.close).where(
                        OhlcvDaily.market == row.market,
                        OhlcvDaily.symbol == sym,
                        OhlcvDaily.ts >= anchor,
                        OhlcvDaily.close.is_not(None),
                    ).order_by(OhlcvDaily.ts).limit(TARGET_DAYS)
                )).scalars().all()
                closes_by_symbol[sym] = [float(c) for c in series]
            new_daily = extend_arrays(daily, closes_by_symbol)
            if new_daily is None:
                continue  # window not mature yet — retry tomorrow
            await db.execute(
                update(Discussion)
                .where(Discussion.id == row.id)
                .values(daily_close_prices=new_daily)
                .execution_options(synchronize_session=False)
            )
            extended += 1
        await db.commit()
    return extended


def _format_error(exc: BaseException) -> str:
    return str(exc)


async def _body() -> TaskOutcome:
    extended = await _do_run()
    if extended == 0:
        return TaskOutcome(row_count=0, status="idle: nothing to extend")
    return TaskOutcome(row_count=extended)


async def run() -> None:
    await run_ingest_task(
        job_id=JOB_ID, lock_key=_LOCK_KEY, lock_ttl=_LOCK_TTL, log=log,
        acquire_lock=acquire_lock, release_lock=release_lock,
        backoff_remaining_seconds=backoff_remaining_seconds,
        get_failure_count=get_failure_count, get_health=get_health,
        record_health=record_health, record_failure=record_failure,
        clear_failures=clear_failures,
        body=_body, format_error=_format_error,
    )
