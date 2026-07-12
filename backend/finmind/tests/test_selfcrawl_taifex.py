"""W2 TAIFEX self-crawl — client dispatch, market-wide dispatcher
routing, and an end-to-end futures ingest with a fake connector."""
from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from finmind.ingest.runner import ingest_chunk
from finmind.ingest.selfcrawl import covers_dataset, resolve_client
from finmind.ingest.selfcrawl.taifex import TaifexClient
from finmind.models.dataset_source import DatasetSource
from finmind.models.derivative import TwFuturesDaily
from finmind.scheduler.dispatcher import expand_due_datasets
from finmind.scripts.init_db import seed_dataset_sources


def test_taifex_registered_and_covers_daily():
    assert isinstance(resolve_client("taifex"), TaifexClient)
    assert covers_dataset("taifex", "TaiwanFuturesDaily")
    assert covers_dataset("taifex", "TaiwanOptionDaily")
    assert covers_dataset("taifex", "TaiwanFuturesInstitutionalInvestors")
    # Option institutional still deferred (batch-pivot).
    assert not covers_dataset("taifex", "TaiwanOptionInstitutionalInvestors")


@pytest.mark.asyncio
async def test_taifex_client_dispatches_to_connector():
    client = TaifexClient()
    fake = [{"date": "2026-01-05", "futures_id": "TX", "close": "30313"}]
    with patch(
        "finmind.ingest.selfcrawl.taifex._taifex.get_futures_daily",
        new=AsyncMock(return_value=fake),
    ):
        out = await client.fetch(
            "TaiwanFuturesDaily", None, date(2026, 1, 5), date(2026, 1, 5),
        )
    assert out == fake


@pytest.mark.asyncio
async def test_taifex_client_unsupported_dataset_raises():
    with pytest.raises(NotImplementedError):
        await TaifexClient().fetch(
            "TaiwanOptionInstitutionalInvestors", None,
            date(2026, 1, 1), date(2026, 1, 1),
        )


def _mk_ds(**kw) -> DatasetSource:
    d = dict(
        dataset_code="TaiwanFuturesDaily", category="derivative",
        local_table="tw_futures_daily", per_symbol=True,
        primary_source="finmind", fallback_source="taifex",
        active_source="taifex", ingest_freq="daily",
        enabled=True, last_ingest_at=None, single_day=False,
    )
    d.update(kw)
    return DatasetSource(**d)


def test_dispatcher_taifex_is_market_wide_despite_per_symbol():
    """A per_symbol dataset on active_source='taifex' emits ONE chunk
    with symbol=None (market-wide download), never a per-equity fan-out."""
    now = datetime(2026, 7, 12, tzinfo=timezone.utc)
    chunks = expand_due_datasets(
        [_mk_ds()], now, symbols=["2330", "2317", "2454"],
    )
    assert len(chunks) == 1
    assert chunks[0].symbol is None


def test_dispatcher_finmind_source_still_fans_out_per_symbol():
    """Guard: the market-wide route keys on active_source, so while the
    dataset is still on finmind it keeps the per-equity fan-out."""
    now = datetime(2026, 7, 12, tzinfo=timezone.utc)
    chunks = expand_due_datasets(
        [_mk_ds(active_source="finmind")], now, symbols=["2330", "2317"],
    )
    assert sorted(c.symbol for c in chunks) == ["2317", "2330"]


@pytest.mark.asyncio
async def test_taifex_futures_ingest_end_to_end(finmind_session):
    """Flip TaiwanFuturesDaily to active_source='taifex' and ingest a
    chunk through the real mapping into tw_futures_daily."""
    await seed_dataset_sources()
    ds = await finmind_session.get(DatasetSource, "TaiwanFuturesDaily")
    ds.enabled = True
    ds.active_source = "taifex"
    await finmind_session.commit()

    fake_rows = [
        {"date": "2026-01-05", "futures_id": "TX", "open": "29888",
         "max": "30487", "min": "29850", "close": "30313", "volume": "95320",
         "open_interest": "75674", "settlement_price": "30309"},
        {"date": "2026-01-05", "futures_id": "MTX", "open": "29890",
         "max": "30489", "min": "29852", "close": "30315", "volume": "120000",
         "open_interest": "50000", "settlement_price": "30310"},
    ]

    class _FakeTaifex:
        async def fetch(self, dataset_code, symbol, start_date, end_date):
            return fake_rows

    result = await ingest_chunk(
        finmind_session, dataset_code="TaiwanFuturesDaily", symbol=None,
        range_start=date(2026, 1, 5), range_end=date(2026, 1, 5),
        client=_FakeTaifex(),
    )
    assert result.status == "done", result
    assert result.rows_written == 2

    rows = (
        await finmind_session.execute(
            select(TwFuturesDaily).order_by(TwFuturesDaily.contract)
        )
    ).scalars().all()
    by = {r.contract: r for r in rows}
    assert set(by) == {"MTX", "TX"}
    assert float(by["TX"].close) == 30313
    assert by["TX"].open_interest == 75674
    assert by["TX"].ts == date(2026, 1, 5)


@pytest.mark.asyncio
async def test_taifex_futures_institutional_ingest_end_to_end(finmind_session):
    """三大法人 futures OI → one wide row per contract in
    tw_futures_inst_daily (day session)."""
    from finmind.models.derivative import TwFuturesInstDaily

    await seed_dataset_sources()
    ds = await finmind_session.get(
        DatasetSource, "TaiwanFuturesInstitutionalInvestors")
    ds.enabled = True
    ds.active_source = "taifex"
    await finmind_session.commit()

    fake_rows = [{
        "date": "2026-01-05", "futures_id": "TX",
        "long_open_interest_balance_volume_foreign_investment": "23747",
        "short_open_interest_balance_volume_foreign_investment": "48848",
        "long_open_interest_balance_volume_investment_trust": "32819",
        "short_open_interest_balance_volume_investment_trust": "5326",
        "long_open_interest_balance_volume_dealer": "4134",
        "short_open_interest_balance_volume_dealer": "8614",
    }]

    class _FakeTaifex:
        async def fetch(self, dataset_code, symbol, start_date, end_date):
            return fake_rows

    result = await ingest_chunk(
        finmind_session,
        dataset_code="TaiwanFuturesInstitutionalInvestors", symbol=None,
        range_start=date(2026, 1, 5), range_end=date(2026, 1, 5),
        client=_FakeTaifex(),
    )
    assert result.status == "done", result
    assert result.rows_written == 1

    row = (await finmind_session.execute(
        select(TwFuturesInstDaily))).scalars().one()
    assert row.contract == "TX"
    assert row.session == "day"
    assert row.foreign_long_open_interest == 23747
    assert row.foreign_short_open_interest == 48848
    assert row.dealer_short_open_interest == 8614
