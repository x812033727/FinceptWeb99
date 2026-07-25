"""4xx → fallback-source routing in ingest_chunk.

Doctrine (spec Track 1): 4xx other than 429 is permanent for that
request shape — retrying FinMind is pointless, but a registered
self-crawl fallback can serve the same dataset. 429/5xx stay on the
existing retry/backoff path.
"""
from datetime import date

import httpx
import pytest

from finmind.ingest import runner as R


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
    result = await R._fetch_with_fallback(
        _Primary(), dataset_code="TaiwanStockBuyBack", symbol=None,
        range_start=date(2026, 7, 17), range_end=date(2026, 7, 24),
        source="finmind",
    )
    assert fb.calls == ["TaiwanStockBuyBack"]
    assert result[0]["stock_id"] == "2330"


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
