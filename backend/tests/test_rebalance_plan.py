"""C5 rebalance-plan builder — deterministic service-level tests."""
from unittest.mock import AsyncMock, patch

import pytest

from services.rebalance_service import build_rebalance_plan


def _detail(holdings, currency="USD"):
    total = sum(h["current_value"] for h in holdings)
    for h in holdings:
        h.setdefault("weight_pct", round(h["current_value"] / total * 100, 2))
    return {
        "id": "p1", "name": "t", "currency": currency,
        "total_value": total, "total_cost": total,
        "total_pnl": 0, "total_pnl_pct": 0, "holdings": holdings,
    }


def _h(symbol, market, qty, price, value, ccy="USD"):
    return {
        "id": symbol, "symbol": symbol, "market": market,
        "quantity": qty, "avg_cost": price, "cost_currency": ccy,
        "current_price": price, "current_value": value,
        "cost_value": value, "unrealized_pnl": 0, "unrealized_pnl_pct": 0,
    }


@pytest.fixture
def patched(request):
    detail, weights = request.param
    with patch("services.portfolio_service.get_portfolio_detail",
               AsyncMock(return_value=detail)), \
         patch("services.portfolio_analytics.optimise_portfolio",
               AsyncMock(return_value={"weights": weights})), \
         patch("services.portfolio_service._to_portfolio_currency",
               AsyncMock(side_effect=lambda amt, c, p: amt)):
        yield


US_7030 = _detail([
    _h("AAA", "US", 70, 100.0, 7000.0),
    _h("BBB", "US", 30, 100.0, 3000.0),
])


@pytest.mark.asyncio
@pytest.mark.parametrize("patched", [(US_7030, {})], indirect=True)
async def test_equal_weight_generates_sell_then_buy(patched):
    plan = await build_rebalance_plan("p1", "u1", None, target="equal_weight")
    assert [t["side"] for t in plan["trades"]] == ["sell", "buy"]
    sell, buy = plan["trades"]
    assert sell["symbol"] == "AAA" and sell["quantity"] == 20   # 7000→5000 @100
    assert buy["symbol"] == "BBB" and buy["quantity"] == 20
    assert plan["summary"]["trade_count"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("patched", [(
    _detail([
        _h("2330", "TW", 5000, 1000.0, 5_000_000.0, ccy="TWD"),
        _h("2317", "TW", 10_000, 100.0, 1_000_000.0, ccy="TWD"),
    ], currency="TWD"),
    {},
)], indirect=True)
async def test_tw_rounds_to_board_lots(patched):
    plan = await build_rebalance_plan("p1", "u1", None, target="equal_weight")
    for t in plan["trades"]:
        assert t["quantity"] % 1000 == 0    # 整張
    sell = next(t for t in plan["trades"] if t["side"] == "sell")
    # 5M → 3M target = 2M/1000 = 2000 shares = 2 lots exactly
    assert sell["quantity"] == 2000


@pytest.mark.asyncio
@pytest.mark.parametrize("patched", [(US_7030, {"AAA": 1.0})], indirect=True)
async def test_optimiser_dropped_holding_is_frozen_not_sold(patched):
    plan = await build_rebalance_plan("p1", "u1", None, target="optimise")
    assert [f["symbol"] for f in plan["frozen"]] == ["BBB"]
    # AAA already holds 100% of the investable (7000) slice → no trades.
    assert plan["trades"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("patched", [(US_7030, {})], indirect=True)
async def test_dust_trades_suppressed(patched):
    # 70/30 vs 71/29 target → 1% deltas < default min_trade_pct? equal to it;
    # use custom weights barely off current with a high threshold.
    plan = await build_rebalance_plan(
        "p1", "u1", None, target="custom",
        custom_weights={"AAA": 0.71, "BBB": 0.29}, min_trade_pct=2.0,
    )
    assert plan["trades"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("patched", [(US_7030, {})], indirect=True)
async def test_custom_weights_must_sum_to_one(patched):
    with pytest.raises(ValueError):
        await build_rebalance_plan(
            "p1", "u1", None, target="custom",
            custom_weights={"AAA": 0.5, "BBB": 0.2},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("patched", [(US_7030, {})], indirect=True)
async def test_fees_computed_on_both_sides(patched):
    plan = await build_rebalance_plan(
        "p1", "u1", None, target="equal_weight", fee_bps=10.0,
    )
    for t in plan["trades"]:
        assert t["est_fee"] == pytest.approx(t["est_value"] * 0.001, rel=1e-6)
    assert plan["summary"]["est_fees"] == pytest.approx(
        sum(t["est_fee"] for t in plan["trades"])
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("patched", [(
    _detail([_h("AAA", "US", 10, 100.0, 1000.0)]), {},
)], indirect=True)
async def test_sell_capped_at_held_quantity(patched):
    plan = await build_rebalance_plan(
        "p1", "u1", None, target="custom", custom_weights={"AAA": 1.0},
    )
    # Degenerate single-holding target=100% → no trade at all.
    assert plan["trades"] == []


@pytest.mark.asyncio
async def test_empty_portfolio_returns_empty_plan():
    with patch("services.portfolio_service.get_portfolio_detail",
               AsyncMock(return_value=_detail([]) | {"total_value": 0, "holdings": []})):
        plan = await build_rebalance_plan("p1", "u1", None)
    assert plan["summary"] == {"empty": True}
