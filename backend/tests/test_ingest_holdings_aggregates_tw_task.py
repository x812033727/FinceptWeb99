"""Tests for `tasks.ingest_holdings_aggregates_tw` — combined cron
for股權分散 + 全市場三大法人 datasets.

Pinned scenarios:
  - Shareholding rows fan out into long-form bucket entries
  - Total-institutional folds per-investor-type rows into one date row
  - Per-substep paywall isolation (mirrors PR #192's pattern)
  - Repository read helpers compute the expected aggregates
"""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.tw_holdings_aggregates import (
    TwMarketInstitutionalDaily,
    TwStockShareholding,
)


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

    today = date.today().isoformat()
    shareholding_payload = [
        {"stock_id": "2330", "date": today, "level": 1,
         "label": "1-999股", "holders": 100_000, "shares": 5_000_000,
         "percent": 0.5},
        {"stock_id": "2330", "date": today, "level": 2,
         "label": "1000-5000股", "holders": 50_000, "shares": 30_000_000,
         "percent": 3.0},
        {"stock_id": "2330", "date": today, "level": 15,
         "label": "1000張+", "holders": 50, "shares": 800_000_000,
         "percent": 80.0},
    ]
    with patch("tasks.ingest_holdings_aggregates_tw.acquire_lock",
               AsyncMock(return_value=True)), \
         patch("tasks.ingest_holdings_aggregates_tw.release_lock", AsyncMock()), \
         patch("tasks.ingest_holdings_aggregates_tw.backoff_remaining_seconds",
               AsyncMock(return_value=0)), \
         patch("tasks.ingest_holdings_aggregates_tw.clear_failures", AsyncMock()), \
         patch("tasks.ingest_holdings_aggregates_tw.record_health", AsyncMock()), \
         patch("tasks.ingest_holdings_aggregates_tw.finmind.get_shareholding_market_wide",
               AsyncMock(return_value=shareholding_payload)), \
         patch("tasks.ingest_holdings_aggregates_tw.finmind.get_total_institutional_market_wide",
               AsyncMock(return_value=[])):
        await ingest_holdings_aggregates_tw.run()

    rows = (await db_session.scalars(
        select(TwStockShareholding).where(TwStockShareholding.symbol == "2330")
    )).all()
    assert len(rows) == 3
    by_bucket = {r.bucket_id: r for r in rows}
    assert by_bucket[1].bucket_label == "1-999股"
    assert int(by_bucket[1].holders_count) == 100_000
    assert float(by_bucket[15].shares_percent) == 80.0


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
