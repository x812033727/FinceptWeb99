"""Crypto ingest pipeline tests — Binance OHLCV end-to-end plus the
universe routing / refresh logic.

The end-to-end test drives the real ingest path (dataset_sources →
find_mapping → transform_row → UPSERT into crypto_ohlcv) with a fake
Binance client so it stays deterministic and offline. Connector
row-shaping is unit-tested separately against a mocked HTTP layer.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select, text

from finmind.ingest.runner import ingest_chunk
from finmind.models.crypto import CryptoOhlcv, CryptoUniverse
from finmind.models.dataset_source import DatasetSource
from finmind.scheduler.dispatcher import DueChunk, expand_due_datasets
from finmind.scheduler.runner import get_crypto_universe
from finmind.scripts.crypto_universe_refresh import apply_refresh, build_rows
from finmind.scripts.init_db import seed_dataset_sources


# Raw rows in the shape `binance_connector.fetch_ohlcv` emits.
def _canned_klines(symbol: str, interval: str) -> list[dict]:
    return [
        {
            "symbol": symbol, "interval": interval,
            "ts": "2026-01-01T00:00:00+00:00",
            "open": "87648.21", "high": "88919.45", "low": "87550.43",
            "close": "88839.04", "volume": "6279.57", "quote_volume": "5.5e8",
            "trades": 1449281,
        },
        {
            "symbol": symbol, "interval": interval,
            "ts": "2026-01-02T00:00:00+00:00",
            "open": "88839.04", "high": "90000.00", "low": "88000.00",
            "close": "89500.00", "volume": "5000.0", "quote_volume": "4.4e8",
            "trades": 1200000,
        },
    ]


class _FakeBinanceClient:
    def __init__(self, interval: str):
        self._interval = interval

    async def fetch(self, dataset_code, symbol, start_date, end_date):
        return _canned_klines(symbol, self._interval)


@pytest.mark.asyncio
async def test_crypto_price_ingest_end_to_end(finmind_session):
    """CryptoPrice chunk → rows land in crypto_ohlcv with market/interval
    stamped and typed values, driving the real mapping + UPSERT path."""
    await seed_dataset_sources()
    ds = await finmind_session.get(DatasetSource, "CryptoPrice")
    assert ds is not None
    assert ds.active_source == "binance"  # born self-sourced
    assert ds.local_table == "crypto_ohlcv"
    ds.enabled = True
    await finmind_session.commit()

    result = await ingest_chunk(
        finmind_session,
        dataset_code="CryptoPrice",
        symbol="BTCUSDT",
        range_start=date(2026, 1, 1),
        range_end=date(2026, 1, 2),
        client=_FakeBinanceClient("1d"),
    )
    assert result.status == "done", result
    assert result.rows_written == 2

    rows = (
        await finmind_session.execute(
            select(CryptoOhlcv).order_by(CryptoOhlcv.ts)
        )
    ).scalars().all()
    assert len(rows) == 2
    r0 = rows[0]
    assert r0.market == "BINANCE"
    assert r0.symbol == "BTCUSDT"
    assert r0.interval == "1d"
    assert str(r0.close) == "88839.04000000"
    assert r0.source == "binance"


@pytest.mark.asyncio
async def test_crypto_price_ingest_is_idempotent(finmind_session):
    """Re-running the same chunk UPSERTs, not duplicates (PK includes
    interval so daily + hourly can coexist, but a repeat daily pull for
    the same ts overwrites)."""
    await seed_dataset_sources()
    ds = await finmind_session.get(DatasetSource, "CryptoPrice")
    ds.enabled = True
    await finmind_session.commit()

    for _ in range(2):
        await ingest_chunk(
            finmind_session, dataset_code="CryptoPrice", symbol="BTCUSDT",
            range_start=date(2026, 1, 1), range_end=date(2026, 1, 2),
            client=_FakeBinanceClient("1d"),
        )
    count = (
        await finmind_session.execute(
            text("SELECT count(*) FROM crypto_ohlcv")
        )
    ).scalar()
    assert count == 2  # not 4


# ── Universe routing ─────────────────────────────────────────────


def _mk_ds(**kw) -> DatasetSource:
    defaults = dict(
        dataset_code="CryptoPrice", category="crypto",
        local_table="crypto_ohlcv", per_symbol=True,
        primary_source="binance", fallback_source=None,
        active_source="binance", ingest_freq="daily",
        enabled=True, last_ingest_at=None, single_day=False,
    )
    defaults.update(kw)
    return DatasetSource(**defaults)


def test_dispatcher_routes_crypto_over_crypto_universe():
    """A binance-sourced per-symbol dataset fans out over crypto_symbols,
    never the TW equity universe."""
    now = datetime(2026, 7, 12, tzinfo=timezone.utc)
    ds = _mk_ds()
    chunks = expand_due_datasets(
        [ds], now,
        symbols=["2330", "2317"],          # equity — must be ignored
        crypto_symbols=["BTCUSDT", "ETHUSDT"],
    )
    syms = sorted(c.symbol for c in chunks)
    assert syms == ["BTCUSDT", "ETHUSDT"]


def test_dispatcher_crypto_without_universe_emits_symbol_none():
    now = datetime(2026, 7, 12, tzinfo=timezone.utc)
    chunks = expand_due_datasets([_mk_ds()], now, symbols=["2330"])
    # No crypto_symbols supplied → single symbol=None chunk (runner will
    # record it failed rather than fanning over the equity universe).
    assert len(chunks) == 1
    assert chunks[0].symbol is None


def test_dispatcher_hourly_is_due_on_null_last_ingest():
    now = datetime(2026, 7, 12, tzinfo=timezone.utc)
    ds = _mk_ds(dataset_code="CryptoPriceHourly", ingest_freq="hourly")
    chunks = expand_due_datasets([ds], now, crypto_symbols=["BTCUSDT"])
    assert [c.symbol for c in chunks] == ["BTCUSDT"]


@pytest.mark.asyncio
async def test_get_crypto_universe_returns_active_mapped_only(finmind_session):
    finmind_session.add_all([
        CryptoUniverse(
            coingecko_id="bitcoin", symbol="BTC", binance_symbol="BTCUSDT",
            exchange="binance", status="active", source="coingecko",
        ),
        CryptoUniverse(
            coingecko_id="tether", symbol="USDT", binance_symbol=None,
            exchange="binance", status="unmapped", source="coingecko",
        ),
        CryptoUniverse(
            coingecko_id="dogecoin", symbol="DOGE", binance_symbol="DOGEUSDT",
            exchange="binance", status="delisted", source="coingecko",
        ),
    ])
    await finmind_session.commit()

    universe = await get_crypto_universe(finmind_session)
    assert universe == ["BTCUSDT"]  # active + mapped only


# ── Universe refresh (build_rows + apply_refresh) ────────────────


def test_build_rows_maps_and_skips_stablecoins():
    markets = [
        {"coingecko_id": "bitcoin", "symbol": "BTC", "name": "Bitcoin",
         "market_cap_rank": 1, "market_cap": 1e12, "circulating_supply": 2e7,
         "total_supply": 2.1e7, "ath": 100000},
        {"coingecko_id": "tether", "symbol": "USDT", "name": "Tether",
         "market_cap_rank": 3, "market_cap": 1e11},
        {"coingecko_id": "someshit", "symbol": "XYZ", "name": "XYZ",
         "market_cap_rank": 150},  # no Binance pair
    ]
    spot = {"BTCUSDT", "USDTUSDT"}  # even if USDT pair existed, it's skipped
    uni, info = build_rows(markets, spot, date(2026, 7, 12))

    by_id = {r["coingecko_id"]: r for r in uni}
    assert by_id["bitcoin"]["status"] == "active"
    assert by_id["bitcoin"]["binance_symbol"] == "BTCUSDT"
    assert by_id["tether"]["status"] == "unmapped"   # stablecoin skip
    assert by_id["tether"]["binance_symbol"] is None
    assert by_id["someshit"]["status"] == "unmapped"  # no pair
    assert len(info) == 3
    assert info[0]["snapshot_date"] == "2026-07-12"


@pytest.mark.asyncio
async def test_apply_refresh_upserts_and_delists(finmind_session):
    # Seed an existing active coin that will fall out of the new top-N.
    finmind_session.add(CryptoUniverse(
        coingecko_id="oldcoin", symbol="OLD", binance_symbol="OLDUSDT",
        exchange="binance", status="active", source="coingecko",
    ))
    await finmind_session.commit()

    markets = [{"coingecko_id": "bitcoin", "symbol": "BTC", "name": "Bitcoin",
                "market_cap_rank": 1, "market_cap": 1e12}]
    uni, info = build_rows(markets, {"BTCUSDT"}, date(2026, 7, 12))
    summary = await apply_refresh(finmind_session, uni, info, date(2026, 7, 12))

    assert summary["active"] == 1
    assert summary["delisted"] == 1  # oldcoin dropped out

    old = await finmind_session.get(CryptoUniverse, "oldcoin")
    assert old.status == "delisted"
    assert old.removed_at == date(2026, 7, 12)
    btc = await finmind_session.get(CryptoUniverse, "bitcoin")
    assert btc.status == "active"
