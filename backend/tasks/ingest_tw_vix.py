"""Daily TAIWAN VIX (臺指選擇權波動率指數) ingest (PR #283).

TAIFEX publishes the 臺指選擇權波動率指數 in two tiers, and we pull
both every tick (~4 + ~20 small requests) into ``tw_vix_daily``:

  - the **monthly archive** (4 months retained) — the only free
    source of history, and what makes a percentile meaningful
  - the **per-session files** (~7 sessions) — carries today's close

Backfill is automatic: re-running the cron fills any missed days
within either window (TAIFEX corrections to prior closes also
propagate via ON CONFLICT DO UPDATE).

Schedule: 16:00 Taipei (08:00 UTC), 1.5 hours after the cash market
close (13:30) but before the FinMind sponsor cluster (10:00+ UTC),
so a TAIFEX outage doesn't cascade into FinMind quota burn.

Look-ahead protection: VIX closes are insert-only — no need for
backtest masking on the read tier.
"""
import logging
from datetime import date, timedelta

import httpx

import data.tw.taifex_connector as taifex
from cache.redis_cache import acquire_lock, release_lock
from db.session import AsyncSessionLocal
from services.ingest.repository import (
    VixDailyRow,
    backoff_remaining_seconds,
    clear_failures,
    get_failure_count,
    get_health,
    record_failure,
    record_health,
    upsert_tw_vix_daily,
)
from tasks._runner import TaskOutcome, run_ingest_task

log = logging.getLogger(__name__)

JOB_ID = "ingest_tw_vix"
MARKET = "TW"

_LOCK_KEY = "lock:ingest_tw_vix"
_LOCK_TTL = 5 * 60
_LOOKBACK_DAYS = 30
# TAIFEX retains ~4 monthly files; asking for more just logs skips.
# Re-pulling all of them every tick keeps the archive self-healing
# (a month ingested while incomplete gets its later sessions filled
# in on the next run) for 4 cheap requests.
_MONTH_WINDOW = 4


_HTTP_HINTS: dict[int, str] = {
    400: "TAIFEX rejected the request — query may be malformed",
    403: "TAIFEX refused — UA blocked or geo-restricted",
    429: "TAIFEX rate-limit — backoff and retry later",
    500: "TAIFEX upstream error",
    502: "TAIFEX bad gateway",
    503: "TAIFEX unavailable",
    504: "TAIFEX gateway timeout",
}


def _format_error(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        reason = exc.response.reason_phrase or "?"
        hint = _HTTP_HINTS.get(code, "")
        suffix = f" ({hint})" if hint else ""
        return f"HTTP {code} {reason}{suffix}"
    if isinstance(exc, httpx.TimeoutException):
        return f"timeout: {exc}"
    if isinstance(exc, httpx.ConnectError):
        return f"connect failed: {exc}"
    if isinstance(exc, httpx.HTTPError):
        return f"http error: {exc}"
    return f"unexpected: {exc}"


async def _body() -> TaskOutcome:
    return TaskOutcome(row_count=await _do_run())


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


def _to_rows(items: list[dict[str, object]]) -> list[VixDailyRow]:
    rows: list[VixDailyRow] = []
    for item in items:
        try:
            ts = date.fromisoformat(str(item["date"]))
        except (KeyError, ValueError, TypeError):
            continue
        try:
            value = float(item["value"])  # type: ignore[arg-type]
        except (KeyError, ValueError, TypeError):
            continue
        rows.append(VixDailyRow(
            market=MARKET, ts=ts, vix_value=value, source="taifex",
        ))
    return rows


async def _do_run() -> int:
    """Pull both tiers and upsert the union.

    Two sources, deliberately:

    - **Month files** (`_MONTH_WINDOW` back) are the only free source of
      VIX *history*. Without them the archive held 15 rows, which is too
      few to tell a persona whether today's level is high or low — the
      gap that let a stale hardcoded baseline stand in for the real
      distribution.
    - **Per-session files** still cover today: the current month's file
      is published on TAIFEX's own schedule, so the day walk is what
      makes the latest close available promptly.

    Both emit the closing-minute average, so overlapping days upsert to
    the same value rather than fighting each other.
    """
    end = date.today()
    start = end - timedelta(days=_LOOKBACK_DAYS)

    monthly = await taifex.get_vix_monthly_history(months=_MONTH_WINDOW, end=end)
    if not monthly:
        # Every month failing is not the steady state — an aged-out
        # month is skipped individually inside the connector.
        log.warning("ingest_tw_vix.monthly_archive_empty")
    daily = await taifex.get_vix_history(start, end)

    # Day walk wins on overlap: it is the tier that carries today.
    merged: dict[str, object] = {
        str(item["date"]): item["value"] for item in monthly
    }
    merged.update({str(item["date"]): item["value"] for item in daily})
    rows = _to_rows(
        [{"date": d, "value": v} for d, v in sorted(merged.items())],
    )
    if not rows:
        # Surfaced as a task failure rather than a quiet 0 — a green
        # health check on an empty write is how earlier ingest holes
        # stayed invisible for weeks.
        raise RuntimeError(
            "TAIFEX VIX returned no rows from either the monthly "
            "archive or the per-session walk",
        )

    async with AsyncSessionLocal() as db:
        return await upsert_tw_vix_daily(db, rows)
