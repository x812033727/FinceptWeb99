"""Daily TAIEX TR (含息報酬指數 IR0001) history ingest.

Mirrors the price-index `ingest_taiex_history` cron from PR #132,
but pulls the dividend-reinvested variant from FinMind sponsor-tier
(`TaiwanStockTotalReturnIndex` dataset). Stored under the synthetic
symbol `_TAIEX_TR` in `ohlcv_daily`, paired with `_TAIEX`.

Why we want both:
  - `_TAIEX`: the headline index every news source prints. Personas
    and discussion context use this for "大盤型態" reasoning.
  - `_TAIEX_TR`: includes dividends, so apples-to-apples vs a TW
    portfolio's mark-to-market value (which also reinvests
    dividends through ex-div price drops mechanically). The
    `PortfolioPage` PerformanceChart will pull this for TWD-
    denominated portfolios — replacing the wrong-by-default SPY
    benchmark that PR #132 left in place.

Schedule: 15:30 + 22:30 Taipei (07:30/14:30 UTC), 20 min after the
price-index cron, so neither shares the same TWSE quiet window. The
evening re-run covers FinMind's index sync lagging the 15:30 slot.

Failure handling: paywall fail-soft via the shared
`data.tw.finmind_paywall` detector — sponsor-tier today, but
FinMind's tier-shuffle history makes the safety net cheap.
"""
import logging
from datetime import date, datetime, timedelta

import httpx
from sqlalchemy import func, select

import data.tw.finmind_connector as finmind
from cache.redis_cache import acquire_lock, release_lock
from data.tw.finmind_connector import FinMindSilentDeny
from data.tw.finmind_paywall import (
    extract_body_message as _extract_body_message,
)
from data.tw.finmind_paywall import (
    looks_like_paywall as _looks_like_paywall,
)
from data.tw.finmind_paywall import (
    raise_if_silent_denied,
)
from db.session import AsyncSessionLocal
from models.ohlcv_daily import OhlcvDaily
from services.ingest.repository import (
    OhlcvBar,
    backoff_remaining_seconds,
    clear_failures,
    get_failure_count,
    get_health,
    record_failure,
    record_health,
    upsert_ohlcv_bars,
)

log = logging.getLogger(__name__)

JOB_ID = "ingest_taiex_tr_history"
MARKET = "TW"
TAIEX_TR_SYMBOL = "_TAIEX_TR"

_LOCK_KEY = "lock:ingest_taiex_tr_history"
_LOCK_TTL = 3 * 60
# Only the empty-archive bound: the first run ever (or a wiped archive)
# backfills from `date.today() - _LOOKBACK_DAYS`, deep enough for the
# factor-validation API's five-year maximum plus forward-execution
# headroom. Every subsequent run is clamped by `clamp_fetch_start` to a
# few days before the newest archived bar — see its docstring — so this
# constant is never reached again once the archive is populated.
_LOOKBACK_DAYS = 5 * 366 + 120
# Healing overlap for `clamp_fetch_start`: a handful of idempotent
# re-upserts near the seam, in case upstream revises a recent bar.
_HEAL_OVERLAP_DAYS = 5


def clamp_fetch_start(*, newest_archived: date | None, epoch: date) -> date:
    """Append-only window: start just before the newest archived bar.

    The 5-day overlap re-upserts a handful of rows (idempotent) so a
    late upstream revision still heals, while the full-history rewrite
    — which would churn compressed chunks daily once 0099 lands — is
    gone."""
    if newest_archived is None:
        return epoch
    return newest_archived - timedelta(days=_HEAL_OVERLAP_DAYS)


def _format_error(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code} {exc.response.reason_phrase or '?'}"
    if isinstance(exc, httpx.HTTPError):
        return f"http error: {exc}"
    return f"unexpected: {exc}"


def _parse_iso(s: str | None) -> date | None:
    if not s:
        return None
    s = str(s).strip()[:10]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


async def run() -> None:
    if not await acquire_lock(_LOCK_KEY, _LOCK_TTL):
        log.info("ingest_taiex_tr_history.skipped_lock_held")
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
        except FinMindSilentDeny as exc:
            await clear_failures(JOB_ID)
            log.warning(
                "ingest_taiex_tr_history.silent_deny",
                extra={"upstream_message": exc.body_msg},
            )
            await record_health(
                JOB_ID, ok=False, row_count=0,
                error=f"silent_paywall: {exc.body_msg}",
                silent_deny=exc.body_msg,
                source="finmind",
            )
            return
        except Exception as exc:
            body_msg = _extract_body_message(exc)
            if _looks_like_paywall(body_msg):
                await clear_failures(JOB_ID)
                log.warning(
                    "ingest_taiex_tr_history.paywalled",
                    extra={"upstream_message": body_msg},
                )
                await record_health(
                    JOB_ID, ok=False, row_count=0,
                    error=(
                        "skipped: FinMind paywalled this dataset "
                        "(TaiwanStockTotalReturnIndex needs paid sponsor "
                        "tier). Existing _TAIEX_TR rows preserved; "
                        "PortfolioPage falls back to SPY benchmark for "
                        f"all currencies. Upstream message: {body_msg}"
                    ),
                )
                return
            detail = _format_error(exc)
            failures = await record_failure(JOB_ID)
            log.warning(
                "ingest_taiex_tr_history.failed",
                extra={"error": detail, "failures": failures},
            )
            await record_health(
                JOB_ID, ok=False, row_count=0,
                error=f"{detail} (failure #{failures}; auto-backoff armed)",
            )
            return

        await clear_failures(JOB_ID)
        log.info(
            "ingest_taiex_tr_history.done",
            extra={"rows_processed": row_count},
        )
        await record_health(JOB_ID, ok=True, row_count=row_count)
    finally:
        await release_lock(_LOCK_KEY)


async def _do_run() -> int:
    epoch = date.today() - timedelta(days=_LOOKBACK_DAYS)
    async with AsyncSessionLocal() as db:
        newest_archived = await db.scalar(
            select(func.max(OhlcvDaily.ts)).where(
                OhlcvDaily.market == MARKET,
                OhlcvDaily.symbol == TAIEX_TR_SYMBOL,
            )
        )
    start = clamp_fetch_start(
        newest_archived=newest_archived, epoch=epoch,
    ).isoformat()
    items = raise_if_silent_denied(
        await finmind.get_taiex_total_return_index(start),
    )
    if not items:
        return 0

    payload: list[OhlcvBar] = []
    for r in items:
        ts = _parse_iso(r.get("time"))
        if ts is None:
            continue
        try:
            close = float(r.get("close") or 0)
        except (TypeError, ValueError):
            continue
        if close <= 0:
            continue
        payload.append(OhlcvBar(
            market=MARKET,
            symbol=TAIEX_TR_SYMBOL,
            ts=ts,
            open=close,    # index has no OHL — flatten
            high=close,
            low=close,
            close=close,
            volume=int(r.get("volume") or 0),
            source="finmind",
        ))

    if not payload:
        return 0

    async with AsyncSessionLocal() as db:
        return await upsert_ohlcv_bars(db, payload)
