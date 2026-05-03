"""Unit tests for `services.post_mortem_service`.

Pure-compute path is exercised against an in-memory `ohlcv_daily`
table; no FinMind / yfinance contact. The LLM-prose formatting is
asserted against fixed strings so regressions in the persona-facing
copy surface loudly (it's the operator's primary lever for shaping
how seriously personas take the self-critique).
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.ohlcv_daily import OhlcvDaily
from services import post_mortem_service as svc


def _bar(symbol: str, ts: date, close: float) -> OhlcvDaily:
    return OhlcvDaily(
        market="TW", symbol=symbol, ts=ts,
        open=close, high=close, low=close, close=close,
        volume=1_000_000, source="test",
    )


# ── _resolve_next_trading_day ─────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_next_trading_day_skips_weekend_naturally(
    db_session: AsyncSession,
):
    """Friday → Monday because there's no Saturday/Sunday data in
    `ohlcv_daily`. Don't need a holiday calendar — the table itself
    is the source of truth for trading days."""
    db_session.add(_bar("2330", date(2026, 3, 20), 600.0))   # Fri
    # No Sat/Sun rows
    db_session.add(_bar("2330", date(2026, 3, 23), 615.0))   # Mon
    await db_session.commit()

    out = await svc._resolve_next_trading_day(
        db_session, market="TW", as_of=date(2026, 3, 20),
    )
    assert out == date(2026, 3, 23)


@pytest.mark.asyncio
async def test_resolve_next_trading_day_returns_none_when_no_future_data(
    db_session: AsyncSession,
):
    """Live mode (as_of=today) or near-future date the archive doesn't
    cover yet — must return None so the API surfaces 'no data'."""
    db_session.add(_bar("2330", date(2026, 3, 23), 600.0))
    await db_session.commit()

    out = await svc._resolve_next_trading_day(
        db_session, market="TW", as_of=date(2026, 3, 23),
    )
    assert out is None


@pytest.mark.asyncio
async def test_resolve_next_trading_day_caps_lookahead(
    db_session: AsyncSession,
):
    """Don't search forever for the next bar — if the archive has a
    huge gap (e.g. someone's dev sandbox missing 2 weeks of data),
    return None rather than walking forward indefinitely."""
    db_session.add(_bar("2330", date(2026, 3, 23), 600.0))
    db_session.add(_bar("2330", date(2026, 4, 30), 700.0))   # > 14 days later
    await db_session.commit()

    out = await svc._resolve_next_trading_day(
        db_session, market="TW", as_of=date(2026, 3, 23),
    )
    assert out is None


# ── compute_top_gainers_after ─────────────────────────────────────


@pytest.mark.asyncio
async def test_compute_top_gainers_returns_correct_ranking(
    db_session: AsyncSession,
):
    """Three symbols, three different change pcts → ranked desc."""
    base = date(2026, 3, 23)
    nxt = date(2026, 3, 24)
    db_session.add_all([
        _bar("2330", base, 100.0),
        _bar("2330", nxt,  108.0),     # +8 %
        _bar("2454", base, 200.0),
        _bar("2454", nxt,  204.0),     # +2 %
        _bar("6505", base, 50.0),
        _bar("6505", nxt,  55.0),      # +10 %
    ])
    await db_session.commit()

    next_day, gainers = await svc.compute_top_gainers_after(
        db_session, as_of=base, market="TW", n=3,
    )
    assert next_day == nxt
    assert [g.symbol for g in gainers] == ["6505", "2330", "2454"]
    assert gainers[0].change_pct == pytest.approx(10.0, abs=0.01)
    assert gainers[1].change_pct == pytest.approx(8.0, abs=0.01)
    assert gainers[2].change_pct == pytest.approx(2.0, abs=0.01)


@pytest.mark.asyncio
async def test_compute_top_gainers_excludes_synthetic_index_symbols(
    db_session: AsyncSession,
):
    """`_TAIEX` / `_TAIEX_TR` are synthetic index rows. Personas
    care about TRADEABLE equities only — index always tops the
    leaderboard if its return is positive but isn't actionable."""
    base = date(2026, 3, 23)
    nxt = date(2026, 3, 24)
    db_session.add_all([
        _bar("_TAIEX", base, 17000.0),
        _bar("_TAIEX", nxt,  17850.0),    # +5 % — would otherwise rank
        _bar("2330", base, 100.0),
        _bar("2330", nxt,  102.0),         # +2 %
    ])
    await db_session.commit()

    _, gainers = await svc.compute_top_gainers_after(
        db_session, as_of=base, market="TW", n=5,
    )
    assert [g.symbol for g in gainers] == ["2330"]


@pytest.mark.asyncio
async def test_compute_top_gainers_drops_zero_baseline_symbols(
    db_session: AsyncSession,
):
    """Division-by-zero protection: a symbol with base_close=0 (data
    glitch / malformed insert) must NOT be returned at infinite
    change %."""
    base = date(2026, 3, 23)
    nxt = date(2026, 3, 24)
    db_session.add_all([
        _bar("BAD", base, 0.0),
        _bar("BAD", nxt,  10.0),
        _bar("2330", base, 100.0),
        _bar("2330", nxt,  102.0),
    ])
    await db_session.commit()

    _, gainers = await svc.compute_top_gainers_after(
        db_session, as_of=base, market="TW", n=5,
    )
    syms = [g.symbol for g in gainers]
    assert "BAD" not in syms
    assert "2330" in syms


@pytest.mark.asyncio
async def test_compute_top_gainers_returns_empty_when_no_next_day(
    db_session: AsyncSession,
):
    """as_of equals the latest archive date → no next_trading_day →
    empty result."""
    base = date(2026, 3, 23)
    db_session.add(_bar("2330", base, 100.0))
    await db_session.commit()

    next_day, gainers = await svc.compute_top_gainers_after(
        db_session, as_of=base, market="TW", n=5,
    )
    assert next_day is None
    assert gainers == []


# ── format_post_mortem_prompt ─────────────────────────────────────


def test_format_prompt_carries_all_four_review_questions():
    """Pin the four-question review structure. Drift here would
    silently regress the self-critique quality — personas are very
    sensitive to prompt structure."""
    g1 = svc.GainerRow(
        symbol="6505", change_pct=9.83, close=110.0, base_close=100.16,
        next_trading_day=date(2026, 3, 24),
    )
    g2 = svc.GainerRow(
        symbol="2330", change_pct=4.81, close=1015.0, base_close=968.4,
        next_trading_day=date(2026, 3, 24),
    )
    text = svc.format_post_mortem_prompt(
        as_of=date(2026, 3, 23),
        next_trading_day=date(2026, 3, 24),
        top_gainers=[g1, g2],
        recommended_symbols=["2330", "2454"],
    )
    # Header + dates
    assert "事後檢討" in text
    assert "2026-03-23" in text
    assert "2026-03-24" in text
    # Both gainers + their numbers
    assert "6505" in text
    assert "+9.83%" in text
    assert "2330" in text
    assert "+4.81%" in text
    # All four review questions
    assert "命中與否" in text
    assert "漏掉的標的" in text
    assert "誤判方向的訊號" in text
    assert "缺失的資料" in text
    # Recap of what they recommended
    assert "2330, 2454" in text or "2454, 2330" in text


def test_format_prompt_handles_empty_recommended_symbols():
    """Conclusion sometimes has no `recommended_symbols` (synthesizer
    couldn't extract any). Prompt must still render — just adapt the
    recap line."""
    g = svc.GainerRow(
        symbol="6505", change_pct=9.83, close=110.0, base_close=100.16,
        next_trading_day=date(2026, 3, 24),
    )
    text = svc.format_post_mortem_prompt(
        as_of=date(2026, 3, 23),
        next_trading_day=date(2026, 3, 24),
        top_gainers=[g],
        recommended_symbols=[],
    )
    assert "沒有推薦任何具體標的" in text


def test_format_prompt_uses_company_name_when_available():
    """zh-TW operators recognise 「6505 (台塑石化)」 instantly; bare
    code 「6505」 is harder to parse. Use the in-memory map's name
    when present, fall through cleanly when not."""
    g1 = svc.GainerRow(
        symbol="6505", change_pct=9.83, close=110.0, base_close=100.16,
        next_trading_day=date(2026, 3, 24),
    )
    g2 = svc.GainerRow(
        symbol="9999", change_pct=5.0, close=50.0, base_close=47.6,
        next_trading_day=date(2026, 3, 24),
    )
    text = svc.format_post_mortem_prompt(
        as_of=date(2026, 3, 23),
        next_trading_day=date(2026, 3, 24),
        top_gainers=[g1, g2],
        recommended_symbols=[],
        company_name_lookup={"6505": "台塑石化", "9999": None},
    )
    assert "6505 (台塑石化)" in text
    # 9999 has no name → just the code
    assert "9999" in text
    assert "9999 ()" not in text
