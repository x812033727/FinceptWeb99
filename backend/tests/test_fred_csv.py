"""Unit tests for the keyless fredgraph CSV path in
data.us.fred_connector.get_series_csv (mocked HTTP)."""
from unittest.mock import patch

import pytest

import data.us.fred_connector as fred

_CSV = (
    "observation_date,DCOILWTICO\n"
    "2026-01-02,57.21\n"
    "2026-01-05,58.10\n"
    "2026-01-06,.\n"        # FRED missing-observation marker → skipped
    "2026-01-07,\n"         # empty → skipped
    "2026-01-08,56.01\n"
)


class _Resp:
    text = _CSV
    def raise_for_status(self):
        return None


class _Client:
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return None
    async def get(self, *a, **k):
        return _Resp()


@pytest.mark.asyncio
async def test_get_series_csv_parses_and_skips_missing():
    with patch.object(fred.httpx, "AsyncClient", lambda **k: _Client()):
        rows = await fred.get_series_csv("DCOILWTICO", "2026-01-01", "2026-01-08")
    assert rows == [
        {"date": "2026-01-02", "value": 57.21},
        {"date": "2026-01-05", "value": 58.10},
        {"date": "2026-01-08", "value": 56.01},
    ]


@pytest.mark.asyncio
async def test_get_series_csv_empty_body_returns_empty():
    class _EmptyResp:
        text = "observation_date,X\n"
        def raise_for_status(self):
            return None

    class _EmptyClient(_Client):
        async def get(self, *a, **k):
            return _EmptyResp()

    with patch.object(fred.httpx, "AsyncClient", lambda **k: _EmptyClient()):
        assert await fred.get_series_csv("X") == []


@pytest.mark.asyncio
async def test_get_series_csv_http_error_fails_soft():
    class _BoomClient(_Client):
        async def get(self, *a, **k):
            raise RuntimeError("network down")

    with patch.object(fred.httpx, "AsyncClient", lambda **k: _BoomClient()):
        assert await fred.get_series_csv("X") == []
