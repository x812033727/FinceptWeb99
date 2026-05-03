"""Per-block tests for the modular discussion context builder.

These tests demonstrate the value of the Phase 2 refactor: each block
in `services.discussion.context.blocks.*` can be exercised in
isolation by patching just its own dependencies — no need to set up
the full `gather_market_context` environment with all 14 blocks
mocked. Compare with the legacy pattern in `test_discussion_service`
where every backtest test had to patch screener / index / macro /
focus_briefs even when only testing chip metrics.

Coverage strategy:
  - One test per block in `blocks/http.py` confirming the block
    writes to the correct ctx key + propagates `as_of`.
  - One test confirming the orchestrator wires the parallel /
    sequential split correctly.
  - Per-block error isolation: a failing block writes to
    `ctx['errors']` but doesn't break sibling blocks.
"""
from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from unittest.mock import AsyncMock, patch

from services.discussion.context import build_market_context
from services.discussion.context.blocks import chip, http, news, owner, risk


def _new_ctx() -> dict:
    """Minimal default ctx — same shape the orchestrator initialises."""
    return {
        "top_gainers": [],
        "top_losers": [],
        "index": None,
        "macro": None,
        "focus_briefs": [],
        "news_sentiment": None,
        "international_sentiment": None,
        "per_symbol_news_sentiment": {},
        "user_context": None,
        "prior_discussions": [],
        "top_foreign_buyers": [],
        "margin_balance_trend": None,
        "top_revenue_growers": [],
        "active_buybacks": [],
        "govt_bank_flow_5d": [],
        "risk_warnings": {
            "active_dispositions": [],
            "recent_suspensions": [],
            "high_day_trading_ratio": [],
        },
        "market_institutional_5d": [],
        "errors": [],
    }


def _record(ctx: dict):
    def _inner(source: str, exc: Exception) -> None:
        ctx["errors"].append({"source": source, "error": str(exc)})
    return _inner


# ── http.fetch_screener ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_http_fetch_screener_passes_as_of_to_tw_market_service():
    """Block-level test: `fetch_screener` calls `get_screener` with
    the `as_of` kwarg so the underlying service can route to the
    historical path. No need to wire up other 13 blocks."""
    ctx = _new_ctx()
    fake_rows = [
        {"symbol": "2330", "change_pct": 5.5, "price": 600, "volume": 10_000_000},
        {"symbol": "2454", "change_pct": -3.2, "price": 1000, "volume": 5_000_000},
    ]
    asof = date(2025, 1, 15)

    with patch(
        "services.tw_market_service.get_screener",
        new=AsyncMock(return_value=fake_rows),
    ) as mock:
        await http.fetch_screener(
            ctx, market="TW", top_n=5, as_of=asof,
            record_error=_record(ctx),
        )

    assert mock.call_args.kwargs.get("as_of") == asof
    assert ctx["top_gainers"][0]["symbol"] == "2330"
    assert ctx["top_losers"][0]["symbol"] == "2454"


@pytest.mark.asyncio
async def test_http_fetch_screener_records_error_on_upstream_failure():
    """Block-level error isolation: a screener failure writes to
    `ctx['errors']` instead of bubbling. Mirrors what the orchestrator
    expects — sibling blocks must keep running."""
    ctx = _new_ctx()
    with patch(
        "services.tw_market_service.get_screener",
        new=AsyncMock(side_effect=RuntimeError("upstream down")),
    ):
        await http.fetch_screener(
            ctx, market="TW", top_n=5, as_of=None,
            record_error=_record(ctx),
        )

    assert ctx["top_gainers"] == []
    assert ctx["errors"][0]["source"] == "screener"
    assert "upstream down" in ctx["errors"][0]["error"]


# ── http.fetch_macro ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_http_fetch_macro_threads_as_of_to_assemble_macro_block():
    ctx = _new_ctx()
    asof = date(2025, 1, 15)

    with patch(
        "services.discussion_service._assemble_macro_block",
        new=AsyncMock(return_value={"fed_funds_rate": {"summary": {"value": 4.25}}}),
    ) as mock:
        await http.fetch_macro(
            ctx, as_of=asof, record_error=_record(ctx),
        )

    assert mock.call_args.kwargs.get("as_of") == asof
    assert ctx["macro"]["fed_funds_rate"]["summary"]["value"] == 4.25


# ── chip.fetch_top_foreign_buyers ─────────────────────────────────


@pytest.mark.asyncio
async def test_chip_fetch_top_foreign_buyers_filters_by_as_of(
    db_session: AsyncSession,
):
    """Insert rows on two dates, run the block with as_of pointing at
    the older date, assert only the older row surfaces."""
    from services.ingest.repository import (
        InstitutionalDailyRow,
        upsert_institutional_daily,
    )

    backtest_day = date(2025, 1, 15)
    later_day = date(2025, 4, 30)
    await upsert_institutional_daily(db_session, [
        InstitutionalDailyRow(
            market="TW", symbol="2330", ts=backtest_day,
            fini_buy=100_000, fini_sell=20_000,
            sitc_buy=0, sitc_sell=0, dealer_buy=0, dealer_sell=0,
            source="twse",
        ),
        InstitutionalDailyRow(
            market="TW", symbol="2454", ts=later_day,
            fini_buy=999_999, fini_sell=0,
            sitc_buy=0, sitc_sell=0, dealer_buy=0, dealer_sell=0,
            source="twse",
        ),
    ])

    ctx = _new_ctx()
    await chip.fetch_top_foreign_buyers(
        ctx, db_session, as_of=backtest_day,
        record_error=_record(ctx),
    )
    syms = [row["symbol"] for row in ctx["top_foreign_buyers"]]
    assert "2330" in syms
    assert "2454" not in syms


# ── risk.fetch_risk_warnings ──────────────────────────────────────


@pytest.mark.asyncio
async def test_risk_fetch_risk_warnings_returns_three_subblocks(
    db_session: AsyncSession,
):
    """Empty DB still produces the three-key dict — personas read the
    empty lists as "no signal" rather than missing keys."""
    ctx = _new_ctx()
    await risk.fetch_risk_warnings(
        ctx, db_session, as_of=None, record_error=_record(ctx),
    )
    assert set(ctx["risk_warnings"].keys()) == {
        "active_dispositions", "recent_suspensions", "high_day_trading_ratio",
    }


# ── news.fetch_market_sentiment ───────────────────────────────────


@pytest.mark.asyncio
async def test_news_fetch_market_sentiment_drops_block_on_empty_backtest(
    db_session: AsyncSession,
):
    """Backtest mode + empty headlines → block drops to None so the
    LLM reads "we have no data here" instead of "0 bullish/0 bearish/
    0 neutral" (which it would interpret as "market neutral")."""
    from datetime import datetime, timezone
    ctx = _new_ctx()
    asof_dt = datetime(2025, 1, 15, 23, 59, tzinfo=timezone.utc)

    with patch(
        "services.news_sentiment_service.read_recent_market_sentiment",
        new=AsyncMock(return_value={"bullish": 0, "headlines": []}),
    ):
        await news.fetch_market_sentiment(
            ctx, db_session, market="TW", as_of_dt=asof_dt,
            record_error=_record(ctx),
        )

    assert ctx["news_sentiment"] is None


@pytest.mark.asyncio
async def test_news_fetch_market_sentiment_keeps_empty_block_in_live_mode(
    db_session: AsyncSession,
):
    """Live mode (`as_of_dt=None`) preserves the empty-counts dict —
    operators read zero counts as "cron hasn't run yet" rather than
    "no data" (a meaningful operational signal)."""
    ctx = _new_ctx()
    empty = {"bullish": 0, "bearish": 0, "neutral": 0, "headlines": []}

    with patch(
        "services.news_sentiment_service.read_recent_market_sentiment",
        new=AsyncMock(return_value=empty),
    ):
        await news.fetch_market_sentiment(
            ctx, db_session, market="TW", as_of_dt=None,
            record_error=_record(ctx),
        )

    assert ctx["news_sentiment"] == empty


# ── owner.fetch_user_context ──────────────────────────────────────


@pytest.mark.asyncio
async def test_owner_fetch_user_context_records_error_on_failure(
    db_session: AsyncSession,
):
    """A user_context failure must not leak into ctx — orchestrator
    contract is "blank the block, log to errors, keep going"."""
    import uuid as _uuid
    ctx = _new_ctx()

    with patch(
        "services.discussion_service._assemble_user_context",
        new=AsyncMock(side_effect=RuntimeError("portfolio table down")),
    ):
        await owner.fetch_user_context(
            ctx, db_session, owner_id=_uuid.uuid4(),
            focus_symbols=["2330"], record_error=_record(ctx),
        )

    assert ctx["user_context"] is None
    assert ctx["errors"][0]["source"] == "user_context"


# ── builder.build_market_context (end-to-end) ─────────────────────


@pytest.mark.asyncio
async def test_build_market_context_initialises_default_shape(
    db_session: AsyncSession,
):
    """All 18 default keys must be present even when every block
    returns nothing — the prompt template assumes a stable shape."""
    expected_keys = {
        "market", "captured_at", "backtest", "as_of",
        "top_gainers", "top_losers", "index",
        "news_sentiment", "per_symbol_news_sentiment",
        "focus_briefs", "macro", "user_context",
        "prior_discussions", "international_sentiment",
        "top_foreign_buyers", "margin_balance_trend",
        "top_revenue_growers", "active_buybacks",
        "govt_bank_flow_5d", "risk_warnings",
        "market_institutional_5d", "errors",
    }

    with patch(
        "services.tw_market_service.get_screener",
        new=AsyncMock(return_value=[]),
    ), patch(
        "services.tw_market_service.get_index",
        new=AsyncMock(return_value={}),
    ), patch(
        "services.discussion_service._assemble_macro_block",
        new=AsyncMock(return_value={}),
    ), patch(
        "services.discussion_service._assemble_focus_briefs",
        new=AsyncMock(return_value=[]),
    ):
        ctx = await build_market_context(db_session, market="TW")

    assert set(ctx.keys()) == expected_keys
    assert ctx["market"] == "TW"
    assert ctx["backtest"] is False


@pytest.mark.asyncio
async def test_build_market_context_skips_chip_blocks_for_non_tw(
    db_session: AsyncSession,
):
    """TWSE-specific data shouldn't bleed into US discussions. The
    six TW-only chip blocks stay at their default empty values when
    market != TW."""
    with patch(
        "services.us_market_service.get_screener",
        new=AsyncMock(return_value=[]),
    ), patch(
        "services.us_market_service.get_quote",
        new=AsyncMock(return_value={}),
    ), patch(
        "services.discussion_service._assemble_macro_block",
        new=AsyncMock(return_value={}),
    ), patch(
        "services.discussion_service._assemble_focus_briefs",
        new=AsyncMock(return_value=[]),
    ):
        ctx = await build_market_context(db_session, market="US")

    assert ctx["top_foreign_buyers"] == []
    assert ctx["margin_balance_trend"] is None
    assert ctx["top_revenue_growers"] == []
    assert ctx["active_buybacks"] == []
    assert ctx["govt_bank_flow_5d"] == []
    assert ctx["market_institutional_5d"] == []
