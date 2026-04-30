"""Daily TW monthly-revenue ingest.

Runs daily at 06:30 UTC (= 14:30 Taipei). TW securities law gives
listed companies until the 10th of the following month to publish
monthly revenue; we re-pull every day to capture lagging filers
+ corrections without needing a precise "everyone's filed" tick.

One FinMind market-wide call (`TaiwanStockMonthRevenue` with empty
`data_id`) returns every listed company's revenue for the
requested date range — no per-symbol fan-out, well below FinMind's
free-tier hourly limit. Rows are upserted into `tw_revenue_monthly`
so re-pulling overwrites stale values when companies file
corrections.

Lookback: 90 days. Wide enough to pick up late filers + any
upstream corrections to recently-published months without writing
years of unchanged history every day.

Failure handling and lock semantics mirror `tasks/ingest_news_tw.py`
— exponential 1h → 6h backoff, last-error preserved across the
cooldown window.
"""
import logging
from datetime import date, datetime, timedelta

import httpx

import data.tw.finmind_connector as finmind
from cache.redis_cache import acquire_lock, release_lock
from db.session import AsyncSessionLocal
from services.ingest.repository import (
    RevenueMonthlyRow,
    backoff_remaining_seconds,
    clear_failures,
    get_failure_count,
    get_health,
    record_failure,
    record_health,
    upsert_revenue_monthly,
)

log = logging.getLogger(__name__)

JOB_ID = "ingest_revenue_tw"
MARKET = "TW"

_LOCK_KEY = "lock:ingest_revenue_tw"
_LOCK_TTL = 10 * 60   # one FinMind call + bulk write
_LOOKBACK_DAYS = 90


_HTTP_HINTS: dict[int, str] = {
    400: "FinMind rejected the request — empty/malformed FINMIND_TOKEN",
    401: "check FINMIND_TOKEN — invalid or missing",
    402: "FinMind dataset requires paid sponsorship",
    403: "FinMind token forbidden for this dataset",
    429: "FinMind quota exhausted — resets at UTC 00:00",
    500: "FinMind upstream error",
    502: "FinMind bad gateway",
    503: "FinMind unavailable",
    504: "FinMind gateway timeout",
}


def _format_error(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        reason = exc.response.reason_phrase or "?"
        body_msg = ""
        try:
            body = exc.response.json()
            for key in ("msg", "message", "detail"):
                if isinstance(body, dict) and body.get(key):
                    body_msg = f" — {body[key]}"
                    break
        except Exception:
            pass
        hint = _HTTP_HINTS.get(code, "")
        suffix = f" ({hint})" if hint else ""
        return f"HTTP {code} {reason}{suffix}{body_msg}"
    if isinstance(exc, httpx.TimeoutException):
        return f"timeout: {exc}"
    if isinstance(exc, httpx.ConnectError):
        return f"connect failed: {exc}"
    if isinstance(exc, httpx.HTTPError):
        return f"http error: {exc}"
    return f"unexpected: {exc}"


async def run() -> None:
    if not await acquire_lock(_LOCK_KEY, _LOCK_TTL):
        log.info("ingest_revenue_tw.skipped_lock_held")
        return
    try:
        remaining = await backoff_remaining_seconds(JOB_ID)
        if remaining > 0:
            failures = await get_failure_count(JOB_ID)
            mins = max(1, remaining // 60)
            previous = await get_health(JOB_ID)
            tail = ""
            if previous and previous.error and "skipped" not in (previous.error or ""):
                last_err = previous.error[:200]
                tail = f"; last: {last_err}"
            await record_health(
                JOB_ID, ok=False, row_count=0,
                error=(
                    f"skipped (backoff after {failures} failures, "
                    f"~{mins} min remaining{tail})"
                ),
            )
            return

        try:
            row_count = await _do_run()
        except Exception as exc:
            detail = _format_error(exc)
            failures = await record_failure(JOB_ID)
            log.warning(
                "ingest_revenue_tw.failed",
                extra={"error": detail, "failures": failures},
            )
            await record_health(
                JOB_ID, ok=False, row_count=0,
                error=f"{detail} (failure #{failures}; auto-backoff armed)",
            )
            return

        await clear_failures(JOB_ID)
        log.info(
            "ingest_revenue_tw.done",
            extra={"rows_processed": row_count},
        )
        await record_health(JOB_ID, ok=True, row_count=row_count)
    finally:
        await release_lock(_LOCK_KEY)


async def _do_run() -> int:
    start = (date.today() - timedelta(days=_LOOKBACK_DAYS)).isoformat()
    items = await finmind.get_monthly_revenue_market_wide(start)
    if not items:
        return 0

    payload: list[RevenueMonthlyRow] = []
    for r in items:
        sym = r.get("symbol", "").strip()
        if not sym:
            continue
        try:
            ts = date.fromisoformat(str(r.get("date", ""))[:10])
        except ValueError:
            continue
        # Coerce growth percentages — FinMind sometimes returns ""
        # or None for newly-listed companies with no prior baseline.
        try:
            yoy = float(r.get("revenue_yoy", 0)) if r.get("revenue_yoy") not in (None, "") else None
        except (TypeError, ValueError):
            yoy = None
        try:
            mom = float(r.get("revenue_mom", 0)) if r.get("revenue_mom") not in (None, "") else None
        except (TypeError, ValueError):
            mom = None
        try:
            rev = int(r.get("revenue", 0))
        except (TypeError, ValueError):
            rev = None

        payload.append(RevenueMonthlyRow(
            market=MARKET,
            symbol=sym,
            ts=ts,
            revenue=rev,
            revenue_yoy=yoy,
            revenue_mom=mom,
            source="finmind",
        ))

    if not payload:
        return 0

    async with AsyncSessionLocal() as db:
        return await upsert_revenue_monthly(db, payload)
