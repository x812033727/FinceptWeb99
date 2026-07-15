from unittest.mock import AsyncMock, patch

import pytest

from analytics.tw_factor_rebalance import build_tw_factor_trades
from services.tw_factor_rebalance_service import build_factor_rebalance_preview


def _target(symbol: str, weight: float, price: float, adv: float = 100_000_000):
    return {
        "symbol": symbol, "weight": weight, "price": price,
        "average_daily_value_twd": adv,
    }


def test_factor_rebalance_supports_new_buys_sells_costs_and_post_trade_drift():
    result = build_tw_factor_trades(
        target_positions=[_target("2330", .5, 100), _target("2317", .3, 50)],
        current_positions=[
            {"symbol": "2330", "quantity": 1_000, "price": 100},
            {"symbol": "0050", "quantity": 1_000, "price": 100},
        ],
        portfolio_notional_twd=300_000, initial_cash_twd=100_000,
        allow_odd_lot=True, min_trade_pct=0,
    )
    by_symbol = {row["symbol"]: row for row in result["trades"]}
    assert by_symbol["2330"]["side"] == "buy"
    assert by_symbol["2317"]["side"] == "buy"
    assert by_symbol["0050"]["side"] == "sell"
    assert by_symbol["0050"]["tax_twd"] > 0
    assert by_symbol["0050"]["liquidity_data_available"] is False
    assert by_symbol["0050"]["impact_bps"] == 100
    # ETF default sell tax is lower than the stock rate and all costs remain explicit.
    assert by_symbol["0050"]["tax_twd"] < by_symbol["0050"]["gross_value_twd"] * .0031
    assert len(result["cost_scenarios"]) == 3
    assert result["cost_scenarios"][0]["estimated_cost_twd"] < result["cost_scenarios"][2]["estimated_cost_twd"]
    assert any(row["symbol"] == "2317" for row in result["post_positions"])
    assert result["summary"]["estimated_total_cost_twd"] > 0


def test_factor_rebalance_board_lots_round_toward_zero_and_reveal_shortfall():
    result = build_tw_factor_trades(
        target_positions=[_target("2330", 1, 100)], current_positions=[],
        portfolio_notional_twd=150_000, initial_cash_twd=150_000,
        allow_odd_lot=False, min_trade_pct=0, fee_bps=100,
    )
    assert result["trades"][0]["quantity"] == 1_000
    assert result["summary"]["funded"] is True
    assert result["summary"]["ending_cash_twd"] > 0

    short = build_tw_factor_trades(
        target_positions=[_target("2330", 1, 100)], current_positions=[],
        portfolio_notional_twd=100_000, initial_cash_twd=100_000,
        allow_odd_lot=False, min_trade_pct=0, fee_bps=100,
    )
    assert short["summary"]["funded"] is False
    assert short["summary"]["funding_shortfall_twd"] > 0


def test_factor_rebalance_rejects_non_finite_inputs():
    try:
        build_tw_factor_trades(
            target_positions=[], current_positions=[], portfolio_notional_twd=100_000,
            initial_cash_twd=float("nan"),
        )
    except ValueError as exc:
        assert "finite" in str(exc)
    else:
        raise AssertionError("expected invalid cash to be rejected")


def test_factor_rebalance_supports_symbol_tax_override():
    result = build_tw_factor_trades(
        target_positions=[],
        current_positions=[{"symbol": "0050", "quantity": 1_000, "price": 100}],
        portfolio_notional_twd=100_000, initial_cash_twd=0,
        allow_odd_lot=True, min_trade_pct=0, sell_tax_bps_by_symbol={"0050": 0},
    )
    assert result["trades"][0]["tax_twd"] == 0


def test_factor_rebalance_uses_effective_security_master_lot_and_tax_metadata():
    result = build_tw_factor_trades(
        target_positions=[],
        current_positions=[{"symbol": "00980B", "quantity": 2_000, "price": 50}],
        portfolio_notional_twd=100_000,
        initial_cash_twd=0,
        allow_odd_lot=False,
        min_trade_pct=0,
        sell_tax_bps_by_symbol={"00980B": 0},
        lot_size_by_symbol={"00980B": 500},
        trading_rule_by_symbol={
            "00980B": {
                "source": "twse_tpex_master",
                "tax_rule_code": "bond_etf_suspended_through_2026",
            },
        },
    )
    trade = result["trades"][0]
    assert trade["quantity"] == 2_000
    assert trade["board_lot_size"] == 500
    assert trade["tax_twd"] == 0
    assert trade["sell_tax_bps"] == 0
    assert trade["trading_rule_source"] == "twse_tpex_master"


@pytest.mark.asyncio
async def test_preview_is_owner_scoped_freezes_non_tw_and_derives_notional():
    detail = {
        "name": "核心", "currency": "USD", "holdings": [
            {"symbol": "2330", "market": "TW", "quantity": 1_000,
             "current_price": 100, "current_value": 3_000},
            {"symbol": "AAPL", "market": "US", "quantity": 10,
             "current_price": 200, "current_value": 2_000},
        ],
    }
    target = {
        "converged": True,
        "positions": [_target("2330", .8, 100)],
        "quality": {"flags": []},
        "risk_comparison": {"current_weight_coverage": 1},
    }
    with patch(
        "services.tw_factor_rebalance_service.get_portfolio_detail",
        AsyncMock(return_value=detail),
    ) as detail_mock, patch(
        "services.tw_factor_rebalance_service.construct_factor_portfolio",
        AsyncMock(return_value=target),
    ) as target_mock:
        result = await build_factor_rebalance_preview(
            portfolio_id="p1", user_id="owner-1", db=None, ranking={"candidates": []},
            as_of=__import__("datetime").date(2026, 7, 15), additional_cash_twd=100_000,
            min_trade_pct=0,
        )
    detail_mock.assert_awaited_once_with("p1", "owner-1", None)
    assert target_mock.await_args.kwargs["portfolio_notional_twd"] == 200_000
    assert target_mock.await_args.kwargs["current_weights"] == {"2330": .5}
    assert result["frozen"][0]["symbol"] == "AAPL"
    assert result["preview_only"] is True
    assert "non_tw_holdings_frozen" in result["quality_flags"]


@pytest.mark.asyncio
async def test_infeasible_target_never_becomes_liquidation_advice():
    detail = {
        "name": "核心", "currency": "TWD", "holdings": [{
            "symbol": "2330", "market": "TW", "quantity": 1_000,
            "current_price": 100, "current_value": 100_000,
        }],
    }
    target = {
        "converged": False, "solver_message": "infeasible", "positions": [],
        "quality": {"flags": ["portfolio_optimizer_infeasible"]},
        "risk_comparison": {"current_weight_coverage": 1},
    }
    with patch(
        "services.tw_factor_rebalance_service.get_portfolio_detail",
        AsyncMock(return_value=detail),
    ), patch(
        "services.tw_factor_rebalance_service.construct_factor_portfolio",
        AsyncMock(return_value=target),
    ):
        result = await build_factor_rebalance_preview(
            portfolio_id="p1", user_id="owner-1", db=None, ranking={},
            as_of=__import__("datetime").date(2026, 7, 15),
        )
    assert result["trades"] == []
    assert result["post_positions"][0]["quantity"] == 1_000
    assert result["summary"]["gross_turnover_twd"] == 0


@pytest.mark.asyncio
async def test_preview_propagates_owner_lookup_failure():
    with patch(
        "services.tw_factor_rebalance_service.get_portfolio_detail",
        AsyncMock(side_effect=ValueError("Portfolio not found")),
    ):
        with pytest.raises(ValueError, match="not found"):
            await build_factor_rebalance_preview(
                portfolio_id="missing", user_id="owner-1", db=None,
                ranking={}, as_of=__import__("datetime").date(2026, 7, 15),
                additional_cash_twd=100_000,
            )


@pytest.mark.asyncio
async def test_preview_uses_actual_twd_ledger_cash_and_freezes_foreign_cash():
    detail = {
        "name": "核心", "currency": "TWD", "cash_balances": {
            "TWD": 25_000, "USD": 1_000,
        },
        "holdings": [{
            "symbol": "2330", "market": "TW", "quantity": 1_000,
            "current_price": 100, "current_value": 100_000,
        }],
    }
    target = {
        "converged": True, "positions": [_target("2330", .8, 100)],
        "quality": {"flags": []},
        "risk_comparison": {"current_weight_coverage": 1},
    }
    with patch(
        "services.tw_factor_rebalance_service.get_portfolio_detail",
        AsyncMock(return_value=detail),
    ), patch(
        "services.tw_factor_rebalance_service.construct_factor_portfolio",
        AsyncMock(return_value=target),
    ) as target_mock:
        result = await build_factor_rebalance_preview(
            portfolio_id="p1", user_id="owner-1", db=None, ranking={},
            as_of=__import__("datetime").date(2026, 7, 15),
            additional_cash_twd=5_000, min_trade_pct=0,
        )
    assert target_mock.await_args.kwargs["portfolio_notional_twd"] == 130_000
    assert result["ledger_cash_twd"] == 25_000
    assert result["additional_cash_twd"] == 5_000
    assert result["summary"]["initial_cash_twd"] == 30_000
    assert "foreign_currency_cash_frozen" in result["quality_flags"]
    assert "hypothetical_additional_cash" in result["quality_flags"]
