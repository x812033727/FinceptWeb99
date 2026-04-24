"""
Pure unit tests for the FX rate helpers in services/portfolio_service.py.

These exercise the 3-tier cache behavior introduced for resilience:
  1. Fresh 4h Redis cache      (fx:twd_usd)
  2. Long-term last-known cache (fx:twd_usd:last_known, 30d)
  3. Hard 32.0 fallback         (only cold-cache + FRED-down)

Dependencies are mocked via monkeypatch so the test runs fully offline.
"""
import pytest
from unittest.mock import AsyncMock

from services import portfolio_service as svc


@pytest.fixture(autouse=True)
def _mute_logger(monkeypatch):
    """Silence the warning/error log lines the helper emits on fallbacks."""
    monkeypatch.setattr(svc.logger, "warning", lambda *a, **k: None)
    monkeypatch.setattr(svc.logger, "error", lambda *a, **k: None)


# ── _get_twd_usd_rate ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fx_returns_fresh_cache_when_present(monkeypatch):
    """If the fresh 4h cache has a value, FRED is not called."""
    get_latest = AsyncMock()
    cache_get = AsyncMock(side_effect=lambda key: "31.5" if key == "fx:twd_usd" else None)
    cache_set = AsyncMock()

    monkeypatch.setattr(svc, "cache_get", cache_get)
    monkeypatch.setattr(svc, "cache_set", cache_set)
    monkeypatch.setattr(svc, "get_latest", get_latest)

    rate = await svc._get_twd_usd_rate()

    assert rate == 31.5
    get_latest.assert_not_called()
    cache_set.assert_not_called()


@pytest.mark.asyncio
async def test_fx_hits_fred_when_fresh_cache_empty_and_populates_both_caches(monkeypatch):
    cache_store: dict[str, str] = {}

    async def fake_cache_get(key):
        return cache_store.get(key)

    async def fake_cache_set(key, value, ttl):
        cache_store[key] = value

    monkeypatch.setattr(svc, "cache_get", fake_cache_get)
    monkeypatch.setattr(svc, "cache_set", fake_cache_set)
    monkeypatch.setattr(svc, "get_latest", AsyncMock(return_value=32.12))

    rate = await svc._get_twd_usd_rate()

    assert rate == 32.12
    assert cache_store["fx:twd_usd"] == "32.12"
    assert cache_store["fx:twd_usd:last_known"] == "32.12"


@pytest.mark.asyncio
async def test_fx_falls_back_to_last_known_when_fred_down(monkeypatch):
    """FRED returns None → use the last-known-good rate in Redis."""
    stored = {"fx:twd_usd": None, "fx:twd_usd:last_known": "30.8"}

    async def fake_cache_get(key):
        return stored.get(key)

    monkeypatch.setattr(svc, "cache_get", fake_cache_get)
    monkeypatch.setattr(svc, "cache_set", AsyncMock())
    monkeypatch.setattr(svc, "get_latest", AsyncMock(return_value=None))

    rate = await svc._get_twd_usd_rate()
    assert rate == 30.8


@pytest.mark.asyncio
async def test_fx_uses_hard_fallback_when_everything_empty(monkeypatch):
    async def fake_cache_get(_key):
        return None

    monkeypatch.setattr(svc, "cache_get", fake_cache_get)
    monkeypatch.setattr(svc, "cache_set", AsyncMock())
    monkeypatch.setattr(svc, "get_latest", AsyncMock(return_value=None))

    rate = await svc._get_twd_usd_rate()
    assert rate == svc._FX_HARD_FALLBACK  # 32.0


@pytest.mark.asyncio
async def test_fx_fresh_cache_takes_priority_over_fred(monkeypatch):
    """Defence-in-depth: even when FRED returns a (stale) value, fresh cache wins."""
    get_latest = AsyncMock(return_value=35.0)   # would be wrong

    async def fake_cache_get(key):
        return "32.5" if key == "fx:twd_usd" else None

    monkeypatch.setattr(svc, "cache_get", fake_cache_get)
    monkeypatch.setattr(svc, "cache_set", AsyncMock())
    monkeypatch.setattr(svc, "get_latest", get_latest)

    rate = await svc._get_twd_usd_rate()
    assert rate == 32.5
    get_latest.assert_not_called()


# ── _to_portfolio_currency ────────────────────────────────────────

@pytest.mark.asyncio
async def test_to_portfolio_currency_identity_when_same_currency(monkeypatch):
    """No FX lookup should happen when currencies match."""
    get_latest = AsyncMock()
    cache_get = AsyncMock()
    monkeypatch.setattr(svc, "get_latest", get_latest)
    monkeypatch.setattr(svc, "cache_get", cache_get)

    result = await svc._to_portfolio_currency(1000.0, "USD", "USD")
    assert result == 1000.0
    get_latest.assert_not_called()
    cache_get.assert_not_called()


@pytest.mark.asyncio
async def test_to_portfolio_currency_twd_to_usd(monkeypatch):
    async def fake_cache_get(key):
        return "32.0" if key == "fx:twd_usd" else None

    monkeypatch.setattr(svc, "cache_get", fake_cache_get)
    monkeypatch.setattr(svc, "cache_set", AsyncMock())
    monkeypatch.setattr(svc, "get_latest", AsyncMock(return_value=32.0))

    # 320 TWD / 32 TWD-per-USD = 10 USD
    result = await svc._to_portfolio_currency(320.0, "TWD", "USD")
    assert result == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_to_portfolio_currency_usd_to_twd(monkeypatch):
    async def fake_cache_get(key):
        return "32.0" if key == "fx:twd_usd" else None

    monkeypatch.setattr(svc, "cache_get", fake_cache_get)
    monkeypatch.setattr(svc, "cache_set", AsyncMock())
    monkeypatch.setattr(svc, "get_latest", AsyncMock(return_value=32.0))

    result = await svc._to_portfolio_currency(10.0, "USD", "TWD")
    assert result == pytest.approx(320.0)


@pytest.mark.asyncio
async def test_to_portfolio_currency_unsupported_pair_returns_input(monkeypatch):
    """Unsupported currency pair (e.g. EUR↔JPY) — return amount unchanged."""
    monkeypatch.setattr(svc, "cache_get", AsyncMock(return_value="32.0"))
    monkeypatch.setattr(svc, "cache_set", AsyncMock())
    monkeypatch.setattr(svc, "get_latest", AsyncMock(return_value=32.0))

    result = await svc._to_portfolio_currency(100.0, "EUR", "JPY")
    assert result == 100.0
