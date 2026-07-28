"""Per-strategy scoreboard aggregation over verified auto-run rows."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from models.discussion import Discussion
from models.user import User, UserRole
from services.daily_scoreboard_service import build_scoreboard


async def _owner(db):
    u = User(id=uuid.uuid4(), email=f"sb-{uuid.uuid4().hex[:8]}@example.com",
             hashed_password="x", role=UserRole.viewer, is_active=True)
    db.add(u)
    await db.flush()
    return u


def _verified(
    owner_id,
    *,
    strategy: str,
    verdict: str,
    day1: dict | None = None,
    day5: dict | None = None,
    pool_performance: dict | None = None,
    auto_run: bool = True,
    status: str = "done",
    as_of_date=None,
    conclusion: dict | None = None,
):
    now = datetime(2026, 7, 10, tzinfo=UTC)
    return Discussion(
        id=uuid.uuid4(), owner_id=owner_id, topic="t", rules="r",
        persona_ids=["buffett"], market="TW", status=status,
        current_round=5, conclusion=conclusion or {"reasoning": "x"},
        auto_run=auto_run, auto_run_strategy=strategy,
        verdict=verdict, day1_open_prices=day1, day5_close_prices=day5,
        pool_performance=pool_performance, as_of_date=as_of_date,
        created_at=now, updated_at=now,
    )


@pytest.mark.asyncio
async def test_scoreboard_groups_and_aggregates(db_session):
    owner = await _owner(db_session)
    db_session.add_all([
        # general: 2 wins (one big), 1 loss → win rate 2/3
        _verified(owner.id, strategy="general", verdict="big_win",
                  day1={"2330": 100.0}, day5={"2330": 125.0},
                  pool_performance={"avg_return_pct": 5.0, "resolved": 40, "pool_size": 50}),
        _verified(owner.id, strategy="general", verdict="win",
                  day1={"2330": 100.0}, day5={"2330": 105.0}),
        _verified(owner.id, strategy="general", verdict="loss",
                  day1={"2330": 100.0}, day5={"2330": 98.0}),
        # unverifiable is excluded from the win-rate denominator
        _verified(owner.id, strategy="general", verdict="unverifiable"),
        # retired key still aggregates under its own name
        _verified(owner.id, strategy="chip_momentum", verdict="win",
                  day1={"1101": 50.0}, day5={"1101": 55.0}),
        # non-auto-run and non-done rows are ignored
        _verified(owner.id, strategy="general", verdict="win", auto_run=False),
        _verified(owner.id, strategy="general", verdict="win", status="draft"),
    ])
    await db_session.commit()

    entries = await build_scoreboard(db_session, owner.id)
    by_key = {e["strategy"]: e for e in entries}

    g = by_key["general"]
    assert g["samples"] == 4
    assert g["wins"] == 2 and g["losses"] == 1
    assert g["big_wins"] == 1 and g["big_losses"] == 0
    assert g["unverifiable"] == 1
    assert g["win_rate"] == round(2 / 3, 4)
    # returns: +25%, +5%, -2% → mean 9.3333
    assert g["avg_return_pct"] == round((25 + 5 - 2) / 3, 4)
    # alpha only from the row with pool_performance: 25 - 5 = 20
    assert g["pool_samples"] == 1
    assert g["avg_alpha_pct"] == 20.0

    legacy = by_key["chip_momentum"]
    assert legacy["samples"] == 1 and legacy["win_rate"] == 1.0
    # ordered by sample count descending
    assert entries[0]["strategy"] == "general"


@pytest.mark.asyncio
async def test_scoreboard_empty_and_partial_prices(db_session):
    owner = await _owner(db_session)
    assert await build_scoreboard(db_session, owner.id) == []

    # verdict without prices: counted in win rate, absent from returns
    db_session.add(_verified(owner.id, strategy="price_signal", verdict="win"))
    await db_session.commit()
    entries = await build_scoreboard(db_session, owner.id)
    assert entries[0]["win_rate"] == 1.0
    assert entries[0]["avg_return_pct"] is None
    assert entries[0]["avg_alpha_pct"] is None


@pytest.mark.asyncio
async def test_abstain_and_pending_stay_out_of_the_win_rate(db_session):
    """The three non-decided outcomes are reported separately.

    Collapsing them is exactly how a single decided round rendered as a
    "100% win rate" on the public page: an abstention is a deliberate
    pass, a pending row hasn't been graded yet, and neither is a win.
    """
    owner = await _owner(db_session)
    db_session.add_all([
        _verified(owner.id, strategy="chip_quality", verdict="win",
                  day1={"2330": 100.0}, day5={"2330": 106.0}),
        _verified(owner.id, strategy="chip_quality", verdict="abstain"),
        _verified(owner.id, strategy="chip_quality", verdict="abstain"),
        _verified(owner.id, strategy="chip_quality", verdict=None),
    ])
    await db_session.commit()

    entry = (await build_scoreboard(db_session, owner.id))[0]
    assert entry["samples"] == 4
    assert entry["abstains"] == 2
    assert entry["pending"] == 1
    assert entry["decided"] == 1
    # 1 win / 1 decided — a real 100%, but the caller can now see it
    # rests on a single round.
    assert entry["win_rate"] == 1.0


@pytest.mark.asyncio
async def test_excess_vs_taiex_uses_the_same_window(db_session):
    """Market-relative column: +6% while the index did +2% is +4% excess."""
    from datetime import date

    from models.ohlcv_daily import OhlcvDaily

    owner = await _owner(db_session)
    # The helper anchors discussions at 2026-07-10 (Taipei date).
    # Seed five index sessions from that date: open 100 → D5 close 102.
    for offset, close in enumerate([100.5, 101.0, 101.5, 101.8, 102.0]):
        db_session.add(OhlcvDaily(
            market="TW", symbol="_TAIEX_TR",
            ts=date(2026, 7, 10 + offset),
            open=100.0 if offset == 0 else close,
            high=close, low=close, close=close, volume=0, source="test",
        ))
    db_session.add(_verified(owner.id, strategy="general", verdict="win",
                             day1={"2330": 100.0}, day5={"2330": 106.0}))
    await db_session.commit()

    entry = (await build_scoreboard(db_session, owner.id))[0]
    assert entry["benchmark_samples"] == 1
    assert entry["avg_excess_vs_taiex_pct"] == pytest.approx(4.0, abs=1e-6)


@pytest.mark.asyncio
async def test_excess_omitted_while_index_window_incomplete(db_session):
    """Fewer than 5 index sessions → no excess, rather than a number
    computed over a truncated benchmark."""
    from datetime import date

    from models.ohlcv_daily import OhlcvDaily

    owner = await _owner(db_session)
    for offset, close in enumerate([100.5, 101.0]):
        db_session.add(OhlcvDaily(
            market="TW", symbol="_TAIEX_TR",
            ts=date(2026, 7, 10 + offset),
            open=100.0, high=close, low=close, close=close, volume=0, source="test",
        ))
    db_session.add(_verified(owner.id, strategy="general", verdict="win",
                             day1={"2330": 100.0}, day5={"2330": 106.0}))
    await db_session.commit()

    entry = (await build_scoreboard(db_session, owner.id))[0]
    assert entry["benchmark_samples"] == 0
    assert entry["avg_excess_vs_taiex_pct"] is None


# ── D5 lens alongside the verdict lens ───────────────────────────


@pytest.mark.asyncio
async def test_d5_lens_reported_alongside_the_verdict_lens(db_session):
    """The verdict bands are asymmetric on purpose: `win` grades on the
    D5 close, `big_loss` fires on ANY close ≤ −5%. A pick that dipped
    below the risk band mid-window but closed up is a `big_loss` by
    verdict and a win by D5 — both are true, and the scoreboard now
    says so instead of showing only the harsher one."""
    owner = await _owner(db_session)
    db_session.add_all([
        # Dipped through the risk band, closed +8% → big_loss verdict,
        # win under the D5 lens.
        _verified(owner.id, strategy="general", verdict="big_loss",
                  day1={"2330": 100.0}, day5={"2330": 108.0}),
        # Closed down: a loss under both lenses.
        _verified(owner.id, strategy="general", verdict="loss",
                  day1={"2330": 100.0}, day5={"2330": 97.0}),
    ])
    await db_session.commit()

    entry = (await build_scoreboard(db_session, owner.id))[0]
    # Verdict lens unchanged — this must stay the risk-aware view.
    assert entry["win_rate"] == pytest.approx(0.0)
    assert entry["big_losses"] == 1
    # D5 lens: one of the two closed above +5%.
    assert entry["d5_decided"] == 2
    assert entry["d5_wins"] == 1
    assert entry["d5_losses"] == 1
    assert entry["d5_win_rate"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_d5_lens_flags_picks_booked_before_their_window_closed(db_session):
    """The 2026-07 case that motivated this: a pick whose trough hit
    −7.1% was booked `big_loss` while `day5_close_prices` was still
    unset. It has no D5 lens at all — counting it as a 0% return would
    invent a number, so it is surfaced as unsettled instead."""
    owner = await _owner(db_session)
    db_session.add(_verified(owner.id, strategy="price_signal",
                             verdict="big_loss",
                             day1={"6243": 50.0}, day5=None))
    await db_session.commit()

    entry = (await build_scoreboard(db_session, owner.id))[0]
    assert entry["decided"] == 1          # counted by the verdict lens
    assert entry["d5_decided"] == 0       # but not yet by the D5 lens
    assert entry["d5_win_rate"] is None
    assert entry["d5_unsettled"] == 1


@pytest.mark.asyncio
async def test_d5_unsettled_ignores_ungraded_rows(db_session):
    """Pending and abstained rows have no D5 lens either, but they are
    not *unsettled verdicts* — only a row already booked win/loss
    without a close counts."""
    owner = await _owner(db_session)
    db_session.add_all([
        _verified(owner.id, strategy="general", verdict=None),
        _verified(owner.id, strategy="general", verdict="abstain"),
        _verified(owner.id, strategy="general", verdict="unverifiable"),
    ])
    await db_session.commit()

    entry = (await build_scoreboard(db_session, owner.id))[0]
    assert entry["d5_unsettled"] == 0


@pytest.mark.asyncio
async def test_scoreboard_excludes_backtest_replay_rows(db_session):
    """The public scoreboard is the LIVE track record. Backtest replay rows
    (`as_of_date` set) run under the same owner as live picks, so without an
    explicit filter they blend hindsight-informed replays into the published
    win rate. Only `as_of_date IS NULL` rows may count."""
    from datetime import date
    owner = await _owner(db_session)
    db_session.add_all([
        # one LIVE win
        _verified(owner.id, strategy="general", verdict="win",
                  day1={"2330": 100.0}, day5={"2330": 105.0}),
        # three BACKTEST rows (2 win, 1 loss) that must NOT count
        _verified(owner.id, strategy="general", verdict="big_win",
                  day1={"2330": 100.0}, day5={"2330": 130.0}, as_of_date=date(2026, 5, 1)),
        _verified(owner.id, strategy="general", verdict="win",
                  day1={"2330": 100.0}, day5={"2330": 106.0}, as_of_date=date(2026, 5, 2)),
        _verified(owner.id, strategy="general", verdict="loss",
                  day1={"2330": 100.0}, day5={"2330": 90.0}, as_of_date=date(2026, 5, 3)),
    ])
    await db_session.flush()
    entries = await build_scoreboard(db_session, owner.id)
    general = next(e for e in entries if e["strategy"] == "general")
    # Only the single live win — not 3 wins / 1 loss blended in.
    assert general["wins"] == 1
    assert general["losses"] == 0
    assert general["decided"] == 1


@pytest.mark.asyncio
async def test_tiered_win_rates_split_the_same_decided_rows(db_session):
    """Per-tier lenses reuse the decided-verdict denominator; the
    untiered win_rate still counts every decided row, and a mangled
    conclusion (tier None) falls into neither tier column."""
    owner = await _owner(db_session)
    recommend = {
        "recommended_symbols": ["2330"], "consensus_score": 0.95,
        "quality_signals": {"hallucination_warnings": []},
    }
    watch = {
        "recommended_symbols": ["1101"], "consensus_score": 0.5,
        "quality_signals": {"hallucination_warnings": []},
    }
    db_session.add_all([
        _verified(owner.id, strategy="general", verdict="win", conclusion=recommend),
        _verified(owner.id, strategy="general", verdict="loss", conclusion=recommend),
        _verified(owner.id, strategy="general", verdict="win", conclusion=watch),
        # Decided but tier-less (no recommended_symbols in stored JSON):
        # counts in the untiered totals, in neither tier column.
        _verified(owner.id, strategy="general", verdict="win", conclusion={"reasoning": "x"}),
    ])
    await db_session.commit()

    entry = (await build_scoreboard(db_session, owner.id))[0]
    assert entry["recommend_decided"] == 2
    assert entry["recommend_wins"] == 1
    assert entry["recommend_win_rate"] == 0.5
    assert entry["watch_decided"] == 1
    assert entry["watch_wins"] == 1
    assert entry["watch_win_rate"] == 1.0
    assert entry["decided"] == 4
    assert entry["win_rate"] == 0.75
