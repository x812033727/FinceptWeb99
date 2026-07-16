"""TW holdings + market-institutional aggregates ingest.

Combined cron for two FinMind sponsor-tier datasets that share a
discussion-context block:

  * `TaiwanStockHoldingSharesPer` (集保股權分散表) — fan-out into
    long-form (market, symbol, ts, bucket_id) rows. Weekly.
  * `TaiwanStockTotalInstitutionalInvestors` (全市場三大法人) —
    aggregated into one row per (market, ts). Daily.

The two datasets disagree about dates: holding-shares is a one-day
market-wide snapshot that must be walked day by day, while
total-institutional honours start/end ranges. Hence the asymmetry
between the two sub-steps.

Same shape as `tasks.ingest_risk_signals_tw` (PR #192): per-substep
fail-soft, paywalled subset reports `skipped:` while the rest still
writes, transient outage arms backoff for the whole job.

Schedule: 19:30 Taipei (11:30 UTC), after the risk-signals cron.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta

import httpx
from sqlalchemy import select

import data.tw.finmind_connector as finmind
from cache.redis_cache import acquire_lock, release_lock
from data.tw.finmind_connector import FinMindSilentDeny
from data.tw.finmind_paywall import (
    extract_body_message as _extract_body_message,
    looks_like_paywall as _looks_like_paywall,
    raise_if_silent_denied,
)
from db.session import AsyncSessionLocal
from models.tw_holdings_aggregates import TwStockShareholding
from tasks._market_wide import collect_market_wide, pending_days_since_newest
from services.ingest.repository import (
    MarketInstitutionalRow,
    ShareholdingRow,
    backoff_remaining_seconds,
    clear_failures,
    get_failure_count,
    get_health,
    record_failure,
    record_health,
    upsert_market_institutional_daily,
    upsert_shareholdings,
)

log = logging.getLogger(__name__)

JOB_ID = "ingest_holdings_aggregates_tw"
MARKET = "TW"

_LOCK_KEY = "lock:ingest_holdings_aggregates_tw"
_LOCK_TTL = 10 * 60
# Shareholding publishes weekly. 60 days catches several recent
# publications without bloating the response.
_SHAREHOLDING_LOOKBACK_DAYS = 60
# Total institutional is daily — 30 days context.
_TOTAL_INSTITUTIONAL_LOOKBACK_DAYS = 30


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


def _to_float(v: object) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return None


def _format_error(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code} {exc.response.reason_phrase or '?'}"
    if isinstance(exc, httpx.HTTPError):
        return f"http error: {exc}"
    return f"unexpected: {exc}"


# ── shareholding sub-step ────────────────────────────────────────

# 集保's holding-size levels, in the order TDCC numbers them. The
# reader contract is already fixed: `read_holdings_concentration_trend`
# documents "bucket_id 1-15 ascending by holding-size, 15 = > 1,000
# lots which is the canonical 千張大戶 threshold" and sums 13/14/15 for
# its concentration signal — so these ids are not ours to renumber.
#
# Mapping by label rather than by response order: FinMind returns the
# levels in order today, but a silently reordered response would
# mis-assign every bucket and the numbers would still look plausible.
# An unknown label is dropped loudly (see below) instead of guessing.
_HOLDING_LEVEL_BUCKETS: dict[str, int] = {
    "1-999": 1,
    "1,000-5,000": 2,
    "5,001-10,000": 3,
    "10,001-15,000": 4,
    "15,001-20,000": 5,
    "20,001-30,000": 6,
    "30,001-40,000": 7,
    "40,001-50,000": 8,
    "50,001-100,000": 9,
    "100,001-200,000": 10,
    "200,001-400,000": 11,
    "400,001-600,000": 12,
    "600,001-800,000": 13,
    "800,001-1,000,000": 14,
    "more than 1,000,001": 15,   # 千張大戶
}

# Not buckets: `total` is the sum of 1-15 (a 100% row that would
# double any consumer's total), and 差異數調整 is TDCC's reconciliation
# line. Both are derivable or meaningless as distribution rows, so
# they're dropped quietly rather than counted as unknown levels.
_HOLDING_LEVEL_NON_BUCKETS = frozenset({"total", "差異數調整（說明4）"})


def _normalize_shareholding(items: list[dict]) -> list[ShareholdingRow]:
    """`TaiwanStockHoldingSharesPer` rows carry one holding-size level
    per row — map the level onto its bucket id and fan out into
    ShareholdingRow. Rows missing date or symbol drop silently.
    """
    out: list[ShareholdingRow] = []
    unknown: set[str] = set()
    for r in items:
        sym = (r.get("stock_id") or "").strip()
        if not sym:
            continue
        ts = _parse_date(r.get("date"))
        if ts is None:
            continue
        level = (r.get("HoldingSharesLevel") or "").strip()
        if level in _HOLDING_LEVEL_NON_BUCKETS:
            continue
        bucket_id = _HOLDING_LEVEL_BUCKETS.get(level)
        if bucket_id is None:
            unknown.add(level)
            continue
        out.append(ShareholdingRow(
            market=MARKET, symbol=sym, ts=ts,
            bucket_id=bucket_id,
            bucket_label=level[:40],
            holders_count=_to_int(r.get("people")),
            shares_count=_to_int(r.get("unit")),
            shares_percent=_to_float(r.get("percent")),
            source="finmind",
        ))
    if unknown:
        # A renamed or added level means the mapping is stale and the
        # archive is quietly losing a bucket — the exact class of
        # silence that kept this table empty. Say so.
        log.warning(
            "ingest_holdings_aggregates_tw.unknown_holding_levels",
            extra={"levels": sorted(unknown)},
        )
    return out


async def _archived_shareholding_days() -> set[date]:
    cutoff = date.today() - timedelta(days=_SHAREHOLDING_LOOKBACK_DAYS)
    async with AsyncSessionLocal() as db:
        rows = await db.scalars(
            select(TwStockShareholding.ts)
            .where(
                TwStockShareholding.market == MARKET,
                TwStockShareholding.ts >= cutoff,
            )
            .distinct()
        )
        return set(rows.all())


async def _ingest_shareholding() -> dict[str, object]:
    # Market-wide is a one-day snapshot here (an end_date returns
    # nothing at all), so the window has to be walked a day at a time.
    # Weekly publication means `pending_market_days` would re-ask the
    # non-publication weekdays forever — anchor on the newest archived
    # publication instead. See tasks/_market_wide.
    days = pending_days_since_newest(
        date.today(), _SHAREHOLDING_LOOKBACK_DAYS,
        await _archived_shareholding_days(),
    )
    if not days:
        return {"rows": 0, "fetched": 0}
    items = raise_if_silent_denied(
        await collect_market_wide(finmind.get_shareholding_market_wide, days),
    )
    payload = _normalize_shareholding(items)
    # One publication per requested day means a repeated key shouldn't
    # happen, but one costs the whole batch to Postgres' "cannot affect
    # row a second time".
    payload = list({(r.symbol, r.ts, r.bucket_id): r for r in payload}.values())
    if not payload:
        return {"rows": 0, "fetched": len(items)}
    async with AsyncSessionLocal() as db:
        return {"rows": await upsert_shareholdings(db, payload), "fetched": len(items)}


# ── total-institutional sub-step ─────────────────────────────────

def _aggregate_total_institutional(items: list[dict]) -> list[MarketInstitutionalRow]:
    """FinMind's `TaiwanStockTotalInstitutionalInvestors` typically
    returns one row per (date, investor_type) — fold into one row
    per date with three pairs of buy/sell. The `name` field carries
    the investor type ("Foreign_Investor" / "Investment_Trust" /
    "Dealer" + variants).
    """
    by_date: dict[date, dict[str, int]] = defaultdict(lambda: {
        "foreign_buy": 0, "foreign_sell": 0,
        "sitc_buy": 0, "sitc_sell": 0,
        "dealer_buy": 0, "dealer_sell": 0,
    })
    for r in items:
        ts = _parse_date(r.get("date"))
        if ts is None:
            continue
        name = (r.get("name") or "").lower()
        buy = _to_int(r.get("buy")) or 0
        sell = _to_int(r.get("sell")) or 0
        if "foreign" in name or "外資" in name:
            by_date[ts]["foreign_buy"] += buy
            by_date[ts]["foreign_sell"] += sell
        elif "trust" in name or "投信" in name:
            by_date[ts]["sitc_buy"] += buy
            by_date[ts]["sitc_sell"] += sell
        elif "dealer" in name or "自營" in name:
            by_date[ts]["dealer_buy"] += buy
            by_date[ts]["dealer_sell"] += sell
        # Unknown name → silently drop; future investor types added
        # by FinMind would need explicit handling.
    return [
        MarketInstitutionalRow(
            market=MARKET, ts=ts,
            foreign_buy=v["foreign_buy"], foreign_sell=v["foreign_sell"],
            sitc_buy=v["sitc_buy"], sitc_sell=v["sitc_sell"],
            dealer_buy=v["dealer_buy"], dealer_sell=v["dealer_sell"],
            source="finmind",
        )
        for ts, v in by_date.items()
    ]


async def _ingest_total_institutional() -> dict[str, object]:
    start = (date.today() - timedelta(days=_TOTAL_INSTITUTIONAL_LOOKBACK_DAYS)).isoformat()
    items = raise_if_silent_denied(
        await finmind.get_total_institutional_market_wide(start),
    )
    payload = _aggregate_total_institutional(items)
    if not payload:
        return {"rows": 0, "fetched": len(items)}
    async with AsyncSessionLocal() as db:
        return {
            "rows": await upsert_market_institutional_daily(db, payload),
            "fetched": len(items),
        }


_SUBSTEPS = (
    ("shareholding", _ingest_shareholding),
    ("total_institutional", _ingest_total_institutional),
)


# ── orchestrator ──────────────────────────────────────────────────

async def run() -> None:
    if not await acquire_lock(_LOCK_KEY, _LOCK_TTL):
        log.info("ingest_holdings_aggregates_tw.skipped_lock_held")
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
        sub_silent_msgs: list[str] = []
        for name, fn in _SUBSTEPS:
            try:
                results[name] = await fn()
            except FinMindSilentDeny as exc:
                sub_paywalls.append(f"{name}: {exc.body_msg[:80]}")
                sub_silent_msgs.append(f"{name}: {exc.body_msg[:80]}")
                log.warning(
                    "ingest_holdings_aggregates_tw.silent_deny",
                    extra={"substep": name, "upstream_message": exc.body_msg},
                )
            except Exception as exc:
                body_msg = _extract_body_message(exc)
                if _looks_like_paywall(body_msg):
                    sub_paywalls.append(f"{name}: {body_msg[:80]}")
                    log.warning(
                        "ingest_holdings_aggregates_tw.paywalled",
                        extra={"substep": name, "upstream_message": body_msg},
                    )
                else:
                    sub_failures.append(f"{name}: {_format_error(exc)}")
                    log.warning(
                        "ingest_holdings_aggregates_tw.substep_failed",
                        extra={"substep": name, "error": str(exc)},
                    )

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
            silent_payload = (
                "; ".join(sub_silent_msgs) if sub_silent_msgs else None
            )
            await record_health(
                JOB_ID, ok=False, row_count=total_rows,
                error=(
                    f"skipped: FinMind paywalled — {'; '.join(sub_paywalls)}. "
                    f"Successful sub-steps: {', '.join(success_names) or 'none'}."
                ),
                silent_deny=silent_payload,
                source=("finmind" if silent_payload else None),
            )
            return

        await clear_failures(JOB_ID)
        log.info(
            "ingest_holdings_aggregates_tw.done",
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
