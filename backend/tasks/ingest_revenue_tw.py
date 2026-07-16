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

Growth rates: FinMind's `TaiwanStockMonthRevenue` doesn't carry YoY
or MoM as percentages — only the period year/month integers. We
compute both ourselves by joining each new row to its prev-month and
prev-year-same-month counterparts (in-payload first, then DB). PR #211.

Failure handling and lock semantics mirror `tasks/ingest_news_tw.py`
— exponential 1h → 6h backoff, last-error preserved across the
cooldown window.
"""
import logging
from datetime import date, timedelta

import httpx
from sqlalchemy import select

import data.tw.finmind_connector as finmind
from cache.redis_cache import acquire_lock, release_lock
from data.tw.finmind_connector import FinMindSilentDeny
from db.session import AsyncSessionLocal
from models.tw_revenue_monthly import TwRevenueMonthly
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

# Paywall detection lives in `data.tw.finmind_paywall` so all
# FinMind-backed crons (ingest_ohlcv_tw, tw_etf_yields_refresh, …)
# can opt into the same fail-soft path with a one-line check.
from data.tw.finmind_paywall import (  # noqa: E402
    extract_body_message as _extract_body_message,
    looks_like_paywall as _looks_like_paywall,
    raise_if_silent_denied,
)

_HTTP_HINTS: dict[int, str] = {
    400: "FinMind rejected the request (likely paywalled dataset or malformed token)",
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
        except FinMindSilentDeny as exc:
            # HTTP 200 + body.status != 200 — FinMind accepted the
            # request but returned nothing because the tier doesn't
            # grant access. Treat exactly like an explicit 402 paywall:
            # don't arm backoff (permanent state), don't bump failure
            # counter, but flip ok=False so the admin badge surfaces it.
            await clear_failures(JOB_ID)
            log.warning(
                "ingest_revenue_tw.silent_deny",
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
                # Paywall is a known-permanent state, not an outage.
                # Don't arm auto-backoff and don't bump the failure
                # counter — both would imply this can recover by
                # itself, which it can't. Reset prior arm so the
                # admin's "Retry now" button doesn't surface a stale
                # backoff banner.
                await clear_failures(JOB_ID)
                log.warning(
                    "ingest_revenue_tw.paywalled",
                    extra={"upstream_message": body_msg},
                )
                await record_health(
                    JOB_ID, ok=False, row_count=0,
                    error=(
                        "skipped: FinMind paywalled this dataset "
                        "(TaiwanStockMonthRevenue market-wide query "
                        "needs paid sponsor tier as of 2026-04). "
                        "Existing tw_revenue_monthly rows preserved; "
                        "discussion `top_revenue_growers` block keeps "
                        "the most recent successful pull. "
                        f"Upstream message: {body_msg}"
                    ),
                )
                return
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


def _shift_month(d: date, delta_months: int) -> date:
    """Shift `d` by N calendar months, anchoring to day-1. Revenue rows
    use `ts = first-of-month` so e.g. `_shift_month(date(2026,2,1), -12)`
    gives `date(2025,2,1)` — the prev-year-same-month for YoY lookup."""
    total = d.year * 12 + (d.month - 1) + delta_months
    year, month = divmod(total, 12)
    return date(year, month + 1, 1)


def _pct_change(current: int | None, base: int | None) -> float | None:
    """`((current - base) / base) * 100` rounded to 4 dp. Returns None
    when either is missing or `base` is zero (fresh-IPO no-baseline +
    division-by-zero protection)."""
    if current is None or base is None or base == 0:
        return None
    return round((current - base) / base * 100, 4)


async def _enrich_growth_rates(
    db, payload: list[RevenueMonthlyRow],
) -> list[RevenueMonthlyRow]:
    """Compute YoY + MoM percentages for each payload row.

    Strategy:
      1. Index the in-memory payload by (symbol, ts) so consecutive
         months in the same FinMind response feed each other for MoM.
      2. Collect (symbol, prev-month-ts) and (symbol, prev-year-ts)
         pairs we still need from DB and run ONE bulk query.
      3. For each row, compute MoM and YoY against the looked-up
         baselines. None when the baseline row doesn't exist (newly
         listed, gaps in archive, ingest not run far enough back).

    Returns a NEW list of `RevenueMonthlyRow` instances — the input
    payload is not mutated.
    """
    by_key: dict[tuple[str, date], int | None] = {
        (r.symbol, r.ts): r.revenue for r in payload
    }

    needed: set[tuple[str, date]] = set()
    for r in payload:
        for delta in (-1, -12):
            k = (r.symbol, _shift_month(r.ts, delta))
            if k not in by_key:
                needed.add(k)

    if needed:
        symbols = {sym for sym, _ in needed}
        ts_set = {ts for _, ts in needed}
        stmt = select(TwRevenueMonthly).where(
            TwRevenueMonthly.market == MARKET,
            TwRevenueMonthly.symbol.in_(symbols),
            TwRevenueMonthly.ts.in_(ts_set),
        )
        rows = (await db.scalars(stmt)).all()
        for row in rows:
            by_key[(row.symbol, row.ts)] = (
                int(row.revenue) if row.revenue is not None else None
            )

    enriched: list[RevenueMonthlyRow] = []
    for r in payload:
        prev_month = by_key.get((r.symbol, _shift_month(r.ts, -1)))
        prev_year = by_key.get((r.symbol, _shift_month(r.ts, -12)))
        enriched.append(RevenueMonthlyRow(
            market=r.market,
            symbol=r.symbol,
            ts=r.ts,
            revenue=r.revenue,
            revenue_yoy=_pct_change(r.revenue, prev_year),
            revenue_mom=_pct_change(r.revenue, prev_month),
            source=r.source,
        ))
    return enriched


def _month_starts_in_window(today: date, lookback_days: int) -> list[date]:
    """Every first-of-month between `today - lookback_days` and `today`,
    oldest first. These are the only dates a market-wide revenue query
    can match — see `_do_run`."""
    oldest = today - timedelta(days=lookback_days)
    months: list[date] = []
    cursor = date(oldest.year, oldest.month, 1)
    while cursor <= today:
        if cursor >= oldest:
            months.append(cursor)
        cursor = _shift_month(cursor, 1)
    return months


async def _do_run() -> int:
    # FinMind's market-wide mode (`data_id=""`) returns rows for the
    # single `start_date` and silently ignores `end_date` — it is a
    # one-day snapshot, not a range (per-symbol queries do honour
    # ranges). Revenue rows only ever land on a first-of-month, so the
    # old `today - 90d` start matched nothing ~29 days out of 30 and
    # the job logged ok/row_count=0 every day. Ask for each month-first
    # in the window instead.
    items: list[dict] = []
    for month in _month_starts_in_window(date.today(), _LOOKBACK_DAYS):
        items.extend(
            raise_if_silent_denied(
                await finmind.get_monthly_revenue_market_wide(month.isoformat()),
            )
        )
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
        try:
            rev = int(r.get("revenue", 0))
        except (TypeError, ValueError):
            rev = None

        # Growth rates filled in by `_enrich_growth_rates` below — the
        # connector no longer fakes them from FinMind's period-year /
        # period-month integers (which were misinterpreted as yoy/mom
        # before PR #211, producing absurd "+2026% YoY" values).
        payload.append(RevenueMonthlyRow(
            market=MARKET,
            symbol=sym,
            ts=ts,
            revenue=rev,
            revenue_yoy=None,
            revenue_mom=None,
            source="finmind",
        ))

    # One month-first per upstream call means duplicates shouldn't
    # occur, but a repeated (symbol, ts) in a single bulk upsert makes
    # Postgres raise "ON CONFLICT DO UPDATE command cannot affect row a
    # second time" — too sharp an edge to leave to upstream goodwill.
    deduped = list({(r.symbol, r.ts): r for r in payload}.values())
    if not deduped:
        return 0

    async with AsyncSessionLocal() as db:
        enriched = await _enrich_growth_rates(db, deduped)
        return await upsert_revenue_monthly(db, enriched)
