"""Tests for `tasks.ingest_holdings_aggregates_tw` — combined cron
for股權分散 + 全市場三大法人 datasets.

Pinned scenarios:
  - Shareholding rows fan out into long-form bucket entries
  - Total-institutional folds per-investor-type rows into one date row
  - Per-substep paywall isolation (mirrors PR #192's pattern)
  - Repository read helpers compute the expected aggregates
"""
from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.tw_holdings_aggregates import (
    TwMarketInstitutionalDaily,
    TwStockShareholding,
)

# 2330's real TaiwanStockHoldingSharesPer publication for 2026-07-09,
# copied from the live API rather than invented. The fixture this
# replaced used a shape no FinMind dataset has ever returned
# (`level` / `label` / `holders` / `shares`) — it went green while
# production wrote zero rows, because it encoded the same wrong
# assumption as the code it was checking.
_REAL_2330_LEVELS = [
    ("1-999",               2374325,    274129930, 1.05),
    ("1,000-5,000",          438862,    837595565, 3.22),
    ("5,001-10,000",          51151,    366679448, 1.41),
    ("10,001-15,000",         16942,    207754181, 0.8),
    ("15,001-20,000",          7970,    140254310, 0.54),
    ("20,001-30,000",          7636,    187068531, 0.72),
    ("30,001-40,000",          3578,    124047689, 0.47),
    ("40,001-50,000",          2025,     91243367, 0.35),
    ("50,001-100,000",         4070,    284390420, 1.09),
    ("100,001-200,000",        2019,    282421517, 1.08),
    ("200,001-400,000",        1340,    374946281, 1.44),
    ("400,001-600,000",         566,    277085423, 1.06),
    ("600,001-800,000",         347,    240775918, 0.92),
    ("800,001-1,000,000",       220,    196419001, 0.75),
    ("more than 1,000,001",    1478,  22047558486, 85.01),
    ("total",               2912529,  25932370067, 100.0),
    ("差異數調整（說明4）",          0,            0, 0.0),
]


def _real_levels(symbol: str) -> list[dict]:
    return [
        {"stock_id": symbol, "HoldingSharesLevel": lvl,
         "people": people, "unit": unit, "percent": pct}
        for lvl, people, unit, pct in _REAL_2330_LEVELS
    ]


def _holding_shares_stub(by_date: dict[str, list[dict]]):
    """Market-wide answers with one publication date's rows; the job now
    asks once per day. Serve each date only its own rows, as the API
    does — a stub returning everything for every date would let each row
    be counted once per call."""
    async def _fetch(day_iso: str) -> list[dict]:
        return [{**r, "date": day_iso} for r in by_date.get(day_iso, [])]

    return _fetch


def _latest_weekday() -> date:
    """Match the task's weekday walk even when the suite runs on a weekend."""
    today = date.today()
    return today - timedelta(days=max(0, today.weekday() - 4))


@pytest.fixture
def patch_session(db_session: AsyncSession):
    class _CM:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *exc):
            return False

    with patch(
        "tasks.ingest_holdings_aggregates_tw.AsyncSessionLocal",
        return_value=_CM(),
    ):
        yield


def _http_status_error(status_code: int, body_msg: str) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.finmindtrade.com/api/v4/data")
    response = httpx.Response(
        status_code, request=request,
        json={"msg": body_msg, "status": status_code},
    )
    return httpx.HTTPStatusError(
        f"HTTP {status_code}", request=request, response=response,
    )


# ── happy path: both datasets ─────────────────────────────────────

@pytest.mark.asyncio
async def test_shareholding_fans_out_to_long_form(
    patch_session, db_session: AsyncSession,
):
    from tasks import ingest_holdings_aggregates_tw

    publication_day = _latest_weekday().isoformat()
    with patch("tasks.ingest_holdings_aggregates_tw.acquire_lock",
               AsyncMock(return_value=True)), \
         patch("tasks.ingest_holdings_aggregates_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_holdings_aggregates_tw.backoff_remaining_seconds",
               AsyncMock(return_value=0)), \
         patch("tasks.ingest_holdings_aggregates_tw.clear_failures", AsyncMock()), \
         patch("tasks.ingest_holdings_aggregates_tw.record_health", AsyncMock()), \
         patch("tasks.ingest_holdings_aggregates_tw.finmind.get_shareholding_market_wide",
               _holding_shares_stub({publication_day: _real_levels("2330")})), \
         patch("tasks.ingest_holdings_aggregates_tw.finmind.get_total_institutional_market_wide",
               AsyncMock(return_value=[])):
        await ingest_holdings_aggregates_tw.run()

    rows = (await db_session.scalars(
        select(TwStockShareholding).where(TwStockShareholding.symbol == "2330")
    )).all()
    # 15 distribution buckets — `total` and 差異數調整 are not buckets.
    assert len(rows) == 15
    by_bucket = {r.bucket_id: r for r in rows}
    assert by_bucket[1].bucket_label == "1-999"
    assert int(by_bucket[1].holders_count) == 2_374_325
    assert int(by_bucket[1].shares_count) == 274_129_930
    # bucket 15 is 千張大戶 — the contract `read_holdings_concentration_trend`
    # sums for its concentration signal.
    assert by_bucket[15].bucket_label == "more than 1,000,001"
    assert float(by_bucket[15].shares_percent) == 85.01


@pytest.mark.asyncio
async def test_total_and_adjustment_levels_are_not_stored_as_buckets(
    patch_session, db_session: AsyncSession,
):
    """`total` is 100% — a consumer summing buckets would double its
    numbers — and 差異數調整 is TDCC's reconciliation line. Neither is a
    distribution bucket."""
    from tasks import ingest_holdings_aggregates_tw

    publication_day = _latest_weekday().isoformat()
    with patch("tasks.ingest_holdings_aggregates_tw.acquire_lock",
               AsyncMock(return_value=True)), \
         patch("tasks.ingest_holdings_aggregates_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_holdings_aggregates_tw.backoff_remaining_seconds",
               AsyncMock(return_value=0)), \
         patch("tasks.ingest_holdings_aggregates_tw.clear_failures", AsyncMock()), \
         patch("tasks.ingest_holdings_aggregates_tw.record_health", AsyncMock()), \
         patch("tasks.ingest_holdings_aggregates_tw.finmind.get_shareholding_market_wide",
               _holding_shares_stub({publication_day: _real_levels("2330")})), \
         patch("tasks.ingest_holdings_aggregates_tw.finmind.get_total_institutional_market_wide",
               AsyncMock(return_value=[])):
        await ingest_holdings_aggregates_tw.run()

    rows = (await db_session.scalars(select(TwStockShareholding))).all()
    labels = {r.bucket_label for r in rows}
    assert "total" not in labels
    assert "差異數調整（說明4）" not in labels
    assert max(r.bucket_id for r in rows) == 15
    # The distribution must still add up to ~100% on its own.
    assert 99.0 <= sum(float(r.shares_percent) for r in rows) <= 101.0


@pytest.mark.asyncio
async def test_unknown_holding_level_is_dropped_and_logged(
    patch_session, db_session: AsyncSession, caplog,
):
    """A renamed or added level means the mapping is stale and the
    archive is quietly losing a bucket — exactly the silence that kept
    this table empty for months."""
    import logging

    from tasks.ingest_holdings_aggregates_tw import _normalize_shareholding

    rows = [
        {"stock_id": "2330", "date": "2026-07-09",
         "HoldingSharesLevel": "1-999", "people": 1, "unit": 2, "percent": 0.5},
        {"stock_id": "2330", "date": "2026-07-09",
         "HoldingSharesLevel": "brand new level", "people": 1, "unit": 2,
         "percent": 0.5},
    ]
    with caplog.at_level(logging.WARNING):
        out = _normalize_shareholding(rows)

    assert [r.bucket_id for r in out] == [1]
    assert "unknown_holding_levels" in caplog.text


@pytest.mark.asyncio
async def test_total_institutional_folds_investor_types_into_one_row(
    patch_session, db_session: AsyncSession,
):
    """FinMind returns one row per (date, investor_type); cron
    aggregates into one row per date."""
    from tasks import ingest_holdings_aggregates_tw

    today = date.today()
    payload = [
        {"date": today.isoformat(), "name": "Foreign_Investor",
         "buy": 100_000_000_000, "sell": 80_000_000_000},
        {"date": today.isoformat(), "name": "Investment_Trust",
         "buy": 5_000_000_000, "sell": 3_000_000_000},
        {"date": today.isoformat(), "name": "Dealer_self",
         "buy": 2_000_000_000, "sell": 1_500_000_000},
    ]
    with patch("tasks.ingest_holdings_aggregates_tw.acquire_lock",
               AsyncMock(return_value=True)), \
         patch("tasks.ingest_holdings_aggregates_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_holdings_aggregates_tw.backoff_remaining_seconds",
               AsyncMock(return_value=0)), \
         patch("tasks.ingest_holdings_aggregates_tw.clear_failures", AsyncMock()), \
         patch("tasks.ingest_holdings_aggregates_tw.record_health", AsyncMock()), \
         patch("tasks.ingest_holdings_aggregates_tw.finmind.get_shareholding_market_wide",
               AsyncMock(return_value=[])), \
         patch("tasks.ingest_holdings_aggregates_tw.finmind.get_total_institutional_market_wide",
               AsyncMock(return_value=payload)):
        await ingest_holdings_aggregates_tw.run()

    rows = (await db_session.scalars(
        select(TwMarketInstitutionalDaily)
    )).all()
    assert len(rows) == 1
    r = rows[0]
    assert r.ts == today
    assert int(r.foreign_buy) == 100_000_000_000
    assert int(r.foreign_sell) == 80_000_000_000
    assert int(r.sitc_buy) == 5_000_000_000
    assert int(r.dealer_sell) == 1_500_000_000


@pytest.mark.asyncio
async def test_chinese_investor_type_names_also_match(
    patch_session, db_session: AsyncSession,
):
    """FinMind sometimes returns Chinese investor-type names instead
    of English. Both should aggregate correctly."""
    from tasks import ingest_holdings_aggregates_tw

    today = date.today()
    payload = [
        {"date": today.isoformat(), "name": "外資", "buy": 1, "sell": 2},
        {"date": today.isoformat(), "name": "投信", "buy": 3, "sell": 4},
        {"date": today.isoformat(), "name": "自營商", "buy": 5, "sell": 6},
    ]
    with patch("tasks.ingest_holdings_aggregates_tw.acquire_lock",
               AsyncMock(return_value=True)), \
         patch("tasks.ingest_holdings_aggregates_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_holdings_aggregates_tw.backoff_remaining_seconds",
               AsyncMock(return_value=0)), \
         patch("tasks.ingest_holdings_aggregates_tw.clear_failures", AsyncMock()), \
         patch("tasks.ingest_holdings_aggregates_tw.record_health", AsyncMock()), \
         patch("tasks.ingest_holdings_aggregates_tw.finmind.get_shareholding_market_wide",
               AsyncMock(return_value=[])), \
         patch("tasks.ingest_holdings_aggregates_tw.finmind.get_total_institutional_market_wide",
               AsyncMock(return_value=payload)):
        await ingest_holdings_aggregates_tw.run()

    rows = (await db_session.scalars(
        select(TwMarketInstitutionalDaily)
    )).all()
    assert len(rows) == 1
    assert int(rows[0].foreign_buy) == 1
    assert int(rows[0].sitc_buy) == 3
    assert int(rows[0].dealer_buy) == 5


@pytest.mark.asyncio
async def test_paywalled_substep_does_not_block_other(
    patch_session, db_session: AsyncSession,
):
    from tasks import ingest_holdings_aggregates_tw

    today = date.today()
    paywall_msg = "Your level is register. Please update your user level."
    record_failure_mock = AsyncMock()
    health_mock = AsyncMock()
    with patch("tasks.ingest_holdings_aggregates_tw.acquire_lock",
               AsyncMock(return_value=True)), \
         patch("tasks.ingest_holdings_aggregates_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_holdings_aggregates_tw.backoff_remaining_seconds",
               AsyncMock(return_value=0)), \
         patch("tasks.ingest_holdings_aggregates_tw.record_failure", record_failure_mock), \
         patch("tasks.ingest_holdings_aggregates_tw.clear_failures", AsyncMock()), \
         patch("tasks.ingest_holdings_aggregates_tw.record_health", health_mock), \
         patch("tasks.ingest_holdings_aggregates_tw.finmind.get_shareholding_market_wide",
               AsyncMock(side_effect=_http_status_error(400, paywall_msg))), \
         patch("tasks.ingest_holdings_aggregates_tw.finmind.get_total_institutional_market_wide",
               AsyncMock(return_value=[
                   {"date": today.isoformat(), "name": "Foreign_Investor",
                    "buy": 1, "sell": 2}
               ])):
        await ingest_holdings_aggregates_tw.run()

    record_failure_mock.assert_not_called()
    rows = (await db_session.scalars(
        select(TwMarketInstitutionalDaily)
    )).all()
    assert len(rows) == 1  # still wrote
    kwargs = health_mock.await_args.kwargs
    assert "skipped" in kwargs["error"].lower()
    assert "shareholding" in kwargs["error"].lower()


# ── repository read helpers ──────────────────────────────────────

@pytest.mark.asyncio
async def test_read_latest_shareholding_returns_buckets_for_latest_date(
    db_session: AsyncSession,
):
    from services.ingest.repository import (
        ShareholdingRow, read_latest_shareholding, upsert_shareholdings,
    )
    today = date.today()
    yesterday = date.today().replace(day=max(today.day - 1, 1))
    await upsert_shareholdings(db_session, [
        # Today's snapshot — should be returned, all 3 buckets
        ShareholdingRow("TW", "2330", today, 1, "1-999股",
                        100_000, 5_000_000, 0.5, "finmind"),
        ShareholdingRow("TW", "2330", today, 2, "1000-5000股",
                        50_000, 30_000_000, 3.0, "finmind"),
        ShareholdingRow("TW", "2330", today, 3, "1000張+",
                        50, 800_000_000, 80.0, "finmind"),
        # Yesterday's snapshot — should NOT be returned
        ShareholdingRow("TW", "2330", yesterday, 1, "1-999股",
                        99_000, 4_900_000, 0.49, "finmind"),
    ])
    rows = await read_latest_shareholding(db_session, market="TW", symbol="2330")
    # Latest date = today, all 3 buckets present, ordered by bucket_id
    assert len(rows) == 3
    assert [r["bucket_id"] for r in rows] == [1, 2, 3]
    assert all(r["ts"] == today.isoformat() for r in rows)


@pytest.mark.asyncio
async def test_holdings_concentration_trend_returns_none_for_single_publication(
    db_session: AsyncSession,
):
    """Need ≥ 2 weekly snapshots to compute a trend. Single
    publication → None so the discussion ctx folds the field out
    instead of emitting a misleading "stable" with no movement
    information."""
    from datetime import timedelta as _td

    from services.ingest.repository import (
        ShareholdingRow,
        read_holdings_concentration_trend,
        upsert_shareholdings,
    )
    anchor = date(2026, 4, 26)
    await upsert_shareholdings(db_session, [
        ShareholdingRow("TW", "2330", anchor, 13, "≥600張",
                        500, 100_000_000, 25.0, "finmind"),
        ShareholdingRow("TW", "2330", anchor, 14, "≥800張",
                        300, 80_000_000, 20.0, "finmind"),
        ShareholdingRow("TW", "2330", anchor, 15, "≥1000張",
                        100, 50_000_000, 10.0, "finmind"),
    ])
    out = await read_holdings_concentration_trend(
        db_session, market="TW", symbol="2330",
        weeks=4, as_of=anchor + _td(days=1),
    )
    assert out is None


@pytest.mark.asyncio
async def test_holdings_concentration_trend_classifies_rising(
    db_session: AsyncSession,
):
    """Top-holder shares_percent walks up over 4 weekly snapshots
    → trend `rising` (institutional accumulation). Pin the
    `change_pp` math so a refactor that loses the
    `latest - earliest` direction regresses loudly."""
    from datetime import timedelta as _td

    from services.ingest.repository import (
        ShareholdingRow,
        read_holdings_concentration_trend,
        upsert_shareholdings,
    )

    base = date(2026, 4, 5)
    rows: list[ShareholdingRow] = []
    # Four weekly snapshots, each top-3 totalling: 50, 51.5, 53, 55
    weekly_totals = [50.0, 51.5, 53.0, 55.0]
    for i, total in enumerate(weekly_totals):
        ts = base + _td(weeks=i)
        # Spread across buckets 13/14/15 so the SUM matches `total`.
        rows.extend([
            ShareholdingRow("TW", "2330", ts, 13, "≥600張",
                            500, 100_000_000, total * 0.4, "finmind"),
            ShareholdingRow("TW", "2330", ts, 14, "≥800張",
                            300,  80_000_000, total * 0.35, "finmind"),
            ShareholdingRow("TW", "2330", ts, 15, "≥1000張",
                            100,  50_000_000, total * 0.25, "finmind"),
        ])
    await upsert_shareholdings(db_session, rows)

    out = await read_holdings_concentration_trend(
        db_session, market="TW", symbol="2330",
        weeks=4, as_of=base + _td(weeks=4),
    )
    assert out is not None
    assert out["publication_count"] == 4
    # latest - earliest = 55.0 - 50.0 = +5.0 pp → trend=rising
    assert out["change_pp"] == pytest.approx(5.0, abs=0.01)
    assert out["trend"] == "rising"
    assert out["latest_top_holders_pct"] == pytest.approx(55.0, abs=0.01)


@pytest.mark.asyncio
async def test_holdings_concentration_trend_classifies_falling(
    db_session: AsyncSession,
):
    from datetime import timedelta as _td

    from services.ingest.repository import (
        ShareholdingRow,
        read_holdings_concentration_trend,
        upsert_shareholdings,
    )

    base = date(2026, 4, 5)
    rows: list[ShareholdingRow] = []
    for i, total in enumerate([60.0, 58.0, 55.0]):
        ts = base + _td(weeks=i)
        rows.append(ShareholdingRow(
            "TW", "2330", ts, 15, "≥1000張",
            100, 50_000_000, total, "finmind",
        ))
    await upsert_shareholdings(db_session, rows)

    out = await read_holdings_concentration_trend(
        db_session, market="TW", symbol="2330",
        weeks=4, as_of=base + _td(weeks=3),
    )
    assert out is not None
    assert out["trend"] == "falling"


@pytest.mark.asyncio
async def test_holdings_concentration_trend_classifies_stable(
    db_session: AsyncSession,
):
    """±1 pp band — 0.5 pp drift over 4 weeks is noise, not a
    trend. Pin the boundary."""
    from datetime import timedelta as _td

    from services.ingest.repository import (
        ShareholdingRow,
        read_holdings_concentration_trend,
        upsert_shareholdings,
    )

    base = date(2026, 4, 5)
    rows: list[ShareholdingRow] = []
    for i, total in enumerate([50.0, 50.3, 50.5]):
        ts = base + _td(weeks=i)
        rows.append(ShareholdingRow(
            "TW", "2330", ts, 15, "≥1000張",
            100, 50_000_000, total, "finmind",
        ))
    await upsert_shareholdings(db_session, rows)

    out = await read_holdings_concentration_trend(
        db_session, market="TW", symbol="2330",
        weeks=4, as_of=base + _td(weeks=3),
    )
    assert out is not None
    assert out["trend"] == "stable"


@pytest.mark.asyncio
async def test_holdings_concentration_trend_excludes_lower_buckets(
    db_session: AsyncSession,
):
    """Only buckets 13/14/15 contribute. Lower-bucket rows in the
    same date must be ignored — they're retail-level holders, not
    the 千張大戶 we're tracking."""
    from datetime import timedelta as _td

    from services.ingest.repository import (
        ShareholdingRow,
        read_holdings_concentration_trend,
        upsert_shareholdings,
    )

    base = date(2026, 4, 5)
    rows: list[ShareholdingRow] = []
    for i in range(2):
        ts = base + _td(weeks=i)
        # bucket 1-12 should be ignored (retail/mid-tier holders).
        for bucket in range(1, 13):
            rows.append(ShareholdingRow(
                "TW", "2330", ts, bucket, f"bucket {bucket}",
                10, 1_000_000, 100.0, "finmind",
            ))
        # Top-3 buckets contribute to the metric.
        rows.append(ShareholdingRow(
            "TW", "2330", ts, 15, "≥1000張",
            100, 50_000_000, 30.0 + i * 2, "finmind",
        ))
    await upsert_shareholdings(db_session, rows)

    out = await read_holdings_concentration_trend(
        db_session, market="TW", symbol="2330",
        weeks=4, as_of=base + _td(weeks=2),
    )
    assert out is not None
    # Only the bucket-15 30 / 32 contributes; lower buckets ignored.
    assert out["latest_top_holders_pct"] == pytest.approx(32.0, abs=0.01)
    assert out["earliest_top_holders_pct"] == pytest.approx(30.0, abs=0.01)


@pytest.mark.asyncio
async def test_read_recent_market_institutional_computes_nets(
    db_session: AsyncSession,
):
    from services.ingest.repository import (
        MarketInstitutionalRow,
        read_recent_market_institutional,
        upsert_market_institutional_daily,
    )
    today = date.today()
    await upsert_market_institutional_daily(db_session, [
        MarketInstitutionalRow(
            market="TW", ts=today,
            foreign_buy=100, foreign_sell=80,
            sitc_buy=20, sitc_sell=15,
            dealer_buy=5, dealer_sell=3,
            source="finmind",
        ),
    ])
    out = await read_recent_market_institutional(
        db_session, market="TW", days=5,
    )
    assert len(out) == 1
    r = out[0]
    assert r["foreign_net"] == 20    # 100 - 80
    assert r["sitc_net"] == 5        # 20 - 15
    assert r["dealer_net"] == 2      # 5 - 3
    assert r["total_net"] == 27      # sum of above
