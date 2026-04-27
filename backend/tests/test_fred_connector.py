"""
Unit tests for data.us.fred_connector.

FRED has two surface methods (get_series / get_latest) and one well-
known quirk: missing observations come back as the literal string "."
which the connector translates to None. The latest-value lookup also
needs to skip those Nones to find the most recent real reading.

httpx.AsyncClient is mocked at the module's import-site so no
network. settings.FRED_API_KEY is set per-test as needed.
"""
from unittest.mock import patch

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
    """Patch settings.FRED_API_KEY for tests that exercise the
    "key configured" branch."""
    return patch.object(fred.settings, "FRED_API_KEY", value)


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
async def test_get_series_returns_empty_when_api_key_not_configured():
    """No key → connector silently no-ops. Callers fall back to
    cached / static data instead of hitting an unauthenticated 401."""
    with patch.object(fred.settings, "FRED_API_KEY", ""):
        out = await fred.get_series("FEDFUNDS")
    assert out == []


@pytest.mark.asyncio
async def test_get_series_returns_empty_when_api_key_is_none():
    with patch.object(fred.settings, "FRED_API_KEY", None):
        out = await fred.get_series("FEDFUNDS")
    assert out == []


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
async def test_get_latest_returns_none_when_api_key_missing():
    """Empty key → get_series short-circuits → get_latest returns
    None without hitting the network."""
    with patch.object(fred.settings, "FRED_API_KEY", ""):
        latest = await fred.get_latest("FEDFUNDS")
    assert latest is None
