"""Daily TW fundamentals ingest task.

One TWSE call (`get_all_valuation_ratios` / BWIBBU_ALL) returns the full
day's PE / PB / dividend_yield for every TWSE-listed security. We
upsert one row per (symbol, today) into `fundamentals_snapshots` so the
read path can serve recent ratios from DB during a TWSE outage and
operators can chart historical valuations without TWSE retro-queries.

Cadence: daily 06:45 UTC (after the 06:30 OHLCV ingest). Multi-pod
safe via Redis SET-NX lock.

Failure handling and lock semantics mirror `tasks/ingest_margin_tw.py` —
exponential 1h → 6h backoff, last-error preserved across the cooldown
window so admins see why the job was skipped without scraping logs.
"""
import logging
from collections import defaultdict
from datetime import date, timedelta

import httpx
from sqlalchemy import select

import data.tw.finmind_connector as finmind
import data.tw.twse_connector as twse
from cache.redis_cache import acquire_lock, release_lock
from db.session import AsyncSessionLocal
from models.fundamentals_snapshot import FundamentalsSnapshot
from services.tw_health_metrics import compute_health
from services.ingest.repository import (
    FundamentalsSnapshotRow,
    backoff_remaining_seconds,
    clear_failures,
    get_failure_count,
    get_health,
    record_failure,
    record_health,
    upsert_fundamentals_snapshots,
)
from tasks._runner import TaskOutcome, run_ingest_task

log = logging.getLogger(__name__)

JOB_ID = "ingest_fundamentals_tw"

_LOCK_KEY = "lock:ingest_fundamentals_tw"
_LOCK_TTL = 10 * 60   # 10 min — one bulk call typically completes in seconds

# Quarters pulled for the statement-derived payload. TTM needs four
# complete standalone quarters; `_quarterize_cash_flow` needs the prior
# filing of the same calendar year to difference YTD cash-flow facts;
# and ROE averages against the equity five quarters back. Six covers all
# three with a spare for a late filing.
_STATEMENT_QUARTERS = 6

# How far back to look for a payload to carry forward. Comfortably wider
# than a quarter so a long FinMind outage can't strand the strategy, but
# tight enough that a delisted symbol's stale figures age out.
_CARRY_FORWARD_DAYS = 120


_HTTP_HINTS: dict[int, str] = {
    400: "TWSE rejected the request — query may be malformed",
    403: "TWSE refused — UA blocked or geo-restricted",
    429: "TWSE rate-limit — backoff and retry later",
    500: "TWSE upstream error",
    502: "TWSE bad gateway",
    503: "TWSE unavailable",
    504: "TWSE gateway timeout",
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
    row_count, latest_ts = await _do_run()
    return TaskOutcome(row_count=row_count, latest_data_ts=latest_ts)


async def run() -> None:
    """Entry point invoked by APScheduler."""
    await run_ingest_task(
        job_id=JOB_ID, lock_key=_LOCK_KEY, lock_ttl=_LOCK_TTL, log=log,
        acquire_lock=acquire_lock, release_lock=release_lock,
        backoff_remaining_seconds=backoff_remaining_seconds,
        get_failure_count=get_failure_count, get_health=get_health,
        record_health=record_health, record_failure=record_failure,
        clear_failures=clear_failures,
        body=_body, format_error=_format_error,
    )


def _recent_quarter_ends(today: date, count: int) -> list[date]:
    """The `count` most recent quarter-end dates on/before `today`,
    oldest first. A quarter whose filing isn't published yet simply
    returns no rows upstream, so asking is harmless."""
    ends: list[date] = []
    year, quarter = today.year, (today.month - 1) // 3 + 1
    while len(ends) < count:
        month = quarter * 3
        end = date(year, month, 31 if month in (3, 12) else 30)
        if end <= today:
            ends.append(end)
        quarter -= 1
        if quarter == 0:
            year, quarter = year - 1, 4
    return sorted(ends)


async def _load_statement_payloads(as_of: date | None = None) -> dict[str, dict]:
    """Statement-derived fundamentals for every listed company.

    `as_of` selects the quarters that were filed on or before that
    session, so a historical backfill sees the statements a reader
    would have had then rather than today's. `None` means today.

    TWSE's BWIBBU_ALL carries only PE / PB / yield — it has no ROE and no
    cash flow, which is why `payload` was hard-coded to None and why the
    chip_quality daily strategy (needing `roe > 0` and
    `operating_cash_flow > 0`) had never produced a candidate.

    FinMind's statement datasets answer market-wide, so the whole market
    costs 3 calls per quarter rather than 3 per symbol (~6000). The
    statement math is shared with the per-symbol health endpoint via
    `compute_health` so the two can't drift.
    """
    quarters = _recent_quarter_ends(as_of or date.today(), _STATEMENT_QUARTERS)
    income: dict[str, list[dict]] = defaultdict(list)
    balance: dict[str, list[dict]] = defaultdict(list)
    cash: dict[str, list[dict]] = defaultdict(list)

    fetchers = (
        (finmind.get_financials_market_wide, income),
        (finmind.get_balance_sheet_market_wide, balance),
        (finmind.get_cash_flow_market_wide, cash),
    )
    for quarter in quarters:
        for fetch, bucket in fetchers:
            for row in await fetch(quarter.isoformat()):
                symbol = str(row.get("stock_id") or "")
                if symbol:
                    bucket[symbol].append(row)

    payloads: dict[str, dict] = {}
    for symbol in set(income) | set(balance) | set(cash):
        summary = compute_health(
            symbol, income.get(symbol, []), balance.get(symbol, []),
            cash.get(symbol, []),
        )["summary"]
        # Only keep what we actually derived. Writing an explicit None
        # would defeat the readers' `payload.get(key, default)` — for
        # `debt_ratio` that flips the missing-data default from 100
        # (worst) to 0 (best) and silently flatters the score.
        candidate = {
            "roe": summary.get("latest_roe"),
            "operating_cash_flow": summary.get("ttm_operating_cf"),
            "ocf_positive_quarters": summary.get("cf_positive_streak_4q"),
            "debt_ratio": summary.get("latest_debt_ratio"),
        }
        derived = {k: v for k, v in candidate.items() if v is not None}
        if derived:
            payloads[symbol] = derived

    log.info(
        "ingest_fundamentals_tw.statements_loaded",
        extra={"quarters": len(quarters), "symbols_with_payload": len(payloads)},
    )
    return payloads


async def _carry_forward_payloads(db) -> dict[str, dict]:
    """The most recent payload already stored per symbol.

    This job rewrites every symbol's row daily, so a statement pull that
    comes back empty — FinMind down, hourly quota spent — would
    overwrite a good payload with None and silently un-qualify every
    chip_quality candidate until the next successful run. Statements
    only change quarterly, so carrying the last known one forward is
    what the data actually means, not a workaround.
    """
    cutoff = date.today() - timedelta(days=_CARRY_FORWARD_DAYS)
    rows = (await db.execute(
        select(FundamentalsSnapshot.symbol, FundamentalsSnapshot.payload)
        .where(
            FundamentalsSnapshot.market == "TW",
            FundamentalsSnapshot.as_of >= cutoff,
            FundamentalsSnapshot.payload.is_not(None),
        )
        .order_by(FundamentalsSnapshot.as_of.asc())
    )).all()
    # Ascending scan, last write wins → newest payload per symbol.
    return {symbol: payload for symbol, payload in rows if payload}


async def _do_run() -> tuple[int, date | None]:
    """Fetch valuation ratios + upsert. Empty TWSE response returns
    (0, None) — recorded as ok=True row_count=0 (an empty BWIBBU_ALL
    is rare but legitimate, e.g. early-morning pre-publish window)."""
    ratios = await twse.get_all_valuation_ratios()
    if not ratios:
        log.warning("ingest_fundamentals_tw.empty_result")
        return 0, None

    # Statements are a bonus, not the job's contract: PE/PB/yield must
    # still land if FinMind is down or out of quota.
    try:
        payloads = await _load_statement_payloads()
    except Exception as exc:
        log.warning(
            "ingest_fundamentals_tw.statements_failed",
            extra={"error": str(exc)},
        )
        payloads = {}

    async with AsyncSessionLocal() as db:
        carried = await _carry_forward_payloads(db)
    for symbol, payload in carried.items():
        payloads.setdefault(symbol, payload)
    log.info(
        "ingest_fundamentals_tw.payloads_resolved",
        extra={"fresh_or_carried": len(payloads), "carried_available": len(carried)},
    )

    today = date.today()
    rows = [
        FundamentalsSnapshotRow(
            market="TW",
            symbol=symbol,
            as_of=today,
            pe_ratio=v.get("pe_ratio"),
            pb_ratio=v.get("pb_ratio"),
            dividend_yield=v.get("dividend_yield"),
            eps=None,
            revenue=None,
            payload=payloads.get(symbol),
            source="twse",
        )
        for symbol, v in ratios.items()
        if symbol  # paranoia guard against empty keys
    ]

    async with AsyncSessionLocal() as db:
        written = await upsert_fundamentals_snapshots(db, rows)

    return written, today
