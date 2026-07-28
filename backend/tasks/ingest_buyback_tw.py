"""Daily TW buyback announcement ingest.

Source: MOPS 庫藏股統計彙總表 (`mops_connector.get_buyback_summary`,
上市+上櫃). Every prior source is dead — FinMind removed
`TaiwanStockBuyBack` from its enum (HTTP 422, migration 0020), and
TWSE re-assigned OpenAPI `t187ap43_L` to 權證交易人數 (verified
2026-07-28), so MOPS is the remaining authoritative feed.

Pulls the last 90 days of announcements so we always see
in-execution buyback windows alongside fresh announcements; the
upsert key is `(market, symbol, announce_date)` so a row that's
re-pulled mid-window simply overwrites with the latest
`current_shares` execution figure.

Schedule: daily 18:10 Taipei (10:10 UTC) — well after the post-
close cluster, when companies that announced same-day buybacks
have had their filings published. Two MOPS calls per tick.
"""
import logging
from datetime import date, datetime, timedelta

import httpx

from cache.redis_cache import acquire_lock, release_lock
from data.tw.mops_connector import get_buyback_summary
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


def _format_error(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        reason = exc.response.reason_phrase or "?"
        return f"HTTP {code} {reason} (MOPS)"
    if isinstance(exc, httpx.TimeoutException):
        return f"timeout: {exc}"
    if isinstance(exc, httpx.ConnectError):
        return f"connect failed: {exc}"
    if isinstance(exc, httpx.HTTPError):
        return f"http error: {exc}"
    return f"unexpected: {exc}"


def _parse_date(s: str | None) -> date | None:
    """Connector dates come as ISO `"2026-04-15"` (or `"2026/04/15"`)
    and occasionally empty / null for in-progress fields. Be tolerant."""
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
    start = date.today() - timedelta(days=_LOOKBACK_DAYS)
    items = await get_buyback_summary(start, date.today())
    if not items:
        return 0

    payload: list[BuybackRow] = []
    for r in items:
        sym = (r.get("symbol") or "").strip()
        if not sym:
            continue
        announce = _parse_date(r.get("announce_date"))
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
            source="mops",
        ))

    if not payload:
        return 0

    async with AsyncSessionLocal() as db:
        return await upsert_buybacks(db, payload)
