"""
Pure unit tests for services/analytics_service.py — the orchestration layer
that sits between the HTTP router and the analytics/* pure-compute modules.

Market-data dependencies (us_history, tw_history, us_fundamentals) are
mocked via monkeypatch, so the tests focus on the glue logic:
  - input-data alignment (different symbol history lengths)
  - empty / insufficient data graceful handling
  - weight normalisation
  - delegation to the correct pure-analytics function
"""
import numpy as np
import pytest
from unittest.mock import AsyncMock

from services import analytics_service as svc


# ── _fetch_returns ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_returns_us_path(monkeypatch):
    """US market symbols should route to us_history (period=1y, interval=1d)."""
    bars = [{"time": f"2024-01-{i+1:02d}", "close": 100.0 + i} for i in range(30)]
    us_mock = AsyncMock(return_value=bars)
    tw_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(svc, "us_history", us_mock)
    monkeypatch.setattr(svc, "tw_history", tw_mock)

    result = await svc._fetch_returns(["AAPL"], ["US"])

    us_mock.assert_awaited_once_with("AAPL", period="1y", interval="1d")
    tw_mock.assert_not_called()
    assert "AAPL" in result
    assert len(result["AAPL"]) == 29   # N-1 returns from N closes


@pytest.mark.asyncio
async def test_fetch_returns_tw_path(monkeypatch):
    bars = [{"time": f"2024-01-{i+1:02d}", "close": 800.0 + i} for i in range(30)]
    us_mock = AsyncMock(return_value=[])
    tw_mock = AsyncMock(return_value=bars)
    monkeypatch.setattr(svc, "us_history", us_mock)
    monkeypatch.setattr(svc, "tw_history", tw_mock)

    result = await svc._fetch_returns(["2330"], ["TW"])

    tw_mock.assert_awaited_once_with("2330", months=12)
    us_mock.assert_not_called()
    assert "2330" in result


@pytest.mark.asyncio
async def test_fetch_returns_drops_symbols_with_insufficient_history(monkeypatch):
    """< 20 closes should cause the symbol to be silently dropped."""
    short_bars = [{"time": f"2024-01-{i+1:02d}", "close": 100.0} for i in range(10)]
    us_mock = AsyncMock(return_value=short_bars)
    monkeypatch.setattr(svc, "us_history", us_mock)
    monkeypatch.setattr(svc, "tw_history", AsyncMock(return_value=[]))

    result = await svc._fetch_returns(["AAPL"], ["US"])
    assert result == {}


@pytest.mark.asyncio
async def test_fetch_returns_filters_missing_closes(monkeypatch):
    """Bars with None / 0 close should be skipped when computing returns."""
    bars = [{"time": f"2024-01-{i+1:02d}", "close": None if i == 5 else 100.0 + i}
            for i in range(30)]
    monkeypatch.setattr(svc, "us_history", AsyncMock(return_value=bars))
    monkeypatch.setattr(svc, "tw_history", AsyncMock(return_value=[]))

    result = await svc._fetch_returns(["AAPL"], ["US"])
    # 29 valid closes (1 None dropped) → 28 returns
    assert len(result["AAPL"]) == 28
    # No NaN/inf in the returns array
    assert np.all(np.isfinite(result["AAPL"]))


@pytest.mark.asyncio
async def test_fetch_returns_handles_connector_exception(monkeypatch):
    """If the connector raises, the symbol is dropped not propagated."""
    monkeypatch.setattr(svc, "us_history", AsyncMock(side_effect=RuntimeError("API down")))
    monkeypatch.setattr(svc, "tw_history", AsyncMock(return_value=[]))

    result = await svc._fetch_returns(["AAPL"], ["US"])
    assert result == {}


@pytest.mark.asyncio
async def test_fetch_returns_mixed_markets(monkeypatch):
    """One call can mix US and TW symbols — each routed to the right connector."""
    us_bars = [{"time": f"2024-01-{i+1:02d}", "close": 100.0 + i} for i in range(30)]
    tw_bars = [{"time": f"2024-01-{i+1:02d}", "close": 800.0 + i} for i in range(30)]
    monkeypatch.setattr(svc, "us_history", AsyncMock(return_value=us_bars))
    monkeypatch.setattr(svc, "tw_history", AsyncMock(return_value=tw_bars))

    result = await svc._fetch_returns(["AAPL", "2330"], ["US", "TW"])
    assert set(result.keys()) == {"AAPL", "2330"}


# ── run_dcf_analysis ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dcf_tw_market_skips_fundamentals_fetch(monkeypatch):
    """TW path should not call us_fundamentals (no TW equivalent)."""
    us_fund_mock = AsyncMock()
    monkeypatch.setattr(svc, "us_fundamentals", us_fund_mock)

    result = await svc.run_dcf_analysis(
        "2330", "TW",
        overrides={"fcf_history": [100e9], "shares": 25e9},
    )

    us_fund_mock.assert_not_called()
    assert result["symbol"] == "2330"
    assert result["market"] == "TW"
    assert "intrinsic_value" in result


@pytest.mark.asyncio
async def test_dcf_us_path_calls_fundamentals(monkeypatch):
    monkeypatch.setattr(svc, "us_fundamentals", AsyncMock(return_value={
        "share_class_shares_outstanding": 15e9,
    }))

    result = await svc.run_dcf_analysis(
        "AAPL", "US",
        overrides={"fcf_history": [90e9, 100e9], "shares": 15e9},
    )
    assert result["symbol"] == "AAPL"
    assert result["market"] == "US"
    assert result["intrinsic_value"] > 0


@pytest.mark.asyncio
async def test_dcf_swallows_fundamentals_fetch_failure(monkeypatch):
    """If us_fundamentals raises, DCF still runs with override-supplied inputs."""
    monkeypatch.setattr(svc, "us_fundamentals", AsyncMock(side_effect=ConnectionError("fred down")))

    result = await svc.run_dcf_analysis(
        "AAPL", "US",
        overrides={"fcf_history": [90e9], "shares": 15e9},
    )
    assert "intrinsic_value" in result


# ── run_var_analysis ──────────────────────────────────────────────

def _returns_matrix(n_assets: int, n_days: int = 252, seed: int = 0) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)
    return {f"S{i}": rng.normal(0.0005, 0.012, n_days) for i in range(n_assets)}


@pytest.mark.asyncio
async def test_var_no_data_returns_error(monkeypatch):
    monkeypatch.setattr(svc, "_fetch_returns", AsyncMock(return_value={}))
    result = await svc.run_var_analysis(
        ["AAPL"], ["US"], [1.0], 100_000, method="historical",
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_var_historical_delegates_correctly(monkeypatch):
    monkeypatch.setattr(svc, "_fetch_returns", AsyncMock(return_value=_returns_matrix(1)))
    result = await svc.run_var_analysis(
        ["S0"], ["US"], [1.0], 100_000, method="historical",
    )
    assert "historical" in result
    assert "var_amount" in result["historical"]
    assert "portfolio_metrics" in result


@pytest.mark.asyncio
async def test_var_aligns_mismatched_history_lengths(monkeypatch):
    """When assets have different history lengths, the shorter one trims the longer."""
    returns = {
        "A": np.full(100, 0.001),   # short history
        "B": np.full(250, 0.002),   # long history
    }
    monkeypatch.setattr(svc, "_fetch_returns", AsyncMock(return_value=returns))

    result = await svc.run_var_analysis(
        ["A", "B"], ["US", "US"], [0.5, 0.5], 100_000, method="historical",
    )
    # Should succeed — the shorter series (100) dictates the aligned length
    assert "historical" in result


@pytest.mark.asyncio
async def test_var_weights_renormalised_to_sum_to_one(monkeypatch):
    monkeypatch.setattr(svc, "_fetch_returns", AsyncMock(return_value=_returns_matrix(2)))

    # Supplied weights [2.0, 2.0] should be normalised to [0.5, 0.5]
    result = await svc.run_var_analysis(
        ["S0", "S1"], ["US", "US"], [2.0, 2.0], 100_000, method="historical",
    )
    assert sum(result["weights"]) == pytest.approx(1.0)
    assert result["weights"] == [pytest.approx(0.5), pytest.approx(0.5)]


@pytest.mark.asyncio
async def test_var_parametric_path(monkeypatch):
    monkeypatch.setattr(svc, "_fetch_returns", AsyncMock(return_value=_returns_matrix(1)))
    result = await svc.run_var_analysis(
        ["S0"], ["US"], [1.0], 50_000, method="parametric", confidence=0.99,
    )
    assert "parametric" in result
    assert "historical" not in result   # only the requested method


# ── run_backtest_analysis ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_backtest_no_data_returns_failed(monkeypatch):
    monkeypatch.setattr(svc, "us_history", AsyncMock(return_value=[]))
    monkeypatch.setattr(svc, "tw_history", AsyncMock(return_value=[]))

    result = await svc.run_backtest_analysis(
        ["AAPL"], ["US"], "sma_crossover", {"fast": 10, "slow": 20},
        "2023-01-01", "2024-01-01",
    )
    assert result["status"] == "failed"


@pytest.mark.asyncio
async def test_backtest_date_range_filtering(monkeypatch):
    """Bars outside [start_date, end_date] should be discarded."""
    bars = [
        {"time": "2020-01-01", "close": 100.0},   # before window
        {"time": "2023-06-01", "close": 110.0},   # inside
        {"time": "2023-06-02", "close": 111.0},
        {"time": "2025-01-01", "close": 200.0},   # after window
    ]
    monkeypatch.setattr(svc, "us_history", AsyncMock(return_value=bars))
    monkeypatch.setattr(svc, "tw_history", AsyncMock(return_value=[]))

    # Only 2 bars in range → < 20 → backtest should report insufficient data
    result = await svc.run_backtest_analysis(
        ["AAPL"], ["US"], "sma_crossover", {"fast": 10, "slow": 20},
        "2023-01-01", "2024-01-01",
    )
    # Either failed with "Insufficient" or "failed" — not a crash
    assert result.get("status") == "failed"


@pytest.mark.asyncio
async def test_backtest_connector_exception_skipped(monkeypatch):
    """If one symbol's fetch raises, the backtest continues with the rest."""
    good_bars = [{"time": f"2023-{(i // 21) + 1:02d}-{(i % 21) + 1:02d}", "close": 100.0 + i}
                 for i in range(250)]

    async def selective_us_history(sym, **_):
        if sym == "BAD":
            raise ConnectionError("down")
        return good_bars

    monkeypatch.setattr(svc, "us_history", selective_us_history)
    monkeypatch.setattr(svc, "tw_history", AsyncMock(return_value=[]))

    result = await svc.run_backtest_analysis(
        ["BAD", "AAPL"], ["US", "US"], "sma_crossover",
        {"fast": 5, "slow": 10}, "2023-01-01", "2024-12-31",
    )
    # Should complete using only AAPL
    assert result.get("status") == "completed"
