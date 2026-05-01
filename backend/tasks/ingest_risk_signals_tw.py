"""Daily TW risk-signals ingest — disposition / suspended / day-trading.

Three small FinMind sponsor-tier datasets pulled in one job because
they share a discussion-context block. Each runs its own
fetch/normalise/upsert sub-step; per-dataset failure is isolated so
one transient FinMind hiccup doesn't drop all three signals.

Design choice — combined cron rather than three separate ones:
  - Operationally simpler (one IngestHealthCard row, one schedule
    entry, one retry button).
  - Total quota footprint is 3 calls/day, well below FinMind's
    sponsor hourly rate.
  - The per-dataset paywall fail-soft is still honoured — if FinMind
    re-tiers any one of the three, that dataset's sub-step records
    `skipped:` and the cron continues with the rest.

Schedule: daily 19:00 Taipei (11:00 UTC), after the buyback /
govt-bank cluster, so the sponsor-tier daily cluster spreads out.
"""
from __future__ import annotations

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
    DayTradingRow,
    DispositionRow,
    SuspendedRow,
    backoff_remaining_seconds,
    clear_failures,
    get_failure_count,
    get_health,
    record_failure,
    record_health,
    upsert_day_trading,
    upsert_dispositions,
    upsert_suspensions,
)

log = logging.getLogger(__name__)

JOB_ID = "ingest_risk_signals_tw"
MARKET = "TW"

_LOCK_KEY = "lock:ingest_risk_signals_tw"
_LOCK_TTL = 10 * 60
# Disposition windows + suspension events — 90 days covers any
# reasonable lookback. Day-trading volume is per-day and we only
# need recent context; pull 14 days so even a sparse cron history
# (deploy gaps, holidays) has enough data for the discussion block.
_DISPOSITION_LOOKBACK_DAYS = 90
_SUSPENDED_LOOKBACK_DAYS = 90
_DAY_TRADING_LOOKBACK_DAYS = 14


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


def _format_error(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code} {exc.response.reason_phrase or '?'}"
    if isinstance(exc, httpx.HTTPError):
        return f"http error: {exc}"
    return f"unexpected: {exc}"


# ── per-dataset sub-steps ─────────────────────────────────────────

async def _ingest_disposition() -> dict[str, object]:
    start = (date.today() - timedelta(days=_DISPOSITION_LOOKBACK_DAYS)).isoformat()
    items = await finmind.get_disposition_market_wide(start)
    payload: list[DispositionRow] = []
    for r in items:
        sym = (r.get("symbol") or "").strip()
        if not sym:
            continue
        period_start = _parse_date(r.get("period_start"))
        if period_start is None:
            continue
        payload.append(DispositionRow(
            market=MARKET, symbol=sym,
            period_start=period_start,
            period_end=_parse_date(r.get("period_end")),
            classification=(r.get("classification") or None),
            level=_to_int(r.get("level")),
            reason=(r.get("reason") or None),
            source="finmind",
        ))
    if not payload:
        return {"rows": 0, "fetched": len(items)}
    async with AsyncSessionLocal() as db:
        return {"rows": await upsert_dispositions(db, payload), "fetched": len(items)}


async def _ingest_suspended() -> dict[str, object]:
    start = (date.today() - timedelta(days=_SUSPENDED_LOOKBACK_DAYS)).isoformat()
    items = await finmind.get_suspended_market_wide(start)
    payload: list[SuspendedRow] = []
    for r in items:
        sym = (r.get("symbol") or "").strip()
        if not sym:
            continue
        ts = _parse_date(r.get("date"))
        if ts is None:
            continue
        payload.append(SuspendedRow(
            market=MARKET, symbol=sym, ts=ts,
            status=(r.get("status") or None),
            reason=(r.get("reason") or None),
            source="finmind",
        ))
    if not payload:
        return {"rows": 0, "fetched": len(items)}
    async with AsyncSessionLocal() as db:
        return {"rows": await upsert_suspensions(db, payload), "fetched": len(items)}


async def _ingest_day_trading() -> dict[str, object]:
    start = (date.today() - timedelta(days=_DAY_TRADING_LOOKBACK_DAYS)).isoformat()
    items = await finmind.get_day_trading_market_wide(start)
    payload: list[DayTradingRow] = []
    for r in items:
        sym = (r.get("symbol") or "").strip()
        if not sym:
            continue
        ts = _parse_date(r.get("date"))
        if ts is None:
            continue
        payload.append(DayTradingRow(
            market=MARKET, symbol=sym, ts=ts,
            volume=_to_int(r.get("volume")),
            buy_amount=_to_int(r.get("buy_amount")),
            sell_amount=_to_int(r.get("sell_amount")),
            source="finmind",
        ))
    if not payload:
        return {"rows": 0, "fetched": len(items)}
    async with AsyncSessionLocal() as db:
        return {"rows": await upsert_day_trading(db, payload), "fetched": len(items)}


_SUBSTEPS = (
    ("disposition", _ingest_disposition),
    ("suspended", _ingest_suspended),
    ("day_trading", _ingest_day_trading),
)


# ── orchestrator ──────────────────────────────────────────────────

async def run() -> None:
    if not await acquire_lock(_LOCK_KEY, _LOCK_TTL):
        log.info("ingest_risk_signals_tw.skipped_lock_held")
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

        results: dict[str, dict[str, object]] = {}
        sub_failures: list[str] = []
        sub_paywalls: list[str] = []
        for name, fn in _SUBSTEPS:
            try:
                results[name] = await fn()
            except Exception as exc:
                body_msg = _extract_body_message(exc)
                if _looks_like_paywall(body_msg):
                    sub_paywalls.append(f"{name}: {body_msg[:80]}")
                    log.warning(
                        "ingest_risk_signals_tw.paywalled",
                        extra={"substep": name, "upstream_message": body_msg},
                    )
                else:
                    sub_failures.append(f"{name}: {_format_error(exc)}")
                    log.warning(
                        "ingest_risk_signals_tw.substep_failed",
                        extra={"substep": name, "error": str(exc)},
                    )

        # Summarise. Three regimes:
        #
        #   1. All three sub-steps succeeded → ok=True.
        #   2. Some succeeded + others paywalled (or all paywalled) →
        #      ok=False with `skipped:` so the badge renders yellow
        #      via PR #187. No failure counter bump.
        #   3. Some / all sub-steps had a real outage → ok=False with
        #      `auto-backoff armed` so the badge renders red.
        total_rows = sum(int(r.get("rows", 0)) for r in results.values())
        success_names = list(results.keys())

        if sub_failures:
            failures_n = await record_failure(JOB_ID)
            await record_health(
                JOB_ID, ok=False, row_count=total_rows,
                error=(
                    f"{', '.join(sub_failures)} "
                    f"(failure #{failures_n}; auto-backoff armed). "
                    f"Successful sub-steps: {', '.join(success_names) or 'none'}."
                ),
            )
            return

        if sub_paywalls:
            await clear_failures(JOB_ID)
            await record_health(
                JOB_ID, ok=False, row_count=total_rows,
                error=(
                    f"skipped: FinMind paywalled — {'; '.join(sub_paywalls)}. "
                    f"Successful sub-steps: {', '.join(success_names) or 'none'}."
                ),
            )
            return

        await clear_failures(JOB_ID)
        log.info(
            "ingest_risk_signals_tw.done",
            extra={"results": results},
        )
        summary_parts = [
            f"{name}={results[name]['rows']} rows" for name in success_names
        ]
        await record_health(
            JOB_ID, ok=True, row_count=total_rows,
            error="; ".join(summary_parts) if summary_parts else None,
        )
    finally:
        await release_lock(_LOCK_KEY)
