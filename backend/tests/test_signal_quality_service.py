"""Unit + integration tests for `services.signal_quality_service`.

Pure helpers (path extraction, D5 computation, Pearson) are tested
directly. The DB-bound `compute_signal_quality` runs against an
in-memory seeded discussion + round_context to verify the end-to-end
aggregation produces the right numbers.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.discussion import Discussion
from models.discussion_round_context import DiscussionRoundContext
from models.user import User, UserRole
from services import signal_quality_service as svc


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    user = User(
        email=f"sq-{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x",
        role=UserRole.analyst,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


# ── _extract_signal_value ─────────────────────────────────────────


def test_extract_signal_resolves_symbol_placeholder():
    ctx = {
        "short_term_signals": {
            "2330": {"rsi_14": 67.3},
            "2454": {"rsi_14": 45.0},
        },
    }
    assert svc._extract_signal_value(
        ctx, "short_term_signals.{symbol}.rsi_14", "2330",
    ) == 67.3
    assert svc._extract_signal_value(
        ctx, "short_term_signals.{symbol}.rsi_14", "2454",
    ) == 45.0


def test_extract_signal_returns_none_for_missing_intermediate():
    """The signal might be None at any level (block missing, symbol
    not covered, sub-key not present). Caller treats all variants
    identically — None means "no data, skip this pair"."""
    ctx = {"short_term_signals": {"2330": {"rsi_14": None}}}
    assert svc._extract_signal_value(
        ctx, "short_term_signals.{symbol}.rsi_14", "2330",
    ) is None
    assert svc._extract_signal_value(
        ctx, "short_term_signals.{symbol}.rsi_14", "9999",
    ) is None
    assert svc._extract_signal_value(
        ctx, "short_term_signals.{symbol}.kd_k", "2330",
    ) is None


def test_extract_signal_walks_market_wide_path_without_symbol():
    """Top-level market-wide signals like `taifex_positioning.trend`
    don't use the symbol placeholder — extractor still resolves them
    correctly when no `{symbol}` token appears in the path."""
    ctx = {"taifex_positioning": {"trend": "bearish"}}
    assert svc._extract_signal_value(
        ctx, "taifex_positioning.trend", "any_symbol",
    ) == "bearish"


# ── _d5_return ────────────────────────────────────────────────────


def test_d5_return_basic():
    """day1_open=100, d5_close=105 → return = +5.0."""
    out = svc._d5_return(
        day1_opens={"2330": 100.0},
        daily_closes={"2330": [101.0, 102.0, 103.0, 104.0, 105.0]},
        symbol="2330",
    )
    assert out == pytest.approx(5.0, abs=1e-9)


def test_d5_return_returns_none_when_d5_unresolved():
    """Cron hasn't filled D5 yet → returns None so the pair is
    skipped from the correlation."""
    out = svc._d5_return(
        day1_opens={"2330": 100.0},
        daily_closes={"2330": [101.0, 102.0, None, None, None]},
        symbol="2330",
    )
    assert out is None


def test_d5_return_returns_none_when_open_missing_or_zero():
    """Defensive: zero or missing open would either crash on
    division or produce inf% — both are unsafe."""
    assert svc._d5_return(
        day1_opens={"2330": 0.0},
        daily_closes={"2330": [1, 2, 3, 4, 5]},
        symbol="2330",
    ) is None
    assert svc._d5_return(
        day1_opens={},
        daily_closes={"2330": [1, 2, 3, 4, 5]},
        symbol="2330",
    ) is None


def test_d5_return_returns_none_when_columns_null():
    """Discussion with neither outcome column populated → None.
    The cron writes both atomically so this is the "still pending"
    state."""
    assert svc._d5_return(None, None, "2330") is None


# ── _pearson ──────────────────────────────────────────────────────


def test_pearson_perfectly_correlated_returns_one():
    out = svc._pearson([1.0, 2.0, 3.0, 4.0], [10.0, 20.0, 30.0, 40.0])
    assert out == pytest.approx(1.0, abs=1e-9)


def test_pearson_perfectly_anticorrelated_returns_minus_one():
    out = svc._pearson([1.0, 2.0, 3.0, 4.0], [40.0, 30.0, 20.0, 10.0])
    assert out == pytest.approx(-1.0, abs=1e-9)


def test_pearson_returns_none_below_three_points():
    """Two points always give r=±1 trivially — surface None instead
    so the caller doesn't false-positive on tiny samples."""
    assert svc._pearson([1.0, 2.0], [1.0, 2.0]) is None


def test_pearson_returns_none_when_zero_variance():
    """Constant series → undefined correlation → None."""
    assert svc._pearson([5.0, 5.0, 5.0], [1.0, 2.0, 3.0]) is None
    assert svc._pearson([1.0, 2.0, 3.0], [5.0, 5.0, 5.0]) is None


# ── compute_signal_quality end-to-end ─────────────────────────────


async def _seed_concluded_discussion(
    db_session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    rsi_14: float,
    industry_rs: float,
    taifex_trend: str,
    d5_return_pct: float,
    symbol: str = "2330",
    market: str = "TW",
) -> uuid.UUID:
    """Helper: create one concluded discussion with a single round-1
    context + populated D1-D5 outcomes computed to produce the
    requested D5 return."""
    disc_id = uuid4()
    day1_open = 100.0
    d5_close = day1_open * (1 + d5_return_pct / 100)
    db_session.add(Discussion(
        id=disc_id, owner_id=owner_id,
        topic=f"test {symbol}", rules="", market=market,
        persona_ids=["market_analyst"],
        status="done", current_round=1,
        day1_open_prices={symbol: day1_open},
        daily_close_prices={
            symbol: [
                day1_open * 1.005,
                day1_open * 1.01,
                day1_open * 1.015,
                day1_open * 1.02,
                d5_close,
            ],
        },
    ))
    db_session.add(DiscussionRoundContext(
        discussion_id=disc_id, round=1,
        context={
            "short_term_signals": {
                symbol: {
                    "rsi_14": rsi_14,
                    "industry_rs": {"rs_score": industry_rs},
                },
            },
            "taifex_positioning": {"trend": taifex_trend},
        },
        captured_at=datetime.now(UTC),
    ))
    await db_session.commit()
    return disc_id


@pytest.mark.asyncio
async def test_compute_signal_quality_returns_zero_when_archive_empty(
    db_session: AsyncSession,
):
    out = await svc.compute_signal_quality(db_session, lookback_days=30)
    assert out.discussions_audited == 0
    # Stats arrays are still populated (one entry per signal in the
    # taxonomy) but each entry's n=0 — the UI / CLI can render the
    # full taxonomy with empty bars rather than blank-listing.
    assert all(s.n == 0 for s in out.numeric)
    assert all(s.n_total == 0 for s in out.categorical)


@pytest.mark.asyncio
async def test_compute_signal_quality_aggregates_numeric_correlation(
    db_session: AsyncSession, owner: User,
):
    """Seed three discussions where higher RSI maps to lower D5
    return — should produce a strongly NEGATIVE Pearson r."""
    await _seed_concluded_discussion(
        db_session, owner_id=owner.id,
        rsi_14=80, industry_rs=2.0, taifex_trend="bullish",
        d5_return_pct=-2.0, symbol="2330",
    )
    await _seed_concluded_discussion(
        db_session, owner_id=owner.id,
        rsi_14=50, industry_rs=0.0, taifex_trend="neutral",
        d5_return_pct=0.0, symbol="2454",
    )
    await _seed_concluded_discussion(
        db_session, owner_id=owner.id,
        rsi_14=30, industry_rs=-1.0, taifex_trend="bearish",
        d5_return_pct=3.0, symbol="3034",
    )

    out = await svc.compute_signal_quality(db_session, lookback_days=30)
    assert out.discussions_audited == 3

    rsi = next(s for s in out.numeric if s.path.endswith("rsi_14"))
    assert rsi.n == 3
    assert rsi.pearson_r is not None
    # RSI ↑ paired with D5 ↓ — strongly negative correlation.
    assert rsi.pearson_r < -0.9


@pytest.mark.asyncio
async def test_compute_signal_quality_aggregates_categorical_buckets(
    db_session: AsyncSession, owner: User,
):
    """Three discussions where TAIFEX bullish → up, bearish → down.
    The bullish bucket's mean D5 should EXCEED the bearish bucket's,
    confirming the signal is directional."""
    await _seed_concluded_discussion(
        db_session, owner_id=owner.id,
        rsi_14=50, industry_rs=0, taifex_trend="bullish",
        d5_return_pct=2.0, symbol="2330",
    )
    await _seed_concluded_discussion(
        db_session, owner_id=owner.id,
        rsi_14=50, industry_rs=0, taifex_trend="bullish",
        d5_return_pct=1.0, symbol="2454",
    )
    await _seed_concluded_discussion(
        db_session, owner_id=owner.id,
        rsi_14=50, industry_rs=0, taifex_trend="bearish",
        d5_return_pct=-2.0, symbol="3034",
    )

    out = await svc.compute_signal_quality(db_session, lookback_days=30)
    taifex = next(
        s for s in out.categorical if s.path == "taifex_positioning.trend"
    )
    assert taifex.n_total == 3
    assert "bullish" in taifex.by_category
    assert "bearish" in taifex.by_category
    # Bullish mean > bearish mean — signal works.
    assert (
        taifex.by_category["bullish"].mean_d5_return
        > taifex.by_category["bearish"].mean_d5_return
    )


@pytest.mark.asyncio
async def test_compute_signal_quality_skips_discussions_without_d5_data(
    db_session: AsyncSession, owner: User,
):
    """A discussion concluded but daily_close_prices never populated
    (cron hasn't reached it yet) MUST be excluded — pairing a signal
    value with a missing D5 would silently bias the report."""
    disc_id = uuid4()
    db_session.add(Discussion(
        id=disc_id, owner_id=owner.id,
        topic="not-yet-scored", rules="", market="TW",
        persona_ids=["market_analyst"],
        status="done", current_round=1,
        day1_open_prices=None,
        daily_close_prices=None,
    ))
    db_session.add(DiscussionRoundContext(
        discussion_id=disc_id, round=1,
        context={"short_term_signals": {"2330": {"rsi_14": 67.0}}},
        captured_at=datetime.now(UTC),
    ))
    await db_session.commit()

    out = await svc.compute_signal_quality(db_session, lookback_days=30)
    assert out.discussions_audited == 0
