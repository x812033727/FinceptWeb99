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
    a confident retrospective narrative that overfits the success."""
    days, rec, _ = _sample_payload()
    text = svc.format_post_mortem_win_prompt(
        as_of=date(2026, 3, 23),
        trading_days=days,
        recommended_performance=rec,
        recommended_symbols=["2330"],
        verdict=svc.OutcomeVerdict(
            status="win", threshold_pct=3.0, window_days=5,
            winners=[svc.WinnerEntry("2330", 8.0, days[1])],
            best_pct=8.0, reason="ok",
        ),
    )
    # Header + window
    assert "事後檢討 — 命中經驗" in text
    assert "2026-03-23" in text
    assert "通過" in text and "3" in text   # 通過 ${threshold}% 門檻
    # Survivor-bias guards
    assert "當時的不確定點" in text
    assert "沒有特別不確定的訊號" in text   # admit-luck escape hatch
    # Category steering
    assert "correct_signal_combo" in text
    assert "successful_thesis" in text


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


def test_evaluate_outcome_marks_win_when_any_symbol_peak_reaches_threshold():
    """One symbol clears 3% peak → status=win; the other's miss
    doesn't downgrade the verdict (any-symbol semantic)."""
    rec = [
        _perf("2330", [(_W_DAYS[0], 1.0), (_W_DAYS[1], 5.0), (_W_DAYS[2], 2.0)]),
        _perf("2454", [(_W_DAYS[0], 0.5), (_W_DAYS[1], -2.0)]),
    ]
    v = svc.evaluate_recommendation_outcome(
        rec, threshold_pct=3.0, window_days=5, trading_days=_W_DAYS,
    )
    assert v.status == "win"
    assert v.best_pct == 5.0
    assert {w.symbol for w in v.winners} == {"2330"}
    assert v.winners[0].peak_day == _W_DAYS[1]


def test_evaluate_outcome_marks_miss_when_all_peaks_below_threshold():
    """All recs peak below 3% → status=miss; best_pct still surfaced
    so the operator can see how close it came."""
    rec = [
        _perf("2330", [(_W_DAYS[0], 1.0), (_W_DAYS[1], 1.5)]),
        _perf("2454", [(_W_DAYS[0], -0.5), (_W_DAYS[1], 2.5)]),
    ]
    v = svc.evaluate_recommendation_outcome(
        rec, threshold_pct=3.0, window_days=5, trading_days=_W_DAYS,
    )
    assert v.status == "miss"
    assert v.winners == []
    assert v.best_pct == 2.5


def test_evaluate_outcome_marks_insufficient_data_with_empty_window():
    v = svc.evaluate_recommendation_outcome(
        [], threshold_pct=3.0, window_days=5, trading_days=[],
    )
    assert v.status == "insufficient_data"
    assert v.best_pct is None


def test_evaluate_outcome_marks_insufficient_data_with_no_recs_having_bars():
    """Trading days resolved but every recommendation lacks bars
    inside the window → can't grade → insufficient_data."""
    v = svc.evaluate_recommendation_outcome(
        [], threshold_pct=3.0, window_days=5, trading_days=_W_DAYS,
    )
    assert v.status == "insufficient_data"


def test_evaluate_outcome_window_narrows_horizon():
    """Peak that lands on D5 doesn't count when the runtime window
    is set to 3 days — important for shorter horizon backtests."""
    rec = [_perf("2330", [
        (_W_DAYS[0], 0.5), (_W_DAYS[1], 0.5), (_W_DAYS[2], 1.0),
        (_W_DAYS[3], 5.0),
    ])]
    v = svc.evaluate_recommendation_outcome(
        rec, threshold_pct=3.0, window_days=3, trading_days=_W_DAYS,
    )
    assert v.status == "miss"
    assert v.best_pct == 1.0


# ── format_post_mortem_miss_prompt ───────────────────────────────


def test_miss_prompt_omits_self_eval_section_and_includes_threshold_note():
    """The miss branch removes the A self-eval block — personas already
    lost, asking them to re-grade adds noise. The prompt must instead
    surface the threshold + best_pct so the negative framing is honest."""
    days = [_W_DAYS[0], _W_DAYS[1]]
    daily = [svc.DailyGainers(
        trading_day=days[0],
        gainers=[svc.GainerRow("6505", 9.83, 110.0, 100.16, days[0])],
    )]
    verdict = svc.OutcomeVerdict(
        status="miss",
        threshold_pct=3.0, window_days=5, winners=[],
        best_pct=1.5, reason="all peaks below threshold",
    )
    text = svc.format_post_mortem_miss_prompt(
        as_of=date(2026, 3, 23),
        trading_days=days,
        daily_top_gainers=daily,
        recommended_symbols=["2330"],
        verdict=verdict,
    )
    # Negative framing
    assert "事後檢討 — 漲幅榜複盤" in text
    assert "+1.50%" in text
    assert "未達 3%" in text
    # Section A explicitly removed in the miss branch
    assert "A. 你的推薦 D1-D5 自評" not in text
    # B-equivalent (winners) still present
    assert "6505" in text
    # Reflection prompts focus on "missed", not self-grading
    assert "缺漏的主題" in text or "漏看" in text
    assert "自評勝負" not in text


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
        _bar("2330", days[2], 105.0),
        _bar("2330", days[3], 103.0),
        _bar("2330", days[4], 99.0),
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
async def test_build_post_mortem_message_returns_miss_prompt_when_below_threshold(
    db_session: AsyncSession,
):
    """All recs peak below 3% → prompt_text is the miss-branch
    prompt (without the self-eval section)."""
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
    assert payload.verdict.status == "miss"
    assert payload.prompt_text
    assert "事後檢討 — 漲幅榜複盤" in payload.prompt_text
    assert "A. 你的推薦 D1-D5 自評" not in payload.prompt_text


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
