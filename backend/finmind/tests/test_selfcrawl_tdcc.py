"""W3 TDCC self-crawl — client dispatch, market-wide dispatcher routing,
and an end-to-end 股權分散 ingest with a fake connector."""
from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from finmind.ingest.runner import ingest_chunk
from finmind.ingest.selfcrawl import covers_dataset, resolve_client
from finmind.ingest.selfcrawl.tdcc import TdccClient
from finmind.models.chip import TwHoldingsAggregates
from finmind.models.dataset_source import DatasetSource
from finmind.scheduler.dispatcher import expand_due_datasets
from finmind.scripts.init_db import seed_dataset_sources


def test_tdcc_registered_and_covers_holding():
    assert isinstance(resolve_client("tdcc"), TdccClient)
    assert covers_dataset("tdcc", "TaiwanStockHoldingSharesPer")
    assert not covers_dataset("tdcc", "TaiwanStockPrice")


@pytest.mark.asyncio
async def test_tdcc_client_dispatches_and_rejects_unknown():
    fake = [{"date": "2026-07-03", "stock_id": "2330"}]
    with patch(
        "finmind.ingest.selfcrawl.tdcc._tdcc.get_holding_shares_per",
        new=AsyncMock(return_value=fake),
    ):
        out = await TdccClient().fetch(
            "TaiwanStockHoldingSharesPer", "2330",
            date(2026, 7, 1), date(2026, 7, 3),
        )
    assert out == fake
    with pytest.raises(NotImplementedError):
        await TdccClient().fetch("TaiwanStockPrice", None,
                                 date(2026, 7, 1), date(2026, 7, 3))


def _mk_ds(**kw) -> DatasetSource:
    d = dict(
        dataset_code="TaiwanStockHoldingSharesPer", category="chip",
        local_table="tw_holdings_aggregates", per_symbol=True,
        primary_source="finmind", fallback_source="tdcc",
        active_source="tdcc", ingest_freq="weekly",
        enabled=True, last_ingest_at=None, single_day=False,
    )
    d.update(kw)
    return DatasetSource(**d)


def test_dispatcher_tdcc_is_market_wide_despite_per_symbol():
    now = datetime(2026, 7, 12, tzinfo=timezone.utc)
    chunks = expand_due_datasets([_mk_ds()], now, symbols=["2330", "2317"])
    assert len(chunks) == 1
    assert chunks[0].symbol is None


@pytest.mark.asyncio
async def test_tdcc_holding_ingest_end_to_end(finmind_session):
    await seed_dataset_sources()
    ds = await finmind_session.get(DatasetSource, "TaiwanStockHoldingSharesPer")
    ds.enabled = True
    ds.active_source = "tdcc"
    await finmind_session.commit()

    fake_rows = [
        {"date": "2026-07-03", "stock_id": "2330", "HoldingSharesLevel": "1",
         "people": "12345", "unit": "6789000", "percent": "0.26"},
        {"date": "2026-07-03", "stock_id": "2330", "HoldingSharesLevel": "15",
         "people": "42", "unit": "9000000000", "percent": "34.71"},
    ]

    class _FakeTdcc:
        async def fetch(self, dataset_code, symbol, start_date, end_date):
            return fake_rows

    result = await ingest_chunk(
        finmind_session, dataset_code="TaiwanStockHoldingSharesPer",
        symbol=None, range_start=date(2026, 7, 1), range_end=date(2026, 7, 3),
        client=_FakeTdcc(),
    )
    assert result.status == "done", result
    assert result.rows_written == 2

    rows = (
        await finmind_session.execute(
            select(TwHoldingsAggregates).order_by(TwHoldingsAggregates.bracket)
        )
    ).scalars().all()
    by = {r.bracket: r for r in rows}
    assert set(by) == {"1", "15"}
    assert by["1"].holders == 12345
    assert by["15"].shares == 9000000000
    assert float(by["15"].pct) == 34.71
    assert by["1"].ts == date(2026, 7, 3)
