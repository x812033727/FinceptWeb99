"""
Unit tests for data.us.fred_connector.

FRED has two surface methods (get_series / get_latest) and one well-
known quirk: missing observations come back as the literal string "."
which the connector translates to None. The latest-value lookup also
needs to skip those Nones to find the most recent real reading.

httpx.AsyncClient is mocked at the module's import-site so no
network. The active FRED key is resolved per-test by patching
`services.market_key_service.resolve_key` (PR #149 moved key
resolution off `settings.FRED_API_KEY` into the DB-managed
`market_provider_keys` table).
"""
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import data.us.fred_connector as fred


# ── Test doubles ──────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=None
            )


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def get(self, url, params=None):
        self.calls.append((url, params or {}))
        return self.response


def install_client(response):
    fake = FakeClient(response)
    return patch.object(fred.httpx, "AsyncClient", lambda **_: fake), fake


def with_api_key(value: str = "test-key"):
    """Patch the resolved FRED key for tests that exercise the
    "key configured" branch.

    `get_series` does `from services.market_key_service import resolve_key`
    inline, so patch that source attribute (it's the resolution call site
    every invocation re-imports against)."""
    return patch(
        "services.market_key_service.resolve_key",
        new=AsyncMock(return_value=value),
    )


def with_no_api_key():
    """Same shape as `with_api_key` but resolves to empty string —
    matches the "no key configured" yfinance-fallback path."""
    return patch(
        "services.market_key_service.resolve_key",
        new=AsyncMock(return_value=""),
    )


# ── SERIES map (frozen contract) ──────────────────────────────────

def test_series_map_exposes_core_macro_indicators():
    """Frontend / service layer reference these keys — pin them down
    so a rename doesn't silently break MacroPage."""
    assert fred.SERIES["fed_funds_rate"] == "FEDFUNDS"
    assert fred.SERIES["cpi"] == "CPIAUCSL"
    assert fred.SERIES["10y_yield"] == "DGS10"
    assert fred.SERIES["10y_minus_2y"] == "T10Y2Y"
    assert fred.SERIES["twd_usd"] == "DEXTW"


# ── get_series: API-key gate ──────────────────────────────────────

@pytest.mark.asyncio
async def test_get_series_returns_empty_when_api_key_not_configured_and_no_fallback():
    """No key + no yfinance fallback (CPI is an economic release, not a
    market quote) → connector returns []."""
    with with_no_api_key():
        out = await fred.get_series("CPIAUCSL")
    assert out == []


@pytest.mark.asyncio
async def test_get_series_returns_empty_when_api_key_is_none_and_no_fallback():
    with patch(
        "services.market_key_service.resolve_key",
        new=AsyncMock(return_value=None),
    ):
        out = await fred.get_series("UNRATE")
    assert out == []


# ── get_series: yfinance fallback when no FRED key ───────────────

@pytest.mark.asyncio
async def test_get_series_falls_back_to_yfinance_when_no_key_and_series_is_tradable():
    """Free-tier deployments without a FRED key still see live values
    for the four tradable series (10Y yield, short rate, dollar index,
    TWD/USD) by reading from yfinance."""
    fake_bars = [
        # yfinance shape: time = unix ms, close = float
        {"time": 1_704_067_200_000, "open": 4.20, "high": 4.30, "low": 4.10, "close": 4.25, "volume": 0},
        {"time": 1_706_745_600_000, "open": 4.25, "high": 4.35, "low": 4.20, "close": 4.32, "volume": 0},
    ]
    with with_no_api_key(), \
         patch.object(fred.yfinance, "get_history", new=lambda *a, **k: _coro(fake_bars)) as _:
        out = await fred.get_series("DGS10")

    assert len(out) == 2
    assert out[0]["date"] == "2024-01-01"
    assert out[0]["value"] == 4.25
    assert out[1]["value"] == 4.32


@pytest.mark.asyncio
async def test_get_series_no_fallback_for_economic_releases():
    """CPI, Unemployment, GDP, T10Y2Y, DGS2 have no Yahoo equivalent
    so they correctly return [] when FRED is missing."""
    for series in ("CPIAUCSL", "UNRATE", "GDP", "T10Y2Y", "DGS2"):
        with with_no_api_key():
            out = await fred.get_series(series)
        assert out == [], f"{series} should not have a fallback"


@pytest.mark.asyncio
async def test_get_series_yfinance_fallback_skips_rows_with_null_close():
    fake_bars = [
        {"time": 1_704_067_200_000, "open": None, "high": None, "low": None, "close": None, "volume": 0},
        {"time": 1_706_745_600_000, "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "volume": 0},
    ]
    with with_no_api_key(), \
         patch.object(fred.yfinance, "get_history", new=lambda *a, **k: _coro(fake_bars)):
        out = await fred.get_series("DTWEXBGS")

    assert len(out) == 1
    assert out[0]["value"] == 101.0


@pytest.mark.asyncio
async def test_get_series_yfinance_fallback_swallows_errors():
    """Yahoo blocked / DNS failure during fallback ⇒ [] (no exception
    bubbles to the request handler)."""
    async def _raise(*_a, **_kw):
        raise RuntimeError("yahoo blocked")

    with with_no_api_key(), \
         patch.object(fred.yfinance, "get_history", new=_raise):
        out = await fred.get_series("DEXTW")
    assert out == []


async def _coro(value):
    """Tiny helper: turn a sync value into an awaitable for AsyncMock-free patching."""
    return value


# ── get_series: parsing ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_series_parses_observations_to_typed_rows():
    payload = {"observations": [
        {"date": "2024-01-02", "value": "5.33"},
        {"date": "2024-02-01", "value": "5.31"},
    ]}
    patcher, _ = install_client(FakeResponse(payload))
    with patcher, with_api_key():
        rows = await fred.get_series("FEDFUNDS")

    assert rows == [
        {"date": "2024-01-02", "value": 5.33},
        {"date": "2024-02-01", "value": 5.31},
    ]


@pytest.mark.asyncio
async def test_get_series_translates_dot_to_none_for_missing_observations():
    """FRED returns "." for non-trading-day / not-yet-published values.
    Connector turns those into None so the chart layer can render
    gaps instead of choking on a non-numeric string."""
    payload = {"observations": [
        {"date": "2024-01-01", "value": "."},
        {"date": "2024-01-02", "value": "5.33"},
        {"date": "2024-01-03", "value": "."},
    ]}
    patcher, _ = install_client(FakeResponse(payload))
    with patcher, with_api_key():
        rows = await fred.get_series("FEDFUNDS")

    assert rows[0]["value"] is None
    assert rows[1]["value"] == 5.33
    assert rows[2]["value"] is None


@pytest.mark.asyncio
async def test_get_series_returns_empty_when_observations_field_missing():
    patcher, _ = install_client(FakeResponse({"error_code": 400}))
    with patcher, with_api_key():
        rows = await fred.get_series("FEDFUNDS")
    assert rows == []


# ── get_series: parameter wiring ──────────────────────────────────

@pytest.mark.asyncio
async def test_get_series_passes_required_params_to_fred():
    patcher, fake = install_client(FakeResponse({"observations": []}))
    with patcher, with_api_key("my-secret-key"):
        await fred.get_series("FEDFUNDS")

    _, params = fake.calls[0]
    assert params["series_id"] == "FEDFUNDS"
    assert params["api_key"] == "my-secret-key"
    assert params["file_type"] == "json"
    assert params["sort_order"] == "asc"
    assert params["limit"] == 1000


@pytest.mark.asyncio
async def test_get_series_includes_date_range_when_provided():
    patcher, fake = install_client(FakeResponse({"observations": []}))
    with patcher, with_api_key():
        await fred.get_series("FEDFUNDS", "2020-01-01", "2024-12-31")

    _, params = fake.calls[0]
    assert params["observation_start"] == "2020-01-01"
    assert params["observation_end"] == "2024-12-31"


@pytest.mark.asyncio
async def test_get_series_omits_date_params_when_not_provided():
    patcher, fake = install_client(FakeResponse({"observations": []}))
    with patcher, with_api_key():
        await fred.get_series("FEDFUNDS")

    _, params = fake.calls[0]
    assert "observation_start" not in params
    assert "observation_end" not in params


@pytest.mark.asyncio
async def test_get_series_propagates_http_errors():
    patcher, _ = install_client(FakeResponse({}, status_code=429))
    with patcher, with_api_key(), pytest.raises(httpx.HTTPStatusError):
        await fred.get_series("FEDFUNDS")


# ── get_latest ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_latest_returns_most_recent_non_null_value():
    """get_latest walks the series in reverse and returns the first
    non-None value. The most recent observations may not be published
    yet (FRED lags behind real time for some series)."""
    payload = {"observations": [
        {"date": "2024-10-01", "value": "5.33"},
        {"date": "2024-11-01", "value": "5.40"},
        {"date": "2024-12-01", "value": "."},  # latest entry, no data yet
    ]}
    patcher, _ = install_client(FakeResponse(payload))
    with patcher, with_api_key():
        latest = await fred.get_latest("FEDFUNDS")
    # Skips the "." sentinel and returns the previous month.
    assert latest == 5.40


@pytest.mark.asyncio
async def test_get_latest_returns_none_when_every_observation_is_missing():
    payload = {"observations": [
        {"date": "2024-11-01", "value": "."},
        {"date": "2024-12-01", "value": "."},
    ]}
    patcher, _ = install_client(FakeResponse(payload))
    with patcher, with_api_key():
        latest = await fred.get_latest("FEDFUNDS")
    assert latest is None


@pytest.mark.asyncio
async def test_get_latest_returns_none_when_series_is_empty():
    """Empty observations → None (rather than IndexError)."""
    patcher, _ = install_client(FakeResponse({"observations": []}))
    with patcher, with_api_key():
        latest = await fred.get_latest("FEDFUNDS")
    assert latest is None


@pytest.mark.asyncio
async def test_get_latest_returns_none_when_api_key_missing_and_no_fallback():
    """Empty key + no yfinance fallback (CPI is an economic release, not
    a market quote) → get_latest returns None without hitting the network."""
    with with_no_api_key():
        latest = await fred.get_latest("CPIAUCSL")
    assert latest is None


@pytest.mark.asyncio
async def test_get_latest_uses_yfinance_fallback_when_api_key_missing():
    """Empty key + tradable series ⇒ get_latest still returns a value
    via the yfinance fallback path."""
    fake_bars = [
        {"time": 1_704_067_200_000, "open": 100.0, "high": 102.0, "low": 99.0, "close": 100.5, "volume": 0},
        {"time": 1_706_745_600_000, "open": 100.5, "high": 103.0, "low": 99.5, "close": 102.0, "volume": 0},
    ]
    with with_no_api_key(), \
         patch.object(fred.yfinance, "get_history", new=lambda *a, **k: _coro(fake_bars)):
        latest = await fred.get_latest("DTWEXBGS")
    assert latest == 102.0
