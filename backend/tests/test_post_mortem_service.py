"""Unit tests for `services.post_mortem_service`.

Pure-compute path is exercised against an in-memory `ohlcv_daily`
table; no FinMind / yfinance contact. The LLM-prose formatting is
asserted against fixed strings so regressions in the persona-facing
copy surface loudly (it's the operator's primary lever for shaping
how seriously personas take the self-critique).

PR #273 evolved the post-mortem from a single-day "next-day top 5"
view to a 5-trading-day window with two ground-truth surfaces:
recommended self-eval (跟自己比) + per-day top-N leaderboards
(看你錯過了誰). Tests cover both surfaces + the prompt-formatting
contract that ties them together.
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


# ── _resolve_trading_days_after ──────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_trading_days_skips_weekend_naturally(
    db_session: AsyncSession,
):
    """Friday + 5 trading days = following Friday because the table
    has no Sat/Sun rows. No holiday calendar needed — `ohlcv_daily`
    is the source of truth for trading days."""
    db_session.add(_bar("2330", date(2026, 3, 20), 600.0))   # Fri
    # Mon-Fri of the next week
    for i, ts in enumerate(
        [date(2026, 3, 23), date(2026, 3, 24), date(2026, 3, 25),
         date(2026, 3, 26), date(2026, 3, 27)],
    ):
        db_session.add(_bar("2330", ts, 600.0 + i))
    await db_session.commit()

    out = await svc._resolve_trading_days_after(
        db_session, market="TW", as_of=date(2026, 3, 20), days=5,
    )
    assert out == [
        date(2026, 3, 23), date(2026, 3, 24), date(2026, 3, 25),
        date(2026, 3, 26), date(2026, 3, 27),
    ]


@pytest.mark.asyncio
async def test_resolve_trading_days_returns_partial_when_archive_thin(
    db_session: AsyncSession,
):
    """Archive only has 2 days past as_of → return both, not None.
    Caller decides whether 2 days is enough to render the prompt."""
    db_session.add_all([
        _bar("2330", date(2026, 3, 20), 600.0),
        _bar("2330", date(2026, 3, 23), 615.0),
        _bar("2330", date(2026, 3, 24), 620.0),
    ])
    await db_session.commit()

    out = await svc._resolve_trading_days_after(
        db_session, market="TW", as_of=date(2026, 3, 20), days=5,
    )
    assert out == [date(2026, 3, 23), date(2026, 3, 24)]


@pytest.mark.asyncio
async def test_resolve_trading_days_returns_empty_when_no_future_data(
    db_session: AsyncSession,
):
    """Live mode (as_of ≥ latest archive date) returns an empty
    list — caller surfaces 400 to the user."""
    db_session.add(_bar("2330", date(2026, 3, 23), 600.0))
    await db_session.commit()

    out = await svc._resolve_trading_days_after(
        db_session, market="TW", as_of=date(2026, 3, 23), days=5,
    )
    assert out == []


# ── compute_recommended_performance ──────────────────────────────


@pytest.mark.asyncio
async def test_recommended_performance_computes_cumulative_change_per_day(
    db_session: AsyncSession,
):
    """Per-symbol D1-D5 changes are vs the as_of base (NOT vs the
    prior day) — that's the "did the pick deliver vs entry price"
    semantic operators want."""
    base = date(2026, 3, 23)
    days = [date(2026, 3, 24), date(2026, 3, 25), date(2026, 3, 26)]
    db_session.add_all([
        _bar("2330", base, 100.0),
        _bar("2330", days[0], 102.0),    # +2 % vs entry
        _bar("2330", days[1], 105.0),    # +5 % vs entry
        _bar("2330", days[2], 95.0),     # -5 % vs entry
    ])
    await db_session.commit()

    out = await svc.compute_recommended_performance(
        db_session, as_of=base, market="TW",
        recommended_symbols=["2330"], trading_days=days,
    )
    assert len(out) == 1
    row = out[0]
    assert row.symbol == "2330"
    assert row.base_close == 100.0
    assert [d.change_pct for d in row.days] == [2.0, 5.0, -5.0]


@pytest.mark.asyncio
async def test_recommended_performance_skips_symbols_without_base_bar(
    db_session: AsyncSession,
):
    """Symbol that wasn't listed on as_of has no entry price →
    skip silently rather than returning a confused row."""
    base = date(2026, 3, 23)
    days = [date(2026, 3, 24)]
    db_session.add_all([
        _bar("2330", base, 100.0), _bar("2330", days[0], 105.0),
        # 9999 only has a future bar — no base close
        _bar("9999", days[0], 50.0),
    ])
    await db_session.commit()

    out = await svc.compute_recommended_performance(
        db_session, as_of=base, market="TW",
        recommended_symbols=["2330", "9999"], trading_days=days,
    )
    assert {r.symbol for r in out} == {"2330"}


@pytest.mark.asyncio
async def test_recommended_performance_handles_partial_day_data(
    db_session: AsyncSession,
):
    """Symbol has D1 + D3 but missing D2 (e.g. one-day suspension).
    Result includes only the days with data, in order."""
    base = date(2026, 3, 23)
    d1, d2, d3 = date(2026, 3, 24), date(2026, 3, 25), date(2026, 3, 26)
    db_session.add_all([
        _bar("2330", base, 100.0),
        _bar("2330", d1, 102.0),
        # No D2 row
        _bar("2330", d3, 110.0),
    ])
    await db_session.commit()

    out = await svc.compute_recommended_performance(
        db_session, as_of=base, market="TW",
        recommended_symbols=["2330"], trading_days=[d1, d2, d3],
    )
    assert len(out) == 1
    days = out[0].days
    assert [d.trading_day for d in days] == [d1, d3]


@pytest.mark.asyncio
async def test_recommended_performance_returns_empty_for_no_recommendations(
    db_session: AsyncSession,
):
    out = await svc.compute_recommended_performance(
        db_session, as_of=date(2026, 3, 23), market="TW",
        recommended_symbols=[], trading_days=[date(2026, 3, 24)],
    )
    assert out == []


# ── compute_daily_top_gainers ────────────────────────────────────


@pytest.mark.asyncio
async def test_daily_top_gainers_uses_each_days_prior_close_for_change(
    db_session: AsyncSession,
):
    """D2's change % is vs D1's close (not vs as_of). Single-day
    momentum, not cumulative — a stock that flatlined D1 then
    spiked D2 should rank high on D2 even if D1's was unremarkable.
    """
    base = date(2026, 3, 23)
    d1, d2 = date(2026, 3, 24), date(2026, 3, 25)
    db_session.add_all([
        _bar("FLAT", base, 100.0),
        _bar("FLAT", d1, 100.0),    # 0 % vs base
        _bar("FLAT", d2, 110.0),    # +10 % vs D1 — should top D2's board

        _bar("STEADY", base, 200.0),
        _bar("STEADY", d1, 210.0),  # +5 %
        _bar("STEADY", d2, 212.0),  # +0.95 %
    ])
    await db_session.commit()

    out = await svc.compute_daily_top_gainers(
        db_session, market="TW", trading_days=[d1, d2], n=5,
    )
    assert len(out) == 2
    # D1: STEADY +5 % vs base, FLAT 0 %.
    d1_block = out[0]
    assert d1_block.trading_day == d1
    assert d1_block.gainers[0].symbol == "STEADY"
    # D2: FLAT +10 % vs D1, STEADY +0.95 % vs D1.
    d2_block = out[1]
    assert d2_block.trading_day == d2
    assert d2_block.gainers[0].symbol == "FLAT"
    assert d2_block.gainers[0].change_pct == pytest.approx(10.0, abs=0.01)


@pytest.mark.asyncio
async def test_daily_top_gainers_excludes_synthetic_index_symbols(
    db_session: AsyncSession,
):
    """`_TAIEX` / `_TAIEX_TR` would otherwise dominate the boards.
    Personas care about TRADEABLE equities only."""
    base = date(2026, 3, 23)
    d1 = date(2026, 3, 24)
    db_session.add_all([
        _bar("_TAIEX", base, 17000.0),
        _bar("_TAIEX", d1, 17850.0),    # +5 % — would rank
        _bar("2330", base, 100.0),
        _bar("2330", d1, 102.0),         # +2 %
    ])
    await db_session.commit()

    out = await svc.compute_daily_top_gainers(
        db_session, market="TW", trading_days=[d1], n=5,
    )
    syms = [g.symbol for g in out[0].gainers]
    assert "_TAIEX" not in syms
    assert "2330" in syms


@pytest.mark.asyncio
async def test_daily_top_gainers_drops_zero_baseline_symbols(
    db_session: AsyncSession,
):
    """Division-by-zero protection: a symbol with prior_day close=0
    must NOT be returned at infinite change %."""
    base = date(2026, 3, 23)
    d1 = date(2026, 3, 24)
    db_session.add_all([
        _bar("BAD", base, 0.0), _bar("BAD", d1, 10.0),
        _bar("2330", base, 100.0), _bar("2330", d1, 102.0),
    ])
    await db_session.commit()

    out = await svc.compute_daily_top_gainers(
        db_session, market="TW", trading_days=[d1], n=5,
    )
    syms = [g.symbol for g in out[0].gainers]
    assert "BAD" not in syms


@pytest.mark.asyncio
async def test_daily_top_gainers_returns_empty_block_when_no_baseline(
    db_session: AsyncSession,
):
    """If the archive has D1 but no day BEFORE D1, the block surfaces
    with empty gainers (not silently skipped) — caller knows the
    day was attempted but couldn't be measured."""
    d1 = date(2026, 3, 24)
    db_session.add(_bar("2330", d1, 100.0))
    await db_session.commit()

    out = await svc.compute_daily_top_gainers(
        db_session, market="TW", trading_days=[d1], n=5,
    )
    assert len(out) == 1
    assert out[0].trading_day == d1
    assert out[0].gainers == []


# ── format_post_mortem_prompt ────────────────────────────────────


def _sample_payload() -> tuple[
    list[date],
    list[svc.RecommendedPerformance],
    list[svc.DailyGainers],
]:
    days = [date(2026, 3, 24), date(2026, 3, 25), date(2026, 3, 26)]
    rec = [
        svc.RecommendedPerformance(
            symbol="2330", base_close=600.0,
            days=[
                svc.DayPerformance(days[0], 612.0, 2.0),
                svc.DayPerformance(days[1], 618.0, 3.0),
                svc.DayPerformance(days[2], 570.0, -5.0),
            ],
        ),
    ]
    daily = [
        svc.DailyGainers(
            trading_day=days[0],
            gainers=[
                svc.GainerRow("6505", 9.83, 110.0, 100.16, days[0]),
                svc.GainerRow("2330", 2.00, 612.0, 600.0, days[0]),
            ],
        ),
        svc.DailyGainers(
            trading_day=days[1],
            gainers=[
                svc.GainerRow("2603", 7.50, 86.0, 80.0, days[1]),
            ],
        ),
        svc.DailyGainers(trading_day=days[2], gainers=[]),
    ]
    return days, rec, daily


def test_format_prompt_carries_both_sections_and_four_questions():
    """PR #273: prompt must surface (a) recommended self-eval table,
    (b) per-day top-N gainers, (c) the four reflection questions."""
    days, rec, daily = _sample_payload()
    text = svc.format_post_mortem_prompt(
        as_of=date(2026, 3, 23),
        trading_days=days,
        recommended_performance=rec,
        daily_top_gainers=daily,
        recommended_symbols=["2330"],
    )
    # Header + window
    assert "事後檢討" in text
    assert "2026-03-23" in text
    assert "2026-03-24" in text
    assert "2026-03-26" in text
    # Section A markers + recommended self-eval cells
    assert "A. 你的推薦 D1-D5 自評" in text
    assert "2330" in text
    assert "+2.00%" in text
    assert "-5.00%" in text
    # Section B daily winners
    assert "B. 每日漲幅榜首" in text
    assert "6505" in text
    assert "+9.83%" in text
    assert "2603" in text
    # Section C reflection questions (all 4 sub-prompts present)
    assert "自評勝負" in text
    assert "跨日贏家" in text
    assert "訊號權重檢討" in text
    assert "缺失的資料" in text


def test_format_win_prompt_carries_survivor_bias_guards():
    """PR-B3 win prompt must include the explicit survivor-bias
    guards (questions 2 + 3) — without them the LLM tends to spin
    a confident retrospective narrative that overfits the success.
    Three-tier update: prompt now also includes the per-day top-N
    leaderboard (Section B) so even on a clear win the personas
    can never miss the comparison against the actual movers."""
    days, rec, daily = _sample_payload()
    text = svc.format_post_mortem_win_prompt(
        as_of=date(2026, 3, 23),
        trading_days=days,
        recommended_performance=rec,
        daily_top_gainers=daily,
        recommended_symbols=["2330"],
        verdict=svc.OutcomeVerdict(
            status="win", threshold_pct=5.0, window_days=5,
            winners=[svc.WinnerEntry("2330", 8.0, days[1])],
            best_pct=8.0, reason="ok",
        ),
    )
    # Header + window
    assert "事後檢討 — 命中經驗" in text
    assert "2026-03-23" in text
    assert "通過" in text and "5" in text   # 通過 ${threshold}% 門檻
    # Survivor-bias guards
    assert "當時的不確定點" in text
    assert "沒有特別不確定的訊號" in text   # admit-luck escape hatch
    # Category steering
    assert "correct_signal_combo" in text
    assert "successful_thesis" in text
    # Three-tier addition: leaderboard section + missing-signal question
    assert "B. 每日漲幅榜首" in text
    assert "6505" in text                   # gainer from the sample payload
    assert "missing_signal_category" in text


def test_format_prompt_handles_empty_recommendations():
    """Synthesizer sometimes can't extract recommended_symbols.
    Section A surfaces an empty-state line; the rest still renders."""
    days, _, daily = _sample_payload()
    text = svc.format_post_mortem_prompt(
        as_of=date(2026, 3, 23),
        trading_days=days,
        recommended_performance=[],
        daily_top_gainers=daily,
        recommended_symbols=[],
    )
    assert "沒有推薦任何具體標的" in text
    assert "沒有推薦標的可比對" in text


def test_format_prompt_renders_company_names_when_available():
    """zh-TW operators recognise 「6505 (台塑石化)」 instantly. Map
    falls through cleanly for symbols without a name."""
    days, rec, daily = _sample_payload()
    text = svc.format_post_mortem_prompt(
        as_of=date(2026, 3, 23),
        trading_days=days,
        recommended_performance=rec,
        daily_top_gainers=daily,
        recommended_symbols=["2330"],
        company_name_lookup={"2330": "台積電", "6505": "台塑石化"},
    )
    assert "2330 (台積電)" in text
    assert "6505 (台塑石化)" in text


def test_format_prompt_handles_empty_window():
    """Archive doesn't reach D1 yet → trading_days=[] → prompt
    surfaces a clear empty-state header instead of a blank table."""
    text = svc.format_post_mortem_prompt(
        as_of=date(2026, 3, 23),
        trading_days=[],
        recommended_performance=[],
        daily_top_gainers=[],
        recommended_symbols=["2330"],
    )
    assert "ohlcv 尚未抵達" in text


# ── serialisation ────────────────────────────────────────────────


def test_gainer_row_to_dict_carries_trading_day():
    g = svc.GainerRow(
        "2330", 5.0, 105.0, 100.0, date(2026, 3, 24),
    )
    out = svc.gainer_row_to_dict(g)
    assert out["trading_day"] == "2026-03-24"
    assert out["change_pct"] == 5.0


def test_recommended_performance_to_dict_serialises_days():
    r = svc.RecommendedPerformance(
        symbol="2330", base_close=100.0,
        days=[svc.DayPerformance(date(2026, 3, 24), 102.0, 2.0)],
    )
    out = svc.recommended_performance_to_dict(r)
    assert out["symbol"] == "2330"
    assert out["days"][0]["trading_day"] == "2026-03-24"
    assert out["days"][0]["change_pct"] == 2.0


def test_daily_gainers_to_dict_serialises_block():
    block = svc.DailyGainers(
        trading_day=date(2026, 3, 24),
        gainers=[svc.GainerRow("2330", 5.0, 105.0, 100.0, date(2026, 3, 24))],
    )
    out = svc.daily_gainers_to_dict(block)
    assert out["trading_day"] == "2026-03-24"
    assert out["gainers"][0]["symbol"] == "2330"


# ── evaluate_recommendation_outcome ──────────────────────────────


def _perf(symbol: str, days_pct: list[tuple[date, float]]) -> svc.RecommendedPerformance:
    return svc.RecommendedPerformance(
        symbol=symbol, base_close=100.0,
        days=[svc.DayPerformance(d, 100.0 * (1 + p / 100), p) for d, p in days_pct],
    )


_W_DAYS = [
    date(2026, 3, 24), date(2026, 3, 25), date(2026, 3, 26),
    date(2026, 3, 27), date(2026, 3, 28),
]


# 4-band classifier kwargs (used by evaluate_recommendation_outcome).
# Match the production defaults so threshold rendering in prompts is
# realistic.
_BANDS = {"big_win_day5_pct": 20.0, "win_pct": 5.0, "big_loss_pct": -5.0}


def test_evaluate_outcome_marks_win_when_any_symbol_holds_the_threshold():
    """One symbol is still ≥ 5% at its latest resolved close →
    status=win; the other's loss doesn't downgrade the verdict
    (any-symbol semantic)."""
    rec = [
        _perf("2330", [(_W_DAYS[0], 1.0), (_W_DAYS[1], 6.0), (_W_DAYS[2], 6.5)]),
        _perf("2454", [(_W_DAYS[0], 0.5), (_W_DAYS[1], -2.0)]),
    ]
    v = svc.evaluate_recommendation_outcome(
        rec, window_days=5, trading_days=_W_DAYS, **_BANDS,
    )
    assert v.status == "win"
    assert v.best_pct == 6.5
    assert {w.symbol for w in v.winners} == {"2330"}


def test_evaluate_outcome_spike_that_faded_is_not_a_win():
    """Peaked +6% mid-window but gave it back → loss.

    Mirrors the scoreboard: the post-mortem must coach on the outcome
    a reader could realize, not on the best tick of the week.
    """
    rec = [
        _perf("2330", [(_W_DAYS[0], 1.0), (_W_DAYS[1], 6.0), (_W_DAYS[2], 2.0)]),
    ]
    v = svc.evaluate_recommendation_outcome(
        rec, window_days=5, trading_days=_W_DAYS, **_BANDS,
    )
    assert v.status == "loss"


def test_evaluate_outcome_marks_loss_when_all_peaks_below_win_threshold():
    """All recs peak below 5% (and no day ≤ -5%) → status=loss;
    best_pct still surfaced so the operator can see how close it came."""
    rec = [
        _perf("2330", [(_W_DAYS[0], 1.0), (_W_DAYS[1], 1.5)]),
        _perf("2454", [(_W_DAYS[0], -0.5), (_W_DAYS[1], 2.5)]),
    ]
    v = svc.evaluate_recommendation_outcome(
        rec, window_days=5, trading_days=_W_DAYS, **_BANDS,
    )
    assert v.status == "loss"
    assert v.winners == []
    assert v.best_pct == 2.5


def test_evaluate_outcome_marks_insufficient_data_with_empty_window():
    v = svc.evaluate_recommendation_outcome(
        [], window_days=5, trading_days=[], **_BANDS,
    )
    assert v.status == "insufficient_data"
    assert v.best_pct is None


def test_evaluate_outcome_marks_insufficient_data_with_no_recs_having_bars():
    """Trading days resolved but every recommendation lacks bars
    inside the window → can't grade → insufficient_data."""
    v = svc.evaluate_recommendation_outcome(
        [], window_days=5, trading_days=_W_DAYS, **_BANDS,
    )
    assert v.status == "insufficient_data"


def test_evaluate_outcome_window_narrows_horizon():
    """Peak that lands on D4 doesn't count when the runtime window
    is set to 3 days — important for shorter horizon backtests."""
    rec = [_perf("2330", [
        (_W_DAYS[0], 0.5), (_W_DAYS[1], 0.5), (_W_DAYS[2], 1.0),
        (_W_DAYS[3], 6.0),
    ])]
    v = svc.evaluate_recommendation_outcome(
        rec, window_days=3, trading_days=_W_DAYS, **_BANDS,
    )
    assert v.status == "loss"
    assert v.best_pct == 1.0


# ── format_post_mortem_loss_prompt ───────────────────────────────


def test_loss_prompt_omits_self_eval_section_and_includes_threshold_note():
    """The loss branch removes the A self-eval block — personas already
    lost, asking them to re-grade adds noise. The prompt must instead
    surface the win threshold + best_pct so the negative framing is honest.
    """
    days = [_W_DAYS[0], _W_DAYS[1]]
    daily = [svc.DailyGainers(
        trading_day=days[0],
        gainers=[svc.GainerRow("6505", 9.83, 110.0, 100.16, days[0])],
    )]
    verdict = svc.OutcomeVerdict(
        status="loss",
        threshold_pct=5.0, window_days=5, winners=[],
        best_pct=1.5, reason="all peaks below win threshold",
    )
    text = svc.format_post_mortem_loss_prompt(
        as_of=date(2026, 3, 23),
        trading_days=days,
        daily_top_gainers=daily,
        recommended_symbols=["2330"],
        verdict=verdict,
    )
    # Negative framing
    assert "事後檢討 — 漲幅榜複盤" in text
    assert "+1.50%" in text
    assert "未達 5%" in text
    # Section A explicitly removed in the loss branch
    assert "A. 你的推薦 D1-D5 自評" not in text
    # B-equivalent (winners) still present
    assert "6505" in text
    # Reflection prompts focus on "missed", not self-grading
    assert "缺漏的主題" in text or "漏看" in text
    assert "自評勝負" not in text


def test_big_loss_prompt_adds_risk_management_header():
    """The big_loss branch reuses the loss prompt body but injects an
    extra risk-management section so personas can't paper over a
    crash with "we just missed the winners" narrative.
    """
    days = [_W_DAYS[0], _W_DAYS[1]]
    daily = [svc.DailyGainers(
        trading_day=days[0],
        gainers=[svc.GainerRow("6505", 9.83, 110.0, 100.16, days[0])],
    )]
    verdict = svc.OutcomeVerdict(
        status="big_loss",
        threshold_pct=-5.0, window_days=5, winners=[],
        best_pct=1.5, reason="2330 trough -7.0% (≤ -5% threshold)",
    )
    text = svc.format_post_mortem_loss_prompt(
        as_of=date(2026, 3, 23),
        trading_days=days,
        daily_top_gainers=daily,
        recommended_symbols=["2330"],
        verdict=verdict,
    )
    # big_loss-specific risk-management section + header
    assert "大敗" in text
    assert "風險控管" in text
    assert "risk_management" in text
    # Standard loss-branch leaderboard still present
    assert "6505" in text
    # Section A self-eval still removed
    assert "A. 你的推薦 D1-D5 自評" not in text


# ── build_post_mortem_message routing (verdict propagation) ──────


@pytest.mark.asyncio
async def test_build_post_mortem_message_returns_win_prompt(
    db_session: AsyncSession,
):
    """PR-B3: when the win threshold is met, build_post_mortem_message
    now returns a win-flavored prompt asking personas to identify
    *why* it worked (so post-mortem captures correct_signal_combo /
    successful_thesis lessons) rather than skipping. Prior to PR-B3
    this returned an empty prompt_text."""
    from datetime import datetime as _dt
    from uuid import uuid4

    base = date(2026, 3, 23)
    # 1 entry bar + 5 future days (Mon-Fri)
    days = [date(2026, 3, 24), date(2026, 3, 25), date(2026, 3, 26),
            date(2026, 3, 27), date(2026, 3, 30)]
    db_session.add_all([
        _bar("2330", base, 100.0),
        _bar("2330", days[0], 102.0),
        _bar("2330", days[1], 108.0),    # +8 % vs entry → clear win
        _bar("2330", days[2], 107.0),
        _bar("2330", days[3], 106.0),
        _bar("2330", days[4], 107.0),    # still +7 % at D5 → holds
    ])
    await db_session.commit()

    fake_disc = type("Disc", (), {})()
    fake_disc.id = uuid4()
    fake_disc.owner_id = uuid4()
    fake_disc.market = "TW"
    fake_disc.as_of_date = base
    fake_disc.conclusion = {"recommended_symbols": ["2330"]}
    fake_disc.created_at = _dt.now()

    payload = await svc.build_post_mortem_message(db_session, fake_disc)
    assert payload.verdict is not None
    assert payload.verdict.status == "win"
    assert payload.prompt_text   # non-empty (PR-B3)
    assert "事後檢討 — 命中經驗" in payload.prompt_text
    # Survivor-bias guard text must surface so the LLM doesn't just
    # spin a confident retrospective narrative.
    assert "survivor bias" in payload.prompt_text.lower() or \
        "事後諸葛" in payload.prompt_text
    # Win-side category names appear so the synthesizer steers
    # toward them.
    assert "correct_signal_combo" in payload.prompt_text
    assert "successful_thesis" in payload.prompt_text
    assert payload.trading_days   # non-empty


@pytest.mark.asyncio
async def test_build_post_mortem_message_returns_loss_prompt_when_below_threshold(
    db_session: AsyncSession,
):
    """All recs peak below 5% (and no day ≤ -5%) → prompt_text is the
    loss-branch prompt (without the self-eval section)."""
    from datetime import datetime as _dt
    from uuid import uuid4

    base = date(2026, 3, 23)
    days = [date(2026, 3, 24), date(2026, 3, 25), date(2026, 3, 26),
            date(2026, 3, 27), date(2026, 3, 30)]
    db_session.add_all([
        _bar("2330", base, 100.0),
        _bar("2330", days[0], 100.5),
        _bar("2330", days[1], 101.0),
        _bar("2330", days[2], 99.0),
        _bar("2330", days[3], 98.0),
        _bar("2330", days[4], 97.0),
    ])
    await db_session.commit()

    fake_disc = type("Disc", (), {})()
    fake_disc.id = uuid4()
    fake_disc.owner_id = uuid4()
    fake_disc.market = "TW"
    fake_disc.as_of_date = base
    fake_disc.conclusion = {"recommended_symbols": ["2330"]}
    fake_disc.created_at = _dt.now()

    payload = await svc.build_post_mortem_message(db_session, fake_disc)
    assert payload.verdict is not None
    assert payload.verdict.status == "loss"
    assert payload.prompt_text
    assert "事後檢討 — 漲幅榜複盤" in payload.prompt_text
    assert "A. 你的推薦 D1-D5 自評" not in payload.prompt_text


@pytest.mark.asyncio
async def test_build_post_mortem_message_returns_big_loss_when_any_day_crashes(
    db_session: AsyncSession,
):
    """Any close ≤ -5% → big_loss branch even when later days recover.
    Confirms the 大敗優先 rule end-to-end through build_post_mortem_message."""
    from datetime import datetime as _dt
    from uuid import uuid4

    base = date(2026, 3, 23)
    days = [date(2026, 3, 24), date(2026, 3, 25), date(2026, 3, 26),
            date(2026, 3, 27), date(2026, 3, 30)]
    db_session.add_all([
        _bar("2330", base, 100.0),
        _bar("2330", days[0], 92.0),     # -8% — triggers big_loss
        _bar("2330", days[1], 95.0),
        _bar("2330", days[2], 98.0),
        _bar("2330", days[3], 100.0),
        _bar("2330", days[4], 125.0),    # D5 +25% — would be big_win without 大敗優先
    ])
    await db_session.commit()

    fake_disc = type("Disc", (), {})()
    fake_disc.id = uuid4()
    fake_disc.owner_id = uuid4()
    fake_disc.market = "TW"
    fake_disc.as_of_date = base
    fake_disc.conclusion = {"recommended_symbols": ["2330"]}
    fake_disc.created_at = _dt.now()

    payload = await svc.build_post_mortem_message(db_session, fake_disc)
    assert payload.verdict is not None
    assert payload.verdict.status == "big_loss"
    assert "大敗" in payload.prompt_text
    assert "風險控管" in payload.prompt_text


# ── verdict serialiser ───────────────────────────────────────────


def test_verdict_to_dict_round_trip():
    v = svc.OutcomeVerdict(
        status="win", threshold_pct=3.0, window_days=5,
        winners=[svc.WinnerEntry("2330", 5.0, _W_DAYS[1])],
        best_pct=5.0, reason="ok",
    )
    out = svc.verdict_to_dict(v)
    assert out["status"] == "win"
    assert out["winners"][0]["symbol"] == "2330"
    assert out["winners"][0]["peak_day"] == "2026-03-25"
    assert out["best_pct"] == 5.0


# ── 4-band verdict classification ────────────────────────────────


def test_evaluate_outcome_big_loss_overrides_big_win():
    """大敗優先: any close ≤ -5% triggers big_loss even when D5 ≥ +20%.
    Confirms the discussion-level precedence agrees with the per-symbol
    classifier."""
    rec = [
        _perf("2330", [
            (_W_DAYS[0], -8.0),   # triggers big_loss
            (_W_DAYS[1], 5.0),
            (_W_DAYS[2], 10.0),
            (_W_DAYS[3], 20.0),
            (_W_DAYS[4], 25.0),   # D5 +25% — would be big_win without precedence
        ]),
    ]
    v = svc.evaluate_recommendation_outcome(
        rec, window_days=5, trading_days=_W_DAYS, **_BANDS,
    )
    assert v.status == "big_loss"
    assert v.threshold_pct == -5.0


def test_evaluate_outcome_big_win_requires_d5_close():
    """big_win is gated on the LAST day's close, not the peak — and a
    spike that fades all the way back is a loss, not a win."""
    rec = [
        _perf("2330", [
            (_W_DAYS[0], 25.0),    # peak +25% on D1
            (_W_DAYS[1], 15.0),
            (_W_DAYS[2], 10.0),
            (_W_DAYS[3], 5.0),
            (_W_DAYS[4], 3.0),     # D5 only +3%
        ]),
    ]
    v = svc.evaluate_recommendation_outcome(
        rec, window_days=5, trading_days=_W_DAYS, **_BANDS,
    )
    assert v.status == "loss"   # peak +25% but D5 only +3%
    assert v.best_pct == 25.0


def test_evaluate_outcome_big_win_when_d5_clears_threshold():
    rec = [
        _perf("2330", [
            (_W_DAYS[0], 5.0), (_W_DAYS[1], 10.0), (_W_DAYS[2], 15.0),
            (_W_DAYS[3], 18.0), (_W_DAYS[4], 22.0),
        ]),
    ]
    v = svc.evaluate_recommendation_outcome(
        rec, window_days=5, trading_days=_W_DAYS, **_BANDS,
    )
    assert v.status == "big_win"
    assert v.threshold_pct == 20.0


# ── New prompt builder: big_win ──────────────────────────────────


def test_format_big_win_prompt_demands_regime_disambiguation():
    """big_win prompt must force the regime-vs-stockpicking
    interrogation — without it, ≥+20% in 5 days gets credited to the
    persona's pick when it was really sector / macro driven."""
    days, rec, daily = _sample_payload()
    text = svc.format_post_mortem_big_win_prompt(
        as_of=date(2026, 3, 23),
        trading_days=days,
        recommended_performance=rec,
        daily_top_gainers=daily,
        recommended_symbols=["2330"],
        verdict=svc.OutcomeVerdict(
            status="big_win", threshold_pct=20.0, window_days=5,
            winners=[svc.WinnerEntry("2330", 22.0, days[1])],
            best_pct=22.0, reason="ok",
        ),
    )
    # Big-win banner
    assert "大勝" in text
    # Section B leaderboard present
    assert "B. 每日漲幅榜首" in text
    # Regime-vs-stockpicking categories surfaced
    assert "regime_context" in text
    assert "regime_capture" in text or "regime_signal" in text
    # Explicit "stock-picking vs regime-riding" framing
    assert "regime-riding" in text or "regime" in text
    assert "alpha" in text


# ── _resolve_thresholds (4-tuple) ────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_thresholds_returns_four_tuple_with_compiled_defaults(
    db_session: AsyncSession,
):
    """No DB overrides → resolver returns the compiled defaults
    (20.0, 5.0, -5.0, 5). Confirms the 4-tuple contract that
    `build_post_mortem_message` depends on."""
    big_win, win_bar, big_loss, window = await svc._resolve_thresholds(
        db_session,
    )
    assert big_win == 20.0
    assert win_bar == 5.0
    assert big_loss == -5.0
    assert window == 5
    assert big_loss < 0 < win_bar < big_win   # ordering invariant


@pytest.mark.asyncio
async def test_resolve_thresholds_falls_back_when_db_values_violate_ordering(
    db_session: AsyncSession,
):
    """If something writes degenerate values straight to the DB
    (bypassing `upsert`'s validation), the resolver detects the
    invariant violation and falls back to compiled defaults rather
    than handing the classifier a degenerate band layout."""
    import uuid as _uuid
    from models.runtime_setting import RuntimeSetting
    from models.user import User, UserRole

    # Seed an admin user so the FK constraint is satisfied.
    u = User(
        id=_uuid.uuid4(),
        email="resolver_test@example.com",
        hashed_password="x",
        role=UserRole.admin,
    )
    db_session.add(u)
    # Write degenerate values: big_loss positive (breaks invariant).
    db_session.add(RuntimeSetting(
        key="OUTCOME_BIG_LOSS_THRESHOLD_PCT",
        value=3.0, updated_by_id=u.id,
    ))
    db_session.add(RuntimeSetting(
        key="OUTCOME_WIN_THRESHOLD_PCT",
        value=5.0, updated_by_id=u.id,
    ))
    await db_session.commit()

    big_win, win_bar, big_loss, _ = await svc._resolve_thresholds(
        db_session,
    )
    # Falls back to compiled defaults.
    assert big_win == 20.0
    assert win_bar == 5.0
    assert big_loss == -5.0


# ── build_post_mortem_message dispatch — three-tier branches ─────


@pytest.mark.asyncio
async def test_build_post_mortem_message_returns_big_win_prompt(
    db_session: AsyncSession,
):
    """D5 close ≥ +20% → big_win branch. Prompt carries the regime
    interrogation + leaderboard."""
    from datetime import datetime as _dt
    from uuid import uuid4

    base = date(2026, 3, 23)
    days = [date(2026, 3, 24), date(2026, 3, 25), date(2026, 3, 26),
            date(2026, 3, 27), date(2026, 3, 30)]
    db_session.add_all([
        _bar("2330", base, 100.0),
        _bar("2330", days[0], 105.0),
        _bar("2330", days[1], 110.0),
        _bar("2330", days[2], 115.0),
        _bar("2330", days[3], 118.0),
        _bar("2330", days[4], 122.0),    # D5 +22% — big_win
    ])
    await db_session.commit()

    fake_disc = type("Disc", (), {})()
    fake_disc.id = uuid4()
    fake_disc.owner_id = uuid4()
    fake_disc.market = "TW"
    fake_disc.as_of_date = base
    fake_disc.conclusion = {"recommended_symbols": ["2330"]}
    fake_disc.created_at = _dt.now()

    payload = await svc.build_post_mortem_message(db_session, fake_disc)
    assert payload.verdict is not None
    assert payload.verdict.status == "big_win"
    assert payload.prompt_text
    assert "大勝" in payload.prompt_text
    assert "B. 每日漲幅榜首" in payload.prompt_text
    assert "regime" in payload.prompt_text


@pytest.mark.asyncio
async def test_build_post_mortem_message_win_branch_now_includes_leaderboard(
    db_session: AsyncSession,
):
    """Regression guard: the win branch (peak ∈ [5%, 10%)) must now
    include Section B leaderboard. Prior to this PR the win prompt
    only showed Section A (recommended self-eval) which created a
    blind spot for "I got 5%, perfect" complacency."""
    from datetime import datetime as _dt
    from uuid import uuid4

    base = date(2026, 3, 23)
    days = [date(2026, 3, 24), date(2026, 3, 25), date(2026, 3, 26),
            date(2026, 3, 27), date(2026, 3, 30)]
    # 2330 hits +7% (win band). Add a second symbol that runs +20% so
    # the leaderboard has visible content the personas would have
    # missed.
    db_session.add_all([
        _bar("2330", base, 100.0),
        _bar("2330", days[0], 102.0),
        _bar("2330", days[1], 107.0),
        _bar("2330", days[2], 106.5),
        _bar("2330", days[3], 106.0),
        _bar("2330", days[4], 107.0),    # holds +7 % at D5 → win band
        _bar("2454", base, 200.0),
        _bar("2454", days[0], 230.0),    # +15% single-day
        _bar("2454", days[1], 240.0),
        _bar("2454", days[2], 235.0),
        _bar("2454", days[3], 232.0),
        _bar("2454", days[4], 230.0),
    ])
    await db_session.commit()

    fake_disc = type("Disc", (), {})()
    fake_disc.id = uuid4()
    fake_disc.owner_id = uuid4()
    fake_disc.market = "TW"
    fake_disc.as_of_date = base
    fake_disc.conclusion = {"recommended_symbols": ["2330"]}
    fake_disc.created_at = _dt.now()

    payload = await svc.build_post_mortem_message(db_session, fake_disc)
    assert payload.verdict is not None
    assert payload.verdict.status == "win"
    assert "B. 每日漲幅榜首" in payload.prompt_text
    # Big mover (2454) appears in the leaderboard so the persona is
    # forced to compare their +7% pick against +15% they missed.
    assert "2454" in payload.prompt_text
