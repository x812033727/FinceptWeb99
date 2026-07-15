from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.fundamentals_snapshot import FundamentalsSnapshot
from models.ohlcv_daily import OhlcvDaily
from models.tw_company_classification_snapshot import TwCompanyClassificationSnapshot
from models.tw_company_info import TwCompanyInfo
from services import tw_factor_service as svc
from services.tw_statement_availability import statement_available_on, statement_row_available_as_of


def _bars(*, daily_return: float, volatility: float = 0, volume: int = 1_000_000) -> list[dict]:
    start = date(2025, 1, 1)
    price = 100.0
    rows = []
    for index in range(180):
        shock = volatility if index % 2 == 0 else -volatility
        price *= 1 + daily_return + shock
        rows.append({
            "date": (start + timedelta(days=index)).isoformat(),
            "close": price,
            "volume": volume,
        })
    return rows


def test_winsorized_zscores_caps_outlier_and_keeps_missing_absent():
    result = svc.winsorized_zscores({"A": 1, "B": 2, "C": 3, "D": 4, "X": 10_000, "N": None})
    assert "N" not in result
    assert result["X"] > result["D"]
    assert max(result.values()) < 3
    assert sum(result.values()) == pytest.approx(0, abs=1e-10)


def test_winsorized_zscores_constant_cross_section_returns_zero():
    assert svc.winsorized_zscores({"A": 5, "B": 5}) == {"A": 0.0, "B": 0.0}


def test_every_profile_has_normalized_quality_aware_weights():
    assert all(set(weights) == set(svc.FACTOR_NAMES) for weights in svc.PROFILES.values())
    assert all(sum(weights.values()) == pytest.approx(1) for weights in svc.PROFILES.values())


def test_walk_forward_weights_use_only_mature_labels_and_stay_bounded():
    base = svc.PROFILES["balanced"]
    history = []
    for index in range(12):
        history.append({
            "available_on": (date(2024, 1, 1) + timedelta(days=index * 21)).isoformat(),
            "rank_ic": {
                factor: (.12 if factor == "quality" else -.08 if factor == "momentum" else .01)
                for factor in svc.FACTOR_NAMES
            },
        })
    history.append({
        "available_on": "2026-01-01",
        "rank_ic": {factor: (-1 if factor == "quality" else 1) for factor in svc.FACTOR_NAMES},
    })

    weights, metadata = svc.learn_walk_forward_weights(
        base_weights=base, learning_history=history, as_of=date(2025, 1, 1),
    )

    assert metadata["source_period_count"] == 12
    assert metadata["fallback_reason"] is None
    assert weights["quality"] > base["quality"]
    assert weights["momentum"] < base["momentum"]
    assert sum(weights.values()) == pytest.approx(1)
    for factor, weight in weights.items():
        assert base[factor] * .5 <= weight <= base[factor] * 1.5


def test_walk_forward_weights_fall_back_before_minimum_sample():
    base = svc.PROFILES["balanced"]
    weights, metadata = svc.learn_walk_forward_weights(
        base_weights=base,
        learning_history=[{
            "available_on": "2024-01-01",
            "rank_ic": {factor: .1 for factor in svc.FACTOR_NAMES},
        }] * 11,
        as_of=date(2025, 1, 1),
    )
    assert weights == base
    assert metadata["fallback_reason"] == "insufficient_mature_labels"


def test_statement_availability_waits_for_statutory_deadline():
    assert statement_available_on(date(2024, 9, 30)) == date(2024, 11, 15)
    assert statement_available_on(date(2024, 12, 31)) == date(2025, 4, 1)
    assert statement_available_on(date(2023, 12, 31)) == date(2024, 4, 2)
    assert not statement_row_available_as_of(
        {"date": "2024-09-30"}, date(2024, 11, 14),
    )
    assert statement_row_available_as_of(
        {"date": "2024-09-30"}, date(2024, 11, 15),
    )


def test_quality_factor_uses_only_available_multimetric_statement_snapshot():
    symbols = ["1101", "1102", "1103"]
    fundamentals = {
        symbol: {"as_of": "2025-06-20", "pe_ratio": 10, "pb_ratio": 1,
                 "dividend_yield": 2}
        for symbol in symbols
    }
    quality = {
        symbol: {
            "period_end": "2025-03-31", "available_on": "2025-05-16",
            "operating_margin": .1 + index * .1,
            "return_on_assets": .02 + index * .02,
            "cash_return_on_assets": .03 + index * .02,
            "balance_strength": -.7 + index * .2,
        }
        for index, symbol in enumerate(symbols)
    }
    result = svc.build_factor_ranking(
        fundamentals=fundamentals,
        bars_by_symbol={symbol: _bars(daily_return=.001) for symbol in symbols},
        companies={symbol: {"name_zh": symbol, "industry": "測試"} for symbol in symbols},
        as_of=date(2025, 6, 29), profile="balanced", limit=10,
        quality_by_symbol=quality, quality_availability_approximated=True,
        sector_neutral=False,
    )

    by_symbol = {row["symbol"]: row for row in result["candidates"]}
    assert by_symbol["1103"]["factors"]["quality"]["z"] > by_symbol["1101"]["factors"]["quality"]["z"]
    assert by_symbol["1103"]["quality_available_on"] == "2025-05-16"
    assert result["quality"]["quality_factor_coverage_pct"] == 100
    assert "financial_statement_availability_approximated" in result["quality"]["flags"]


def test_security_master_excludes_fund_like_symbol_without_code_heuristic():
    symbols = ["2330", "1234"]
    result = svc.build_factor_ranking(
        fundamentals={
            symbol: {
                "as_of": "2025-06-20", "pe_ratio": 10,
                "pb_ratio": 1, "dividend_yield": 2,
            }
            for symbol in symbols
        },
        bars_by_symbol={symbol: _bars(daily_return=.001) for symbol in symbols},
        companies={symbol: {"name_zh": symbol, "industry": "測試"} for symbol in symbols},
        as_of=date(2025, 6, 29),
        sector_neutral=False,
        security_profiles={
            "2330": {"is_etf": False},
            "1234": {"is_etf": True},
        },
        security_master_coverage_pct=100,
    )
    assert [row["symbol"] for row in result["candidates"]] == ["2330"]
    assert result["quality"]["security_master_coverage_pct"] == 100
    assert "security_master_fallback" not in result["quality"]["flags"]


def test_point_in_time_quality_rejects_unpublished_and_stale_periods():
    history = {
        "2330": [
            {"period_end": "2024-09-30", "available_on": "2024-11-15",
             "operating_margin": .4},
            {"period_end": "2024-12-31", "available_on": "2025-04-01",
             "operating_margin": .5},
        ],
    }
    november = svc.point_in_time_quality(history=history, as_of=date(2024, 11, 30))
    assert november["2330"]["period_end"] == "2024-09-30"
    assert svc.point_in_time_quality(history=history, as_of=date(2024, 10, 31)) == {}
    assert svc.point_in_time_quality(history=history, as_of=date(2025, 12, 31)) == {}


@pytest.mark.asyncio
async def test_quality_statement_loader_combines_three_statements():
    income = SimpleNamespace(
        symbol="2330", period="2024Q3", period_end=date(2024, 9, 30),
        revenue=1000, operating_income=300, net_income=200, raw=None,
    )
    balance = SimpleNamespace(
        symbol="2330", period="2024Q3", period_end=date(2024, 9, 30),
        total_assets=2000, total_liabilities=800, raw=None,
    )
    cash = SimpleNamespace(
        symbol="2330", period="2024Q3", period_end=date(2024, 9, 30),
        operating_cash_flow=250, raw=None,
    )
    db = AsyncMock()
    db.scalars.side_effect = [
        MagicMock(all=lambda: [income]),
        MagicMock(all=lambda: [balance]),
        MagicMock(all=lambda: [cash]),
    ]
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=db)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    with patch("finmind.db.session.FinmindAsyncSessionLocal", factory):
        history, ready = await svc._load_quality_statement_history(end=date(2025, 1, 1))

    snapshot = history["2330"][0]
    assert ready is True
    assert snapshot["available_on"] == "2024-11-15"
    assert snapshot["operating_margin"] == pytest.approx(.3)
    assert snapshot["return_on_assets"] == pytest.approx(.1)
    assert snapshot["cash_return_on_assets"] == pytest.approx(.125)
    assert snapshot["balance_strength"] == pytest.approx(-.4)


def test_value_subfactors_are_standardised_before_composition():
    snapshots = {
        "1101": {"as_of": "2025-06-20", "pe_ratio": 5, "pb_ratio": 10, "dividend_yield": 2},
        "1216": {"as_of": "2025-06-20", "pe_ratio": 10, "pb_ratio": 5, "dividend_yield": 2},
        "1301": {"as_of": "2025-06-20", "pe_ratio": 20, "pb_ratio": 2, "dividend_yield": 2},
    }
    earnings_z = svc.winsorized_zscores({symbol: 1 / row["pe_ratio"] for symbol, row in snapshots.items()})
    book_z = svc.winsorized_zscores({symbol: 1 / row["pb_ratio"] for symbol, row in snapshots.items()})
    result = svc.build_factor_ranking(
        fundamentals=snapshots,
        bars_by_symbol={symbol: _bars(daily_return=.001) for symbol in snapshots},
        companies={symbol: {"name_zh": symbol, "industry": "測試"} for symbol in snapshots},
        as_of=date(2025, 6, 29), profile="value", limit=10,
    )
    by_symbol = {row["symbol"]: row for row in result["candidates"]}
    for symbol in snapshots:
        expected = (earnings_z[symbol] + book_z[symbol]) / 2
        assert by_symbol[symbol]["factors"]["value"]["raw"] == pytest.approx(expected, abs=1e-6)


def test_build_factor_ranking_is_explainable_deterministic_and_ranked():
    as_of = date(2025, 6, 29)
    fundamentals = {
        "1101": {"as_of": "2025-06-20", "pe_ratio": 8, "pb_ratio": 0.8, "dividend_yield": 6},
        "1216": {"as_of": "2025-06-20", "pe_ratio": 12, "pb_ratio": 1.5, "dividend_yield": 4},
        "1301": {"as_of": "2025-06-20", "pe_ratio": 16, "pb_ratio": 2.0, "dividend_yield": 3},
        "2002": {"as_of": "2025-06-20", "pe_ratio": 20, "pb_ratio": 2.5, "dividend_yield": 2},
        "2330": {"as_of": "2025-06-20", "pe_ratio": 25, "pb_ratio": 5.0, "dividend_yield": 1.5},
        "0050": {"as_of": "2025-06-20", "pe_ratio": 10, "pb_ratio": 1.0, "dividend_yield": 5},
    }
    bars = {
        "1101": _bars(daily_return=0.003, volatility=0.0005, volume=3_000_000),
        "1216": _bars(daily_return=0.002, volatility=0.001, volume=2_000_000),
        "1301": _bars(daily_return=0.001, volatility=0.002),
        "2002": _bars(daily_return=0.0005, volatility=0.003, volume=800_000),
        "2330": _bars(daily_return=0, volatility=0.004, volume=500_000),
        "0050": _bars(daily_return=0.01),
    }
    companies = {symbol: {"name_zh": symbol, "industry": "測試"}
                 for symbol in fundamentals if symbol != "0050"}

    result = svc.build_factor_ranking(
        fundamentals=fundamentals, bars_by_symbol=bars, companies=companies,
        as_of=as_of, profile="balanced", limit=5,
        quality_by_symbol={
            symbol: {
                "period_end": "2025-03-31", "available_on": "2025-05-16",
                "operating_margin": .2, "return_on_assets": .03,
            }
            for symbol in companies
        },
    )

    assert [row["symbol"] for row in result["candidates"]] == ["1101", "1216", "1301", "2002", "2330"]
    assert [row["rank"] for row in result["candidates"]] == [1, 2, 3, 4, 5]
    assert result["candidates"][0]["score"] == 100
    assert result["candidates"][0]["missing_factors"] == []
    assert set(result["candidates"][0]["factors"]) == set(svc.FACTOR_NAMES)
    assert result["methodology_version"] == "tw-explainable-multifactor-v9"
    assert "0050" not in {row["symbol"] for row in result["candidates"]}
    assert "unadjusted_price_history" in result["quality"]["flags"]


def test_adjusted_prices_drive_returns_but_not_display_price_or_liquidity():
    bars = {
        "2330": [
            {"date": "2025-01-01", "close": 100, "volume": 10},
            {"date": "2025-01-02", "close": 50, "volume": 10},
        ],
    }
    merged = svc.merge_adjusted_prices(
        bars, {"2330": {"2025-01-01": 50, "2025-01-02": 50}},
    )
    factors = svc._price_factors(merged["2330"])

    assert factors["latest_close"] == 50
    assert factors["adjusted_observations"] == 2
    assert merged["2330"][1]["close"] / merged["2330"][0]["close"] - 1 == 0


def test_adjusted_sidecar_preserves_delisted_symbol_missing_from_raw_archive():
    merged = svc.merge_adjusted_prices(
        {}, {"1111": {"2020-01-01": 10, "2020-01-02": 11}},
    )

    assert [row["close"] for row in merged["1111"]] == [10, 11]
    assert all(row["adjusted"] for row in merged["1111"])
    assert all(row["volume"] is None for row in merged["1111"])


def test_point_in_time_universe_requires_data_for_relevant_delisted_names():
    kwargs = {
        "catalog_ready": True,
        "as_of": date(2022, 1, 1),
        "delistings": {"1111": date(2023, 1, 1)},
        "fundamentals": {"1111": {"as_of": "2021-12-20"}},
    }
    assert not svc.point_in_time_universe_ready(
        **kwargs, bars_by_symbol={},
    )
    assert svc.point_in_time_universe_ready(
        **kwargs, bars_by_symbol={"1111": [{"date": "2022-01-01", "close": 10}]},
    )
    assert not svc.point_in_time_universe_ready(
        **{**kwargs, "fundamentals": {"1111": {"as_of": "2020-01-01"}}},
        bars_by_symbol={"1111": [{"date": "2022-01-01", "close": 10}]},
    )


def test_point_in_time_companies_reintroduces_delisted_names_at_historical_anchor():
    bars = {
        "1111": [{"date": "2020-01-02", "close": 10}],
        "2222": [{"date": "2024-01-02", "close": 20}],
    }
    result = svc.point_in_time_companies(
        as_of=date(2022, 1, 1),
        current={"2222": {"name_zh": "目前公司"}},
        stock_info={"1111": {"name_zh": "已下市公司", "listed_at": date(2010, 1, 1)}},
        delistings={"1111": date(2023, 1, 1)},
        bars_by_symbol=bars,
    )

    assert set(result) == {"1111"}
    assert result["1111"]["name_zh"] == "已下市公司"


def test_point_in_time_classification_uses_latest_snapshot_not_future_value():
    companies, coverage = svc.apply_point_in_time_classifications(
        as_of=date(2024, 6, 30),
        companies={"2330": {"industry": "目前分類"}},
        snapshots_by_symbol={"2330": [
            {"snapshot_date": "2024-01-01", "industry": "半導體業"},
            {"snapshot_date": "2025-01-01", "industry": "其他電子業"},
        ]},
        universe_symbols={"2330"},
    )

    assert companies["2330"]["industry"] == "半導體業"
    assert companies["2330"]["classification_as_of"] == "2024-01-01"
    assert coverage == 100


def test_sector_neutral_ranking_demeans_industry_and_excludes_singleton_group():
    anchor = date(2025, 6, 29)
    symbols = ["1101", "1102", "2201", "2202", "9901"]
    fundamentals = {
        symbol: {
            "as_of": "2025-06-20", "pe_ratio": 8 + index * 3,
            "pb_ratio": 1 + index * .4, "dividend_yield": 5 - index * .5,
        }
        for index, symbol in enumerate(symbols)
    }
    industries = {
        "1101": "水泥業", "1102": "水泥業",
        "2201": "汽車業", "2202": "汽車業", "9901": "單一產業",
    }
    result = svc.build_factor_ranking(
        fundamentals=fundamentals,
        bars_by_symbol={
            symbol: _bars(daily_return=.0005 + index * .0005)
            for index, symbol in enumerate(symbols)
        },
        companies={
            symbol: {"name_zh": symbol, "industry": industries[symbol]}
            for symbol in symbols
        },
        as_of=anchor, profile="balanced", limit=10,
        classification_coverage_pct=100, sector_neutral=True,
    )

    assert result["quality"]["sector_neutral_applied"] is True
    assert result["quality"]["sector_coverage_pct"] == 80
    assert "9901" not in {row["symbol"] for row in result["candidates"]}
    assert all(row["sector_adjustment"] is not None for row in result["candidates"])


def test_execution_model_defers_limit_locks_and_scales_to_capacity():
    sessions = [date(2025, 1, 1) + timedelta(days=index) for index in range(8)]
    bars = [
        {"date": session.isoformat(), "close": 100 + index * 10,
         "raw_close": 100 + index * 10, "volume": 100}
        for index, session in enumerate(sessions)
    ]
    limits = {"2330": {
        sessions[1].isoformat(): {"upper_limit": 110, "lower_limit": 90},
        sessions[4].isoformat(): {"upper_limit": 160, "lower_limit": 140},
    }}

    trade = svc.simulate_forward_trade(
        symbol="2330", bars=bars, market_sessions=sessions,
        anchor=sessions[0], holding_sessions=2,
        target_notional_twd=10_000, max_participation_rate=.05,
        impact_coefficient_bps=10, price_limits=limits, suspensions={},
    )

    assert trade is not None and trade["executed"] is True
    assert trade["entry_session"] == sessions[2].isoformat()
    assert trade["exit_session"] == sessions[5].isoformat()
    assert trade["entry_delay_sessions"] == 1
    assert trade["exit_delay_sessions"] == 1
    assert trade["capacity_limited"] is True
    assert trade["fill_ratio"] == pytest.approx((120 * 100 * .05) / 10_000)
    assert trade["impact_cost"] > 0


def test_execution_model_blocks_when_volume_is_missing():
    sessions = [date(2025, 1, 1) + timedelta(days=index) for index in range(4)]
    bars = [{"date": session.isoformat(), "close": 100, "volume": None}
            for session in sessions]
    trade = svc.simulate_forward_trade(
        symbol="1111", bars=bars, market_sessions=sessions,
        anchor=sessions[0], holding_sessions=1,
        target_notional_twd=1_000_000, max_participation_rate=.05,
        impact_coefficient_bps=10, price_limits={}, suspensions={},
    )
    assert trade == {"executed": False, "blocked_side": "capacity"}


def test_total_return_benchmark_uses_fixed_market_sessions_and_past_volatility():
    sessions = [date(2025, 1, 1) + timedelta(days=index) for index in range(90)]
    bars = [
        {"date": session.isoformat(), "close": 100 * (1.001 ** index)}
        for index, session in enumerate(sessions)
    ]
    anchor = sessions[70]

    forward = svc.benchmark_forward_return(
        bars=bars, market_sessions=sessions, anchor=anchor, holding_sessions=10,
    )
    before = svc.benchmark_volatility(bars=bars, anchor=anchor)
    bars[-1]["close"] = 10_000_000
    after = svc.benchmark_volatility(bars=bars, anchor=anchor)

    assert forward == pytest.approx(1.001 ** 10 - 1)
    assert after == before


def test_excess_statistics_and_regimes_are_deterministic():
    values = [.01, .02, -.01, .03, .005, .015]
    assert svc.excess_return_statistics(values) == svc.excess_return_statistics(values)
    stats = svc.excess_return_statistics(values)
    assert stats["excess_return_t_stat"] is not None
    assert stats["excess_return_ci_low_pct"] is not None

    periods = [
        {"benchmark_return_pct": 2, "net_return_pct": 3, "excess_return_pct": 1,
         "benchmark_volatility_pct": 12},
        {"benchmark_return_pct": -2, "net_return_pct": -1, "excess_return_pct": 1,
         "benchmark_volatility_pct": 24},
    ]
    regimes = svc.build_regime_analysis(periods)
    assert regimes["bull"]["period_count"] == 1
    assert regimes["bear"]["period_count"] == 1
    assert periods[0]["market_regime"] == "bull"


def test_rank_ic_quintiles_and_holm_correction_identify_monotonic_signal():
    candidates = []
    forward_returns = {}
    for index in range(20):
        symbol = f"{index:04d}"
        forward_returns[symbol] = index / 1_000
        candidates.append({
            "symbol": symbol, "composite_z": float(index),
            "factors": {
                factor: {"z": float(index) if factor == "momentum" else float(index % 4)}
                for factor in svc.FACTOR_NAMES
            },
        })

    cross_section = svc.diagnose_factor_cross_section(
        candidates=candidates, forward_returns=forward_returns,
    )

    assert cross_section["rank_ic"]["composite"] == pytest.approx(1)
    assert cross_section["rank_ic"]["momentum"] == pytest.approx(1)
    assert cross_section["top_bottom_spread_pct"] > 0
    assert len(cross_section["quintile_returns_pct"]) == 5

    periods = []
    correlations = []
    for index in range(12):
        rank_ic = {signal: None for signal in svc.DIAGNOSTIC_SIGNALS}
        rank_ic["composite"] = .15 + index / 1_000
        rank_ic["momentum"] = .12 + index / 1_000
        periods.append({
            "rank_ic": rank_ic,
            "quintile_returns_pct": cross_section["quintile_returns_pct"],
            "top_bottom_spread_pct": cross_section["top_bottom_spread_pct"],
        })
        correlations.append(cross_section["factor_correlations"])
    diagnostics, matrix, quantiles = svc.aggregate_factor_diagnostics(
        periods=periods, correlation_samples=correlations, holding_sessions=21,
    )

    assert diagnostics["composite"]["significant_after_holm_5pct"] is True
    assert diagnostics["composite"]["holm_adjusted_p_value"] >= diagnostics["composite"]["p_value"]
    assert matrix["momentum"]["momentum"] == pytest.approx(1)
    assert quantiles["period_count"] == 12


def test_build_factor_ranking_abstains_below_weight_coverage():
    anchor = date(2025, 6, 29)
    short_bars = _bars(daily_return=0.001)[:20]
    for index, row in enumerate(short_bars):
        row["date"] = (anchor - timedelta(days=19 - index)).isoformat()
    result = svc.build_factor_ranking(
        fundamentals={"2330": {"as_of": "2025-06-20", "pe_ratio": 20,
                                "pb_ratio": 4, "dividend_yield": 2}},
        bars_by_symbol={"2330": short_bars},
        companies={"2330": {"name_zh": "台積電", "industry": "半導體"}},
        as_of=anchor, profile="balanced", limit=10,
    )
    # value .30 + income .10 + liquidity .15 = .55, below the .60 gate.
    assert result["candidates"] == []
    assert result["quality"]["status"] == "unavailable"


def test_build_factor_ranking_flags_historical_bias_and_stale_snapshots():
    result = svc.build_factor_ranking(
        fundamentals={"2330": {"as_of": "2024-01-01", "pe_ratio": 20}},
        bars_by_symbol={"2330": _bars(daily_return=0.001)},
        companies={"2330": {"name_zh": "台積電", "industry": "半導體"}},
        as_of=date(2025, 6, 29), profile="balanced", limit=10, historical=True,
    )
    assert result["candidates"] == []
    assert result["quality"]["stale_fundamentals_excluded"] == 1
    assert "survivorship_bias" in result["quality"]["flags"]
    assert "sector_classification_not_point_in_time" in result["quality"]["flags"]


def test_build_factor_ranking_excludes_stale_price_session():
    result = svc.build_factor_ranking(
        fundamentals={"2330": {"as_of": "2025-12-25", "pe_ratio": 20,
                                "pb_ratio": 4, "dividend_yield": 2}},
        bars_by_symbol={"2330": _bars(daily_return=0.001)},
        companies={"2330": {"name_zh": "台積電", "industry": "半導體"}},
        as_of=date(2025, 12, 31), profile="value", limit=10,
    )
    assert result["candidates"] == []
    assert result["quality"]["stale_price_history_excluded"] == 1
    assert "stale_price_history_excluded" in result["quality"]["flags"]


def test_build_factor_ranking_rejects_future_dated_fundamental_input():
    result = svc.build_factor_ranking(
        fundamentals={"2330": {"as_of": "2025-07-01", "pe_ratio": 20,
                                "pb_ratio": 4, "dividend_yield": 2}},
        bars_by_symbol={"2330": _bars(daily_return=0.001)},
        companies={"2330": {"name_zh": "台積電", "industry": "半導體"}},
        as_of=date(2025, 6, 29), profile="value", limit=10,
    )
    assert result["candidates"] == []
    assert result["quality"]["future_dated_inputs_excluded"] == 1
    assert "future_dated_inputs_excluded" in result["quality"]["flags"]


def test_unknown_profile_is_rejected():
    with pytest.raises(ValueError, match="unknown factor profile"):
        svc.build_factor_ranking(
            fundamentals={}, bars_by_symbol={}, companies={}, as_of=date.today(), profile="magic",
        )


@pytest.mark.asyncio
async def test_load_inputs_uses_latest_snapshot_at_or_before_anchor(db_session: AsyncSession):
    anchor = date(2025, 6, 30)
    db_session.add_all([
        FundamentalsSnapshot(
            market="TW", symbol="9876", as_of=date(2025, 6, 20),
            pe_ratio=10, pb_ratio=1, dividend_yield=5, eps=None, revenue=None,
            payload=None, source="twse",
        ),
        FundamentalsSnapshot(
            market="TW", symbol="9876", as_of=date(2025, 7, 1),
            pe_ratio=99, pb_ratio=9, dividend_yield=0, eps=None, revenue=None,
            payload=None, source="future",
        ),
        TwCompanyInfo(symbol="9876", exchange="TWSE", industry="測試業", name_zh="測試公司"),
        OhlcvDaily(
            market="TW", symbol="9876", ts=date(2025, 6, 27), open=99, high=101,
            low=98, close=100, volume=1000, source="twse",
        ),
    ])
    await db_session.flush()

    fundamentals, bars, companies = await svc._load_inputs(
        db_session, as_of=anchor, start=date(2025, 1, 1),
    )

    assert fundamentals["9876"]["as_of"] == "2025-06-20"
    assert fundamentals["9876"]["pe_ratio"] == 10
    assert bars["9876"][0]["date"] == "2025-06-27"
    assert companies["9876"]["industry"] == "測試業"


@pytest.mark.asyncio
async def test_load_classification_history_orders_snapshots(db_session: AsyncSession):
    db_session.add_all([
        TwCompanyClassificationSnapshot(
            snapshot_date=date(2025, 2, 1), symbol="2330", exchange="TWSE",
            industry="其他電子業", name_zh="台積電", source="test",
        ),
        TwCompanyClassificationSnapshot(
            snapshot_date=date(2025, 1, 1), symbol="2330", exchange="TWSE",
            industry="半導體業", name_zh="台積電", source="test",
        ),
    ])
    await db_session.flush()

    result = await svc._load_classification_history(
        db_session, end=date(2025, 12, 31),
    )

    assert [row["snapshot_date"] for row in result["2330"]] == [
        "2025-01-01", "2025-02-01",
    ]


@pytest.mark.asyncio
async def test_get_factor_ranking_cache_hit_skips_database_scan():
    cached = {
        "quality": {"status": "good"}, "candidates": [{"symbol": "2330"}],
    }
    with patch.object(svc, "cache_get_json", new_callable=AsyncMock, return_value=cached), \
         patch.object(svc, "AsyncSessionLocal") as session_factory:
        result = await svc.get_factor_ranking(
            as_of=date(2025, 6, 30), profile="balanced", limit=50,
        )

    assert result is cached
    session_factory.assert_not_called()


@pytest.mark.asyncio
async def test_rolling_validation_uses_next_session_costs_and_returns_periods(
    db_session: AsyncSession,
):
    symbols = ["9101", "9102", "9103", "9104", "9105"]
    first = date(2024, 1, 1)
    last = date(2025, 8, 1)
    total_days = (last - first).days + 1
    for symbol_index, symbol in enumerate(symbols):
        db_session.add(TwCompanyInfo(
            symbol=symbol, exchange="TWSE", industry="測試業", name_zh=f"測試{symbol}",
        ))
        price = 50.0 + symbol_index
        for day_index in range(total_days):
            session = first + timedelta(days=day_index)
            price *= 1 + 0.0002 * (symbol_index + 1)
            db_session.add(OhlcvDaily(
                market="TW", symbol=symbol, ts=session, open=price, high=price,
                low=price, close=price, volume=1_000_000 + symbol_index * 100_000,
                source="test",
            ))
            if day_index % 15 == 0:
                db_session.add(FundamentalsSnapshot(
                    market="TW", symbol=symbol, as_of=session,
                    pe_ratio=10 + symbol_index, pb_ratio=1 + symbol_index * .2,
                    dividend_yield=5 - symbol_index * .5, eps=None, revenue=None,
                    payload=None, source="test",
                ))
    index_price = 20_000.0
    for day_index in range(total_days):
        session = first + timedelta(days=day_index)
        index_price *= 1.0003
        db_session.add(OhlcvDaily(
            market="TW", symbol="_TAIEX_TR", ts=session, open=index_price,
            high=index_price, low=index_price, close=index_price, volume=0,
            source="test",
        ))
    await db_session.flush()

    with patch.object(svc, "cache_get_json", new_callable=AsyncMock, return_value=None), \
         patch.object(svc, "cache_set_json", new_callable=AsyncMock):
        result = await svc.validate_factor_ranking(
            start_date=date(2025, 1, 1), end_date=date(2025, 4, 30),
            profile="balanced", top_n=5, holding_sessions=21, transaction_cost_bps=20,
            impact_coefficient_bps=0,
        )

    assert result["quality"]["status"] == "degraded"
    assert len(result["periods"]) >= 3
    assert result["periods"][0]["turnover"] == 1
    assert result["periods"][0]["cost_pct"] == pytest.approx(.2)
    assert result["summary"]["period_count"] == len(result["periods"])
    assert result["benchmark_used"] == "taiex_total_return"
    assert result["quality"]["benchmark_coverage_pct"] == 100
    assert result["summary"]["excess_return_t_stat"] is not None
    assert result["regime_analysis"]["bull"]["period_count"] > 0
    assert set(result["sensitivity_analysis"]["holding_sessions"]) == {"5", "21", "63"}
    assert "5" in result["sensitivity_analysis"]["top_n"]
    assert result["weight_mode"] == "walk_forward"
    assert result["weight_stability"]["adaptive_period_count"] == 0
    assert result["weight_stability"]["fallback_period_count"] == len(result["periods"])
    assert "walk_forward_weights_unavailable" in result["quality"]["flags"]
    assert all(
        period["weight_fallback_reason"] == "insufficient_mature_labels"
        and sum(period["factor_weights"].values()) == pytest.approx(1)
        for period in result["periods"]
    )
    assert set(result["factor_decay_analysis"]) == set(svc.DIAGNOSTIC_SIGNALS)
    assert set(result["factor_decay_analysis"]["composite"]["average_rank_ic_by_horizon"]) == {
        "5", "21", "63",
    }
    assert "survivorship_bias" in result["quality"]["flags"]
