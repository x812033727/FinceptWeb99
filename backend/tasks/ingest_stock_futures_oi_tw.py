"""Daily TW 個股期貨 三大法人未平倉 ingest (PR #282).

`TaiwanFuturesInstitutionalInvestors` from FinMind sponsor tier.
One market-wide call (`data_id=""`) returns rows for every futures
contract — index futures (TX/MTX/TE/TF) AND every individual stock
future (CDF/CFF/IIF/...). We filter to stock-futures only, then
aggregate the per-investor-type rows into one row per
(stock, date) with foreign / SITC / dealer columns.

Schedule: 18:45 Taipei (10:45 UTC), after the buyback +
govt_bank_flow crons so the post-close FinMind cluster spreads
out predictably and a single 429 doesn't tip multiple jobs into
backoff at once.

Look-ahead protection: futures OI rows are insert-only on
FinMind's side (no retroactive corrections to long/short/net),
so the read tier doesn't need backtest masking. The cron's
`_LOOKBACK_DAYS=14` keeps the pull window to roughly 2 trading
weeks — enough for the read tier's 5-trading-day delta
calculation plus headroom for FinMind backfilling a missed bar.
"""
import logging
from collections import defaultdict
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
    StockFuturesOiRow,
    backoff_remaining_seconds,
    clear_failures,
    get_failure_count,
    get_health,
    record_failure,
    record_health,
    upsert_stock_futures_oi,
)

log = logging.getLogger(__name__)

JOB_ID = "ingest_stock_futures_oi_tw"
MARKET = "TW"

_LOCK_KEY = "lock:ingest_stock_futures_oi_tw"
_LOCK_TTL = 5 * 60
_LOOKBACK_DAYS = 14

# Index-futures contract codes — exclude these so the table only
# carries individual-stock futures (which is what we want for
# per-stock smart-money signals). The TAIFEX-published index
# futures lineup is small + stable; hardcoding is safer than a
# regex pattern that might accidentally drop a stock future
# whose code starts with "T".
_INDEX_FUTURES_CONTRACTS: frozenset[str] = frozenset({
    "TX",   # 台股期貨 (TAIEX)
    "MTX",  # 小型台指
    "TE",   # 電子期
    "TF",   # 金融期
    "T5F",  # 台灣 50 期
    "XIF",  # 非金電期
    "GTF",  # 櫃買期
    "SHF",  # 航運期
    "SOF",  # 半導體 30 期
})


# Map FinMind's `institutional_investors` Chinese labels to the
# canonical column-prefix used in `tw_stock_futures_oi`. FinMind
# returns the same labels for both stock futures + index futures,
# so the mapping is shared.
_INVESTOR_PREFIX: dict[str, str] = {
    "外資": "fini",
    "外資及陸資": "fini",   # appears in some date ranges
    "投信": "sitc",
    "自營商": "dealer",
    "自營商(自行買賣)": "dealer",
    "自營商(避險)": "dealer",
}


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


def _acc(a: int | None, b: int | None) -> int | None:
    """Sum two optional ints. None+None → None (no data on either
    side); otherwise treat None as 0 so a missing sub-type doesn't
    blank the running total."""
    if a is None and b is None:
        return None
    return (a or 0) + (b or 0)


def _aggregate_to_per_stock_rows(
    items: list[dict],
) -> list[StockFuturesOiRow]:
    """FinMind returns one row per (date, contract, investor_type).
    We collapse those into one row per (date, stock) with three
    investor-type triplets (long_oi / short_oi / net_oi) as
    columns, mirroring `tw_institutional_daily`'s shape.

    Skip rules:
      - Index futures (TX / MTX / ...) — not single-stock
      - Rows missing date / futures_id — can't be slotted
      - Rows whose investor-type label isn't in `_INVESTOR_PREFIX`
        (e.g. some FinMind ranges include 'Total' aggregate rows)
    """
    # Bucket key: (ts, symbol) — nested dict per investor type so
    # we can drop in long/short/net values as we encounter them.
    buckets: dict[
        tuple[date, str], dict[str, dict[str, int | None]]
    ] = defaultdict(lambda: {"fini": {}, "sitc": {}, "dealer": {}})
    contracts: dict[tuple[date, str], str] = {}

    for r in items:
        contract = (r.get("futures_id") or "").strip()
        if not contract or contract in _INDEX_FUTURES_CONTRACTS:
            continue
        # Underlying stock — FinMind populates `stock_id` for
        # individual stock futures. When absent, fall back to the
        # contract code itself so we don't drop the row.
        symbol = (r.get("stock_id") or "").strip() or contract
        ts = _parse_date(r.get("date"))
        if ts is None:
            continue
        investor_label = (r.get("institutional_investors") or "").strip()
        prefix = _INVESTOR_PREFIX.get(investor_label)
        if prefix is None:
            continue

        long_oi = _to_int(r.get("long_open_interest_balance_volume"))
        short_oi = _to_int(r.get("short_open_interest_balance_volume"))
        net_oi: int | None
        if long_oi is not None and short_oi is not None:
            net_oi = long_oi - short_oi
        else:
            net_oi = None

        bucket = buckets[(ts, symbol)]
        # 自營商 has two sub-types (自行買賣 / 避險) — accumulate
        # rather than overwrite so the dealer column reflects the
        # combined position the persona would care about. `_acc`
        # treats two None inputs as None (we have NO data) but
        # promotes a single None on either side to 0 so a missing
        # sub-type doesn't blank the running total.
        prev = bucket[prefix]
        bucket[prefix] = {
            "long":  _acc(prev.get("long"),  long_oi),
            "short": _acc(prev.get("short"), short_oi),
            "net":   _acc(prev.get("net"),   net_oi),
        }
        contracts[(ts, symbol)] = contract

    rows: list[StockFuturesOiRow] = []
    for (ts, symbol), per_inv in buckets.items():
        rows.append(StockFuturesOiRow(
            market=MARKET,
            symbol=symbol,
            contract_id=contracts[(ts, symbol)],
            ts=ts,
            fini_long_oi=per_inv["fini"].get("long"),
            fini_short_oi=per_inv["fini"].get("short"),
            fini_net_oi=per_inv["fini"].get("net"),
            sitc_long_oi=per_inv["sitc"].get("long"),
            sitc_short_oi=per_inv["sitc"].get("short"),
            sitc_net_oi=per_inv["sitc"].get("net"),
            dealer_long_oi=per_inv["dealer"].get("long"),
            dealer_short_oi=per_inv["dealer"].get("short"),
            dealer_net_oi=per_inv["dealer"].get("net"),
            source="finmind",
        ))
    return rows


async def run() -> None:
    if not await acquire_lock(_LOCK_KEY, _LOCK_TTL):
        log.info("ingest_stock_futures_oi_tw.skipped_lock_held")
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
                    "ingest_stock_futures_oi_tw.paywalled",
                    extra={"upstream_message": body_msg},
                )
                await record_health(
                    JOB_ID, ok=False, row_count=0,
                    error=(
                        "skipped: FinMind paywalled this dataset "
                        "(TaiwanFuturesInstitutionalInvestors needs paid "
                        "sponsor tier). Existing tw_stock_futures_oi rows "
                        f"preserved. Upstream message: {body_msg}"
                    ),
                )
                return
            detail = _format_error(exc)
            failures = await record_failure(JOB_ID)
            log.warning(
                "ingest_stock_futures_oi_tw.failed",
                extra={"error": detail, "failures": failures},
            )
            await record_health(
                JOB_ID, ok=False, row_count=0,
                error=f"{detail} (failure #{failures}; auto-backoff armed)",
            )
            return

        await clear_failures(JOB_ID)
        log.info(
            "ingest_stock_futures_oi_tw.done",
            extra={"rows_processed": row_count},
        )
        await record_health(JOB_ID, ok=True, row_count=row_count)
    finally:
        await release_lock(_LOCK_KEY)


async def _do_run() -> int:
    start = (date.today() - timedelta(days=_LOOKBACK_DAYS)).isoformat()
    items = await finmind.get_stock_futures_institutional_market_wide(start)
    if not items:
        return 0

    rows = _aggregate_to_per_stock_rows(items)
    if not rows:
        return 0

    async with AsyncSessionLocal() as db:
        return await upsert_stock_futures_oi(db, rows)
