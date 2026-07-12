"""W4 FRED macro self-crawl — client dispatch, market-wide dispatcher
routing, and end-to-end ingest into the migration-only macro tables.

us_bond_yield / commodity_price have no ORM model (they live only in
migration 0021 and the runner upserts via raw SQL), so the end-to-end
tests create them explicitly before ingesting.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from finmind.ingest.runner import ingest_chunk
from finmind.ingest.selfcrawl import covers_dataset, resolve_client
from finmind.ingest.selfcrawl.fred import FredClient
from finmind.models.dataset_source import DatasetSource
from finmind.scheduler.dispatcher import expand_due_datasets
from finmind.scripts.init_db import seed_dataset_sources


def test_fred_registered_and_covers_macro():
    assert isinstance(resolve_client("fred"), FredClient)
    assert covers_dataset("fred", "GovernmentBondsYield")
    assert covers_dataset("fred", "CrudeOilPrices")
    assert not covers_dataset("fred", "CnnFearGreedIndex")  # deferred to W4b


@pytest.mark.asyncio
async def test_fred_client_builds_bond_and_oil_rows():
    async def fake_csv(series_id, start, end):
        return {"DGS10": [{"date": "2026-01-02", "value": 4.19}],
                "DCOILWTICO": [{"date": "2026-01-02", "value": 57.21}]}.get(series_id, [])

    with patch("finmind.ingest.selfcrawl.fred._fred.get_series_csv",
               new=AsyncMock(side_effect=fake_csv)):
        bonds = await FredClient().fetch(
            "GovernmentBondsYield", None, date(2026, 1, 2), date(2026, 1, 2))
        oil = await FredClient().fetch(
            "CrudeOilPrices", None, date(2026, 1, 2), date(2026, 1, 2))

    assert {"date": "2026-01-02", "name": "United States 10-Year", "value": 4.19} in bonds
    assert {"date": "2026-01-02", "name": "WTI", "price": 57.21} in oil


def _mk_ds(**kw) -> DatasetSource:
    d = dict(
        dataset_code="GovernmentBondsYield", category="macro",
        local_table="us_bond_yield", per_symbol=True,
        primary_source="finmind", fallback_source="fred",
        active_source="fred", ingest_freq="daily",
        enabled=True, last_ingest_at=None, single_day=False,
    )
    d.update(kw)
    return DatasetSource(**d)


def test_dispatcher_fred_bonds_is_market_wide_despite_per_symbol():
    now = datetime(2026, 7, 12, tzinfo=timezone.utc)
    chunks = expand_due_datasets([_mk_ds()], now, symbols=["2330", "2317"])
    assert len(chunks) == 1
    assert chunks[0].symbol is None


_CREATE_BOND = text(
    "CREATE TABLE us_bond_yield (tenor VARCHAR(32) NOT NULL, ts DATE NOT NULL, "
    "yield_pct NUMERIC(8,4), source VARCHAR(16) NOT NULL, "
    "ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
    "PRIMARY KEY (tenor, ts))"
)


@pytest.mark.asyncio
async def test_fred_bonds_ingest_end_to_end(finmind_session):
    await finmind_session.execute(_CREATE_BOND)
    await seed_dataset_sources()
    ds = await finmind_session.get(DatasetSource, "GovernmentBondsYield")
    ds.enabled = True
    ds.active_source = "fred"
    await finmind_session.commit()

    fake_rows = [
        {"date": "2026-01-02", "name": "United States 10-Year", "value": 4.19},
        {"date": "2026-01-02", "name": "United States 2-Year", "value": 3.55},
    ]

    class _FakeFred:
        async def fetch(self, dataset_code, symbol, start_date, end_date):
            return fake_rows

    result = await ingest_chunk(
        finmind_session, dataset_code="GovernmentBondsYield", symbol=None,
        range_start=date(2026, 1, 2), range_end=date(2026, 1, 2),
        client=_FakeFred(),
    )
    assert result.status == "done", result
    assert result.rows_written == 2

    rows = (await finmind_session.execute(
        text("SELECT tenor, yield_pct, source FROM us_bond_yield ORDER BY tenor")
    )).all()
    by = {r[0]: r for r in rows}
    assert "United States 10-Year" in by
    assert float(by["United States 10-Year"][1]) == 4.19
    assert by["United States 10-Year"][2] == "finmind"  # extra source default
