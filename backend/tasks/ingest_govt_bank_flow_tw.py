"""Daily TW 八大行庫 flow ingest.

`TaiwanStockGovernmentBankBuySell` from FinMind sponsor tier. One
market-wide call per day returns 8-bank rows. Adopts the shared
paywall fail-soft pattern.

Schedule: 18:30 Taipei (10:30 UTC), after the buyback cron, so the
post-close FinMind cluster spreads out predictably.
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
    GovtBankFlowRow,
    backoff_remaining_seconds,
    clear_failures,
    get_failure_count,
    get_health,
    record_failure,
    record_health,
    upsert_govt_bank_flows,
)

log = logging.getLogger(__name__)

JOB_ID = "ingest_govt_bank_flow_tw"
MARKET = "TW"

_LOCK_KEY = "lock:ingest_govt_bank_flow_tw"
_LOCK_TTL = 5 * 60
_LOOKBACK_DAYS = 30


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


async def run() -> None:
    if not await acquire_lock(_LOCK_KEY, _LOCK_TTL):
        log.info("ingest_govt_bank_flow_tw.skipped_lock_held")
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
                await clear_failures(JOB_ID)
                log.warning(
                    "ingest_govt_bank_flow_tw.paywalled",
                    extra={"upstream_message": body_msg},
                )
                await record_health(
                    JOB_ID, ok=False, row_count=0,
                    error=(
                        "skipped: FinMind paywalled this dataset "
                        "(TaiwanStockGovernmentBankBuySell needs paid "
                        "sponsor tier). Existing tw_govt_bank_flow_daily "
                        f"rows preserved. Upstream message: {body_msg}"
                    ),
                )
                return
            detail = _format_error(exc)
            failures = await record_failure(JOB_ID)
            log.warning(
                "ingest_govt_bank_flow_tw.failed",
                extra={"error": detail, "failures": failures},
            )
            await record_health(
                JOB_ID, ok=False, row_count=0,
                error=f"{detail} (failure #{failures}; auto-backoff armed)",
            )
            return

        await clear_failures(JOB_ID)
        log.info(
            "ingest_govt_bank_flow_tw.done",
            extra={"rows_processed": row_count},
        )
        await record_health(JOB_ID, ok=True, row_count=row_count)
    finally:
        await release_lock(_LOCK_KEY)


async def _do_run() -> int:
    start = (date.today() - timedelta(days=_LOOKBACK_DAYS)).isoformat()
    items = await finmind.get_government_bank_flow_market_wide(start)
    if not items:
        return 0

    payload: list[GovtBankFlowRow] = []
    for r in items:
        bank = (r.get("bank_name") or "").strip()
        if not bank:
            continue
        ts = _parse_date(r.get("date"))
        if ts is None:
            continue
        payload.append(GovtBankFlowRow(
            market=MARKET,
            ts=ts,
            bank_name=bank[:40],
            buy_amount=_to_int(r.get("buy_amount")),
            sell_amount=_to_int(r.get("sell_amount")),
            source="finmind",
        ))

    if not payload:
        return 0

    async with AsyncSessionLocal() as db:
        return await upsert_govt_bank_flows(db, payload)
