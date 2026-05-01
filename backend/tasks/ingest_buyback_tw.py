"""Daily TW buyback announcement ingest.

`TaiwanStockBuyBack` from FinMind. Sponsor-tier as of 2026-04
(see `data.tw.finmind_paywall` for the shared detector). Pulls the
last 90 days of announcements market-wide so we always see
in-execution buyback windows alongside fresh announcements; the
upsert key is `(market, symbol, announce_date)` so a row that's
re-pulled mid-window simply overwrites with the latest
`current_shares` execution figure.

Schedule: daily 18:00 Taipei (10:00 UTC) — well after the post-
close cluster, when companies that announced same-day buybacks
have had their filings published. One FinMind market-wide call
per tick — negligible quota.

Failure handling: paywall fail-soft via the shared detector
(matches the pattern PR #183 introduced for ingest_revenue_tw).
"""
import logging
from datetime import date, datetime, timedelta

import httpx

import data.tw.finmind_connector as finmind
from cache.redis_cache import acquire_lock, release_lock
from data.tw.finmind_paywall import (
    extract_body_message as _extract_body_message,
    looks_like_paywall as _looks_like_paywall,
)
from db.session import AsyncSessionLocal
from services.ingest.repository import (
    BuybackRow,
    backoff_remaining_seconds,
    clear_failures,
    get_failure_count,
    get_health,
    record_failure,
    record_health,
    upsert_buybacks,
)

log = logging.getLogger(__name__)

JOB_ID = "ingest_buyback_tw"
MARKET = "TW"

_LOCK_KEY = "lock:ingest_buyback_tw"
_LOCK_TTL = 10 * 60
_LOOKBACK_DAYS = 90


_HTTP_HINTS: dict[int, str] = {
    400: "FinMind rejected the request (likely paywalled or malformed)",
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
        body_msg = _extract_body_message(exc)
        body_suffix = f" — {body_msg}" if body_msg else ""
        hint = _HTTP_HINTS.get(code, "")
        hint_suffix = f" ({hint})" if hint else ""
        return f"HTTP {code} {reason}{hint_suffix}{body_suffix}"
    if isinstance(exc, httpx.TimeoutException):
        return f"timeout: {exc}"
    if isinstance(exc, httpx.ConnectError):
        return f"connect failed: {exc}"
    if isinstance(exc, httpx.HTTPError):
        return f"http error: {exc}"
    return f"unexpected: {exc}"


def _parse_date(s: str | None) -> date | None:
    """FinMind dates come as `"2026-04-15"` or `"2026/04/15"` and
    occasionally empty / null for in-progress fields. Be tolerant."""
    if not s:
        return None
    s = str(s).strip()[:10]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _to_int(v: object) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(float(str(v).replace(",", "")))
    except (TypeError, ValueError):
        return None


def _to_float(v: object) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


async def run() -> None:
    if not await acquire_lock(_LOCK_KEY, _LOCK_TTL):
        log.info("ingest_buyback_tw.skipped_lock_held")
        return
    try:
        remaining = await backoff_remaining_seconds(JOB_ID)
        if remaining > 0:
            failures = await get_failure_count(JOB_ID)
            mins = max(1, remaining // 60)
            previous = await get_health(JOB_ID)
            tail = ""
            if previous and previous.error and "skipped" not in (previous.error or ""):
                tail = f"; last: {previous.error[:200]}"
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
            body_msg = _extract_body_message(exc)
            if _looks_like_paywall(body_msg):
                # Paywall is a known-permanent state, not an outage —
                # same shape as PR #183's revenue cron handler.
                await clear_failures(JOB_ID)
                log.warning(
                    "ingest_buyback_tw.paywalled",
                    extra={"upstream_message": body_msg},
                )
                await record_health(
                    JOB_ID, ok=False, row_count=0,
                    error=(
                        "skipped: FinMind paywalled this dataset "
                        "(TaiwanStockBuyBack market-wide query needs "
                        "paid sponsor tier). Existing tw_stock_buyback "
                        "rows preserved. "
                        f"Upstream message: {body_msg}"
                    ),
                )
                return
            detail = _format_error(exc)
            failures = await record_failure(JOB_ID)
            log.warning(
                "ingest_buyback_tw.failed",
                extra={"error": detail, "failures": failures},
            )
            await record_health(
                JOB_ID, ok=False, row_count=0,
                error=f"{detail} (failure #{failures}; auto-backoff armed)",
            )
            return

        await clear_failures(JOB_ID)
        log.info(
            "ingest_buyback_tw.done",
            extra={"rows_processed": row_count},
        )
        await record_health(JOB_ID, ok=True, row_count=row_count)
    finally:
        await release_lock(_LOCK_KEY)


async def _do_run() -> int:
    start = (date.today() - timedelta(days=_LOOKBACK_DAYS)).isoformat()
    items = await finmind.get_buyback_market_wide(start)
    if not items:
        return 0

    payload: list[BuybackRow] = []
    for r in items:
        sym = (r.get("symbol") or "").strip()
        if not sym:
            continue
        announce = _parse_date(r.get("date"))
        if announce is None:
            continue
        payload.append(BuybackRow(
            market=MARKET,
            symbol=sym,
            announce_date=announce,
            period_start=_parse_date(r.get("period_start")),
            period_end=_parse_date(r.get("period_end")),
            method=_to_int(r.get("method")),
            purpose=(r.get("purpose") or None),
            max_shares=_to_int(r.get("max_shares")),
            current_shares=_to_int(r.get("current_shares")),
            price_lower=_to_float(r.get("price_lower")),
            price_upper=_to_float(r.get("price_upper")),
            source="finmind",
        ))

    if not payload:
        return 0

    async with AsyncSessionLocal() as db:
        return await upsert_buybacks(db, payload)
