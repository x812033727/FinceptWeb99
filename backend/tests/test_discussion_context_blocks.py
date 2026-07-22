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
from datetime import date, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from unittest.mock import AsyncMock, patch

from services.discussion.context import build_market_context
from services.discussion.context.blocks import chip, http, news, owner, risk, technical


def _new_ctx() -> dict:
    """Minimal default ctx — same shape the orchestrator initialises."""
    return {
        "top_gainers": [],
        "top_losers": [],
        "index": None,
        "macro": None,
        "focus_briefs": [],
        "news_sentiment": None,
        "news_backfill": None,
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
        "short_term_signals": {},
        "taifex_positioning": None,
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



# ── technical.fetch_short_term_signals ────────────────────────────


@pytest.mark.asyncio
async def test_technical_block_populates_per_symbol_signals(
    db_session: AsyncSession,
):
    """Seed enough OHLCV history for one focus symbol; assert the block
    populates `ctx['short_term_signals'][symbol]` with the five Tier-1
    metrics. A symbol without enough history must be silently skipped
    (don't blank the block, don't raise)."""
    from services.ingest.repository import OhlcvBar, upsert_ohlcv_bars

    base = date(2026, 3, 1)
    # 30 bars for 2330 = enough for all metrics.
    enough = [
        OhlcvBar(
            market="TW", symbol="2330", ts=base + timedelta(days=i),
            open=600.0 + i, high=601.0 + i, low=599.0 + i,
            close=600.0 + i, volume=1_000_000, source="test",
        )
        for i in range(30)
    ]
    # 5 bars for 2454 = NOT enough; should be skipped.
    too_few = [
        OhlcvBar(
            market="TW", symbol="2454", ts=base + timedelta(days=i),
            open=900.0, high=905.0, low=895.0,
            close=900.0, volume=500_000, source="test",
        )
        for i in range(5)
    ]
    await upsert_ohlcv_bars(db_session, enough + too_few)

    from unittest.mock import AsyncMock, patch

    # Mock the FinMind-facing securities-lending helper so the test
    # doesn't trip the connector → market_key_service import chain.
    with patch(
        "services.derivatives_service.get_securities_lending_trend",
        new=AsyncMock(return_value=None),
    ), patch(
        "services.event_calendar_service.get_upcoming_event",
        new=AsyncMock(return_value=None),
    ):
        ctx = _new_ctx()
        await technical.fetch_short_term_signals(
            ctx, db_session, market="TW",
            focus_symbols=["2330", "2454"],
            as_of=base + timedelta(days=29),
            record_error=_record(ctx),
        )

    assert "2330" in ctx["short_term_signals"]
    assert "2454" not in ctx["short_term_signals"]
    metrics = ctx["short_term_signals"]["2330"]
    assert metrics["close"] == 629.0
    assert metrics["return_5d"] is not None
    assert metrics["rsi_14"] == 100.0
    assert ctx["errors"] == []


@pytest.mark.asyncio
async def test_technical_block_folds_in_day_trading_trend_for_tw(
    db_session: AsyncSession,
):
    """For TW focus symbols, the per-symbol day-trading trend (5-day
    rising/falling/stable) must land under
    `signals['day_trading_trend']` alongside the other technical
    metrics. Non-TW markets fold the field to None."""
    from services.ingest.repository import (
        DayTradingRow, OhlcvBar, upsert_day_trading, upsert_ohlcv_bars,
    )

    base = date(2026, 3, 1)
    # 30 OHLCV bars so compute_short_term_signals returns a dict.
    await upsert_ohlcv_bars(db_session, [
        OhlcvBar(
            market="TW", symbol="2330", ts=base + timedelta(days=i),
            open=600.0 + i, high=601.0 + i, low=599.0 + i,
            close=600.0 + i, volume=1_000_000, source="test",
        )
        for i in range(30)
    ])
    # 5 day-trading sessions — flat 0.50 ratio → stable trend.
    await upsert_day_trading(db_session, [
        DayTradingRow(
            "TW", "2330", base + timedelta(days=25 + i),
            10000, 5000, 5000, "finmind",
        )
        for i in range(5)
    ])

    from unittest.mock import AsyncMock, patch

    with patch(
        "services.derivatives_service.get_securities_lending_trend",
        new=AsyncMock(return_value=None),
    ), patch(
        "services.event_calendar_service.get_upcoming_event",
        new=AsyncMock(return_value=None),
    ):
        ctx = _new_ctx()
        await technical.fetch_short_term_signals(
            ctx, db_session, market="TW",
            focus_symbols=["2330"],
            as_of=base + timedelta(days=29),
            record_error=_record(ctx),
        )

    sig = ctx["short_term_signals"]["2330"]
    assert sig["day_trading_trend"] is not None
    assert sig["day_trading_trend"]["latest_ratio"] == 0.5
    assert sig["day_trading_trend"]["trend"] == "stable"
    assert ctx["errors"] == []


@pytest.mark.asyncio
async def test_technical_block_folds_in_securities_lending_trend_for_tw(
    db_session: AsyncSession,
):
    """For TW focus symbols, the per-symbol securities-lending
    (借券) 5-day trend must land under
    `signals['securities_lending_trend']`. Mocked the FinMind-
    facing helper directly so the test stays offline."""
    from unittest.mock import AsyncMock, patch

    from services.ingest.repository import OhlcvBar, upsert_ohlcv_bars

    base = date(2026, 3, 1)
    await upsert_ohlcv_bars(db_session, [
        OhlcvBar(
            market="TW", symbol="2330", ts=base + timedelta(days=i),
            open=600.0 + i, high=601.0 + i, low=599.0 + i,
            close=600.0 + i, volume=1_000_000, source="test",
        )
        for i in range(30)
    ])

    sbl_payload = {
        "as_of": "2026-03-30", "session_count": 5,
        "latest_balance": 130_000, "balance_change_5d": 30_000,
        "latest_volume": 8_000, "mean_volume_5d": 6_500,
        "trend": "rising",
    }
    with patch(
        "services.derivatives_service.get_securities_lending_trend",
        new=AsyncMock(return_value=sbl_payload),
    ):
        ctx = _new_ctx()
        await technical.fetch_short_term_signals(
            ctx, db_session, market="TW",
            focus_symbols=["2330"],
            as_of=base + timedelta(days=29),
            record_error=_record(ctx),
        )

    sig = ctx["short_term_signals"]["2330"]
    assert sig["securities_lending_trend"] == sbl_payload
    assert ctx["errors"] == []


@pytest.mark.asyncio
async def test_technical_block_folds_in_upcoming_event(
    db_session: AsyncSession,
):
    """`upcoming_event` populated by `event_calendar_service` must
    land under `signals['upcoming_event']` for any market (yfinance
    covers TW + US). When None (no events in 14d window), the field
    is still present so personas can branch on `is None` cleanly."""
    from unittest.mock import AsyncMock, patch

    from services.ingest.repository import OhlcvBar, upsert_ohlcv_bars

    base = date(2026, 3, 1)
    await upsert_ohlcv_bars(db_session, [
        OhlcvBar(
            market="TW", symbol="2330", ts=base + timedelta(days=i),
            open=600.0 + i, high=601.0 + i, low=599.0 + i,
            close=600.0 + i, volume=1_000_000, source="test",
        )
        for i in range(30)
    ])

    event_payload = {
        "as_of": "2026-03-30",
        "earnings_date": "2026-04-04",
        "earnings_in_days": 5,
        "ex_dividend_date": None,
        "ex_dividend_in_days": None,
        "next_event": "earnings",
        "next_event_in_days": 5,
    }
    with patch(
        "services.derivatives_service.get_securities_lending_trend",
        new=AsyncMock(return_value=None),
    ), patch(
        "services.event_calendar_service.get_upcoming_event",
        new=AsyncMock(return_value=event_payload),
    ):
        ctx = _new_ctx()
        await technical.fetch_short_term_signals(
            ctx, db_session, market="TW",
            focus_symbols=["2330"],
            as_of=base + timedelta(days=29),
            record_error=_record(ctx),
        )

    sig = ctx["short_term_signals"]["2330"]
    assert sig["upcoming_event"] == event_payload
    assert sig["upcoming_event"]["next_event"] == "earnings"
    assert ctx["errors"] == []


@pytest.mark.asyncio
async def test_technical_block_noop_when_focus_symbols_empty(
    db_session: AsyncSession,
):
    """Empty `focus_symbols` → block returns immediately without any
    DB hit. Verified via the empty result + no error capture."""
    ctx = _new_ctx()
    await technical.fetch_short_term_signals(
        ctx, db_session, market="TW",
        focus_symbols=[],
        as_of=None,
        record_error=_record(ctx),
    )
    assert ctx["short_term_signals"] == {}
    assert ctx["errors"] == []


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
async def test_news_backfill_result_surfaces_into_ctx(
    db_session: AsyncSession,
):
    """PR #216: when auto-backfill runs in backtest mode, its result
    must land in `ctx['news_backfill']` so the user can diagnose why
    news is empty (paywall? missing token? archive truly thin?)
    without digging into ctx['errors']. Live mode (no `as_of_dt`)
    leaves the field at its default None."""
    from datetime import datetime, timezone
    ctx = _new_ctx()
    asof_dt = datetime(2025, 1, 15, 23, 59, tzinfo=timezone.utc)

    fake_result = {"covered": False, "backfilled": 0}
    with patch(
        "services.news_backfill_service.ensure_news_archive_covers",
        new=AsyncMock(return_value=fake_result),
    ), patch(
        "services.news_sentiment_service.read_recent_market_sentiment",
        new=AsyncMock(return_value={"bullish": 0, "headlines": []}),
    ):
        await news.fetch_market_sentiment(
            ctx, db_session, market="TW", as_of_dt=asof_dt,
            record_error=_record(ctx),
        )

    assert ctx["news_backfill"] == fake_result


@pytest.mark.asyncio
async def test_news_backfill_skipped_in_live_mode(
    db_session: AsyncSession,
):
    """Live mode (`as_of_dt=None`) must not invoke the auto-backfill
    helper — it's specifically a backtest-mode crutch. The
    `news_backfill` field stays at its default (None)."""
    ctx = _new_ctx()

    backfill_mock = AsyncMock()
    with patch(
        "services.news_backfill_service.ensure_news_archive_covers",
        new=backfill_mock,
    ), patch(
        "services.news_sentiment_service.read_recent_market_sentiment",
        new=AsyncMock(return_value={"bullish": 0, "bearish": 0,
                                     "neutral": 0, "headlines": []}),
    ):
        await news.fetch_market_sentiment(
            ctx, db_session, market="TW", as_of_dt=None,
            record_error=_record(ctx),
        )

    backfill_mock.assert_not_awaited()
    assert ctx["news_backfill"] is None


@pytest.mark.asyncio
async def test_news_backfill_exception_recorded_into_ctx(
    db_session: AsyncSession,
):
    """If `ensure_news_archive_covers` raises (very rare — it normally
    catches everything and returns a dict), the wrapper must still
    populate `ctx['news_backfill']` with the error message so the
    user has actionable diagnostic info."""
    from datetime import datetime, timezone
    ctx = _new_ctx()
    asof_dt = datetime(2025, 1, 15, 23, 59, tzinfo=timezone.utc)

    with patch(
        "services.news_backfill_service.ensure_news_archive_covers",
        new=AsyncMock(side_effect=RuntimeError("DB connection lost")),
    ), patch(
        "services.news_sentiment_service.read_recent_market_sentiment",
        new=AsyncMock(return_value={"bullish": 0, "headlines": []}),
    ):
        await news.fetch_market_sentiment(
            ctx, db_session, market="TW", as_of_dt=asof_dt,
            record_error=_record(ctx),
        )

    assert ctx["news_backfill"]["covered"] is False
    assert "DB connection lost" in ctx["news_backfill"]["error"]


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
    """All default keys must be present even when every block
    returns nothing — the prompt template assumes a stable shape.
    Updated through PRs #269 (overseas_indicators), #282
    (single_stock_futures_oi), #283 (taiwan_vix), #284
    (upcoming_events_calendar), #285 (broker_concentration)."""
    expected_keys = {
        "market", "captured_at",
        "captured_session",          # PR for expert-quote freshness
        "backtest", "as_of",
        "info_cutoff",               # backtest look-ahead guard (prev trading day)
        "data_gaps",                 # blocks with no data this session
        "top_gainers", "top_losers", "index",
        "news_sentiment", "news_backfill", "per_symbol_news_sentiment",
        "short_term_signals",
        "focus_briefs", "macro", "user_context",
        "prior_discussions", "international_sentiment",
        "top_foreign_buyers", "margin_balance_trend",
        "top_revenue_growers", "active_buybacks",
        "govt_bank_flow_5d", "risk_warnings",
        "market_institutional_5d", "taifex_positioning",
        "single_stock_futures_oi",   # PR #282
        "taiwan_vix",                # PR #283
        "upcoming_events_calendar",  # PR #284
        "broker_concentration",      # PR #285
        "overseas_indicators",       # PR #269
        "recent_lessons",            # learning loop
        # (G7-1) corporate_announcements removed — dead weight, no
        # persona profile ever consumed it.
        "errors",
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
    # Every tracked block is empty in this stubbed build, so the gap
    # list must name them rather than leaving the personas to guess
    # why the blocks aren't there (which is when they invent numbers).
    assert "taiwan_vix" in ctx["data_gaps"]
    # `captured_session` must be a fully-populated dict in live mode —
    # downstream blocks (focus_briefs.quote, screener rows) read its
    # `session_date` to stamp per-row `as_of_session` consistently.
    # If it ever defaults to None / partial, personas re-anchor on
    # `captured_at` and the original "yesterday's close looks like
    # today's" bug regresses silently.
    sess = ctx["captured_session"]
    assert isinstance(sess, dict)
    assert set(sess.keys()) == {
        "session_date", "phase", "is_intraday", "hint_zh",
    }
    assert sess["hint_zh"]
    assert isinstance(sess["is_intraday"], bool)


@pytest.mark.asyncio
async def test_build_market_context_invokes_progress_callback_at_milestones(
    db_session: AsyncSession,
):
    """`progress_cb` (PR #252) lets the SSE flow surface
    ctx-gathering progress to the user instead of going silent for
    15-30 s during inline news scoring. Pin the milestones the
    callback should fire — drift here regresses the user-visible
    preparing card's stage descriptors.

    Expected sequence:
      1. `fetching_market_data` — before the asyncio.gather block
      2. `scoring_news_sentiment` — before the slow news block
      3. `ctx_ready` — after every block finishes

    All HTTP-bound + DB-bound block fetchers are patched at the
    `services.discussion.context.blocks` layer (the level closest
    to the builder) so the test doesn't transitively import the
    cryptography / pandas / yfinance heavy deps.
    """
    from unittest.mock import AsyncMock, patch

    stages: list[str] = []

    async def _cb(stage: str) -> None:
        stages.append(stage)

    with patch(
        "services.discussion.context.blocks.http.fetch_screener",
        new=AsyncMock(),
    ), patch(
        "services.discussion.context.blocks.http.fetch_index",
        new=AsyncMock(),
    ), patch(
        "services.discussion.context.blocks.http.fetch_macro",
        new=AsyncMock(),
    ), patch(
        "services.discussion.context.blocks.http.fetch_focus_briefs",
        new=AsyncMock(),
    ), patch(
        "services.discussion.context.blocks.technical.fetch_short_term_signals",
        new=AsyncMock(),
    ), patch(
        "services.discussion.context.blocks.derivatives.fetch_taifex_positioning",
        new=AsyncMock(),
    ), patch(
        "services.discussion.context.blocks.chip.fetch_top_foreign_buyers",
        new=AsyncMock(),
    ), patch(
        "services.discussion.context.blocks.chip.fetch_margin_balance_trend",
        new=AsyncMock(),
    ), patch(
        "services.discussion.context.blocks.chip.fetch_top_revenue_growers",
        new=AsyncMock(),
    ), patch(
        "services.discussion.context.blocks.chip.fetch_active_buybacks",
        new=AsyncMock(),
    ), patch(
        "services.discussion.context.blocks.chip.fetch_govt_bank_flow",
        new=AsyncMock(),
    ), patch(
        "services.discussion.context.blocks.chip.fetch_market_institutional",
        new=AsyncMock(),
    ), patch(
        "services.discussion.context.blocks.risk.fetch_risk_warnings",
        new=AsyncMock(),
    ), patch(
        "services.discussion.context.blocks.news.fetch_market_sentiment",
        new=AsyncMock(),
    ), patch(
        "services.discussion.context.blocks.news.fetch_international_sentiment",
        new=AsyncMock(),
    ), patch(
        "services.discussion.context.blocks.news.fetch_per_symbol_sentiment",
        new=AsyncMock(),
    ):
        await build_market_context(
            db_session, market="TW", progress_cb=_cb,
        )

    assert "fetching_market_data" in stages
    assert "scoring_news_sentiment" in stages
    assert "ctx_ready" in stages
    # Order matters — preparing card transitions through the stages
    # in this sequence, so a UI tester relying on the order won't
    # see weird transitions.
    assert stages.index("fetching_market_data") < stages.index(
        "scoring_news_sentiment"
    )
    assert stages.index("scoring_news_sentiment") < stages.index("ctx_ready")


@pytest.mark.asyncio
async def test_per_symbol_sentiment_emits_progress_counter_per_symbol(
    db_session: AsyncSession,
):
    """C1-3 from `misty-mixing-harbor.md`: each per-symbol
    read fires `progress_cb("scoring_news_sentiment", done=k, total=N)`
    so the preparing card can show `"Scoring news sentiment 3/5"`
    during the longest single sub-block window. Older frontends that
    only read `stage` keep working — the parent milestone label is
    preserved verbatim.
    """
    from unittest.mock import AsyncMock, patch

    events: list[tuple[str, int | None, int | None]] = []

    async def _cb(
        stage: str, *, done: int | None = None, total: int | None = None,
    ) -> None:
        events.append((stage, done, total))

    ctx = _new_ctx()
    focus = ["2330", "2454", "2412"]

    with patch(
        "services.news_sentiment_service.read_symbol_sentiment",
        new=AsyncMock(return_value={"bullish": 1, "bearish": 0,
                                     "neutral": 0, "headlines": []}),
    ):
        await news.fetch_per_symbol_sentiment(
            ctx, db_session,
            market="TW",
            focus_symbols=focus,
            as_of_dt=None,
            record_error=_record(ctx),
            max_focus_symbols=5,
            progress_cb=_cb,
        )

    # One event per symbol, all with the same stage label and the
    # `done` field tracking cumulative progress against `total=3`.
    assert len(events) == 3
    for stage, _done, total in events:
        assert stage == "scoring_news_sentiment"
        assert total == 3
    # asyncio.gather doesn't guarantee completion order across the
    # parametrized reads, but the cumulative count must reach the
    # total exactly once at the end.
    done_values = sorted(d for _, d, _ in events)
    assert done_values == [1, 2, 3]


@pytest.mark.asyncio
async def test_per_symbol_sentiment_progress_cb_failure_does_not_blank_ctx(
    db_session: AsyncSession,
):
    """An SSE pipe failure in the progress callback shouldn't blank
    the ctx — the round still has useful data even without the
    progress event. Per-symbol reads must continue and persist
    their results."""
    from unittest.mock import AsyncMock, patch

    async def _broken_cb(
        stage: str, *, done: int | None = None, total: int | None = None,
    ) -> None:
        raise RuntimeError("sse pipe closed")

    ctx = _new_ctx()
    focus = ["2330"]

    with patch(
        "services.news_sentiment_service.read_symbol_sentiment",
        new=AsyncMock(return_value={"bullish": 1, "bearish": 0,
                                     "neutral": 0, "headlines": []}),
    ):
        await news.fetch_per_symbol_sentiment(
            ctx, db_session,
            market="TW",
            focus_symbols=focus,
            as_of_dt=None,
            record_error=_record(ctx),
            max_focus_symbols=5,
            progress_cb=_broken_cb,
        )

    assert "2330" in ctx["per_symbol_news_sentiment"]


@pytest.mark.asyncio
async def test_build_market_context_progress_cb_optional(
    db_session: AsyncSession,
):
    """Default `progress_cb=None` means callers that don't care
    (auto-run cron, synthesizer-internal calls) don't have to plumb
    one in. Build must complete normally without it."""
    from unittest.mock import AsyncMock, patch

    with patch(
        "services.discussion.context.blocks.http.fetch_screener",
        new=AsyncMock(),
    ), patch(
        "services.discussion.context.blocks.http.fetch_index",
        new=AsyncMock(),
    ), patch(
        "services.discussion.context.blocks.http.fetch_macro",
        new=AsyncMock(),
    ), patch(
        "services.discussion.context.blocks.http.fetch_focus_briefs",
        new=AsyncMock(),
    ), patch(
        "services.discussion.context.blocks.technical.fetch_short_term_signals",
        new=AsyncMock(),
    ), patch(
        "services.discussion.context.blocks.derivatives.fetch_taifex_positioning",
        new=AsyncMock(),
    ), patch(
        "services.discussion.context.blocks.chip.fetch_top_foreign_buyers",
        new=AsyncMock(),
    ), patch(
        "services.discussion.context.blocks.chip.fetch_margin_balance_trend",
        new=AsyncMock(),
    ), patch(
        "services.discussion.context.blocks.chip.fetch_top_revenue_growers",
        new=AsyncMock(),
    ), patch(
        "services.discussion.context.blocks.chip.fetch_active_buybacks",
        new=AsyncMock(),
    ), patch(
        "services.discussion.context.blocks.chip.fetch_govt_bank_flow",
        new=AsyncMock(),
    ), patch(
        "services.discussion.context.blocks.chip.fetch_market_institutional",
        new=AsyncMock(),
    ), patch(
        "services.discussion.context.blocks.risk.fetch_risk_warnings",
        new=AsyncMock(),
    ), patch(
        "services.discussion.context.blocks.news.fetch_market_sentiment",
        new=AsyncMock(),
    ), patch(
        "services.discussion.context.blocks.news.fetch_international_sentiment",
        new=AsyncMock(),
    ), patch(
        "services.discussion.context.blocks.news.fetch_per_symbol_sentiment",
        new=AsyncMock(),
    ):
        ctx = await build_market_context(db_session, market="TW")
    assert ctx is not None
    assert "errors" in ctx


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


# ── data_gaps ────────────────────────────────────────────────────


def test_record_data_gaps_names_only_the_empty_blocks():
    """A block with a real reading is not a gap; `False` / `0` are real
    readings, matching what `_minify_for_prompt` keeps."""
    from services.discussion.context.builder import _record_data_gaps

    ctx = {
        "taiwan_vix": None,
        "broker_concentration": {},
        "top_foreign_buyers": [{"symbol": "2330"}],
        "market_institutional_5d": {"trend": "bearish", "net": 0},
        "overseas_indicators": {"as_of": "2026-07-21", "indices": [
            {"symbol": "^SOX"},
        ]},
    }
    _record_data_gaps(ctx)
    assert "taiwan_vix" in ctx["data_gaps"]
    assert "broker_concentration" in ctx["data_gaps"]
    assert "top_foreign_buyers" not in ctx["data_gaps"]
    assert "market_institutional_5d" not in ctx["data_gaps"]
    assert "overseas_indicators" not in ctx["data_gaps"]


def test_record_data_gaps_flags_overseas_envelope_with_no_indices():
    """`overseas_indicators` stays a non-empty `{as_of, indices}` dict
    when the connector fails — the payload is what decides."""
    from services.discussion.context.builder import _record_data_gaps

    ctx = {"overseas_indicators": {"as_of": "2026-07-21", "indices": []}}
    _record_data_gaps(ctx)
    assert "overseas_indicators" in ctx["data_gaps"]


def test_record_data_gaps_reports_present_but_stale_blocks():
    """A block that is there but weeks old is worse than an absent one:
    the panel reads it as current unless told otherwise."""
    from services.discussion.context.builder import _record_data_gaps

    ctx = {
        "captured_session": {"session_date": "2026-07-21"},
        "broker_concentration": {"as_of": "2026-07-07", "symbols": ["2330"]},
        "taifex_positioning": {"as_of": "2026-07-21", "trend": "bearish"},
    }
    _record_data_gaps(ctx)
    assert ctx["data_stale"]["broker_concentration"]["days_behind"] == 14
    assert "taifex_positioning" not in ctx["data_stale"]


def test_record_data_gaps_omits_stale_map_when_everything_is_current():
    from services.discussion.context.builder import _record_data_gaps

    ctx = {
        "captured_session": {"session_date": "2026-07-21"},
        "taifex_positioning": {"as_of": "2026-07-21", "trend": "bearish"},
    }
    _record_data_gaps(ctx)
    assert "data_stale" not in ctx


def test_record_data_gaps_does_not_double_report_empty_blocks():
    """An empty block is a gap, not a staleness — it must not appear in
    both lists."""
    from services.discussion.context.builder import _record_data_gaps

    ctx = {
        "captured_session": {"session_date": "2026-07-21"},
        "broker_concentration": {},
    }
    _record_data_gaps(ctx)
    assert "broker_concentration" in ctx["data_gaps"]
    assert "broker_concentration" not in ctx.get("data_stale", {})
