"""4xx → fallback-source routing in ingest_chunk.

Doctrine (spec Track 1): 4xx other than 429 is permanent for that
request shape — retrying FinMind is pointless, but a registered
self-crawl fallback can serve the same dataset. 429/5xx stay on the
existing retry/backoff path.
"""
from datetime import date

import httpx
import pytest
from sqlalchemy import text

import finmind.dataset_catalog as catalog_mod
import finmind.ingest.selfcrawl as selfcrawl_mod
from finmind.ingest import runner as R
from finmind.scripts.init_db import seed_dataset_sources


def _http_error(status: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://api.finmindtrade.com/api/v4/data")
    resp = httpx.Response(status, request=req)
    return httpx.HTTPStatusError(f"{status}", request=req, response=resp)


class _Primary:
    async def fetch(self, dataset_code, symbol, start, end):
        raise _http_error(422)


class _Primary429:
    async def fetch(self, dataset_code, symbol, start, end):
        raise _http_error(429)


class _Fallback:
    def __init__(self):
        self.calls = []

    async def fetch(self, dataset_code, symbol, start, end):
        self.calls.append(dataset_code)
        return [{"stock_id": "2330", "date": "2026-07-24"}]


@pytest.mark.asyncio
async def test_422_routes_to_fallback_and_completes(monkeypatch):
    fb = _Fallback()
    monkeypatch.setattr(R, "_resolve_fallback_client",
                        lambda code, source: fb)
    rows, served_source = await R._fetch_with_fallback(
        _Primary(), dataset_code="TaiwanStockBuyBack", symbol=None,
        range_start=date(2026, 7, 17), range_end=date(2026, 7, 24),
        source="finmind",
    )
    assert fb.calls == ["TaiwanStockBuyBack"]
    assert rows[0]["stock_id"] == "2330"
    # TaiwanStockBuyBack's real catalog fallback is "twse" (Task 1) —
    # `_resolve_fallback_client` is mocked above to bypass the actual
    # client resolution, but `served_source` still comes from the real
    # `fallback_source_for` lookup inside `_fetch_with_fallback`.
    assert served_source == "twse"


@pytest.mark.asyncio
async def test_429_does_not_fall_back(monkeypatch):
    fb = _Fallback()
    monkeypatch.setattr(R, "_resolve_fallback_client",
                        lambda code, source: fb)
    with pytest.raises(httpx.HTTPStatusError):
        await R._fetch_with_fallback(
            _Primary429(), dataset_code="TaiwanStockBuyBack", symbol=None,
            range_start=date(2026, 7, 17), range_end=date(2026, 7, 24),
            source="finmind",
        )
    assert fb.calls == []


@pytest.mark.asyncio
async def test_no_fallback_registered_reraises_original(monkeypatch):
    monkeypatch.setattr(R, "_resolve_fallback_client",
                        lambda code, source: None)
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await R._fetch_with_fallback(
            _Primary(), dataset_code="TaiwanStockBuyBack", symbol=None,
            range_start=date(2026, 7, 17), range_end=date(2026, 7, 24),
            source="finmind",
        )
    assert exc_info.value.response.status_code == 422


@pytest.mark.asyncio
async def test_fallback_failure_reraises_original_error(monkeypatch):
    class _BrokenFallback:
        async def fetch(self, *a, **k):
            raise NotImplementedError("no handler")

    monkeypatch.setattr(R, "_resolve_fallback_client",
                        lambda code, source: _BrokenFallback())
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await R._fetch_with_fallback(
            _Primary(), dataset_code="TaiwanStockBuyBack", symbol=None,
            range_start=date(2026, 7, 17), range_end=date(2026, 7, 24),
            source="finmind",
        )
    assert exc_info.value.response.status_code == 422


# ── `_resolve_fallback_client` — direct coverage ────────────────


def test_resolve_fallback_client_none_when_source_not_finmind():
    """A dataset already running on its fallback (active_source !=
    'finmind') has nowhere further to go."""
    assert R._resolve_fallback_client("TaiwanStockBuyBack", "twse") is None


def test_resolve_fallback_client_none_when_no_fallback_registered(monkeypatch):
    monkeypatch.setattr(catalog_mod, "fallback_source_for", lambda code: None)
    assert R._resolve_fallback_client("SomeDataset", "finmind") is None


def test_resolve_fallback_client_none_when_fallback_equals_source(monkeypatch):
    monkeypatch.setattr(catalog_mod, "fallback_source_for", lambda code: "finmind")
    assert R._resolve_fallback_client("SomeDataset", "finmind") is None


def test_resolve_fallback_client_none_on_keyerror(monkeypatch):
    monkeypatch.setattr(catalog_mod, "fallback_source_for", lambda code: "twse")

    def _raise_key_error(source):
        raise KeyError(f"no SourceClient registered for active_source={source!r}")

    monkeypatch.setattr(selfcrawl_mod, "resolve_client", _raise_key_error)
    assert R._resolve_fallback_client("SomeDataset", "finmind") is None


def test_resolve_fallback_client_happy_path_returns_client(monkeypatch):
    sentinel_client = object()
    monkeypatch.setattr(catalog_mod, "fallback_source_for", lambda code: "twse")
    monkeypatch.setattr(
        selfcrawl_mod, "resolve_client", lambda source: sentinel_client
    )
    assert R._resolve_fallback_client("SomeDataset", "finmind") is sentinel_client


# ── `ingest_chunk` end-to-end through the fallback path ─────────


@pytest.mark.asyncio
async def test_ingest_chunk_persists_fallback_rows_with_true_provenance(
    finmind_session, monkeypatch,
):
    """Integration-level assertion for Finding 1: when a permanent 4xx
    routes TaiwanStockBuyBack to its catalog fallback (twse), the rows
    that land in tw_buyback must carry source='twse', not the
    mapping's unconditional extra={"source": "finmind"}."""
    await seed_dataset_sources()

    class _FallbackBuyBack:
        async def fetch(self, dataset_code, symbol, start, end):
            return [{
                "date": "2026-07-20",
                "stock_id": "2330",
                "BuyBackStartDate": "2026-07-21",
                "BuyBackEndDate": "2026-08-20",
                "BuyBackPlanQuantity": "1000000",
                "BuyBackActualQuantity": "500000",
                "BuyBackAveragePrice": "600.5",
            }]

    monkeypatch.setattr(
        R, "_resolve_fallback_client",
        lambda code, source: _FallbackBuyBack(),
    )

    result = await R.ingest_chunk(
        finmind_session,
        dataset_code="TaiwanStockBuyBack",
        symbol="2330",
        range_start=date(2026, 7, 17),
        range_end=date(2026, 7, 24),
        client=_Primary(),
    )

    assert result.status == "done"
    assert result.rows_written == 1

    rows = (
        await finmind_session.execute(
            text(
                "SELECT symbol, source FROM tw_buyback "
                "WHERE symbol = '2330'"
            )
        )
    ).all()
    assert len(rows) == 1
    assert rows[0][1] == "twse"
