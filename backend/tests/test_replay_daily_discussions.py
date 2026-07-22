"""Tests for scripts.replay_daily_discussions.

The look-ahead detector is the part worth guarding: a replay that
leaked future prices would produce excellent-looking numbers that mean
nothing, and it would do so silently.
"""
from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.ohlcv_daily import OhlcvDaily
from scripts import replay_daily_discussions as rp


# ── _future_dates ────────────────────────────────────────────────


ANCHOR = date(2026, 7, 1)


def test_future_dates_flags_a_session_stamp_after_the_anchor():
    ctx = {"short_term_signals": {"2330": {"as_of": "2026-07-08", "rsi_14": 60}}}
    found = list(rp._future_dates(ctx, ANCHOR))
    assert found == [("short_term_signals.2330.as_of", "2026-07-08")]


def test_future_dates_accepts_the_anchor_itself_and_earlier():
    ctx = {
        "a": {"as_of": "2026-07-01"},
        "b": {"as_of": "2026-06-30"},
    }
    assert list(rp._future_dates(ctx, ANCHOR)) == []


def test_future_dates_walks_lists_and_reports_the_path():
    ctx = {"top_gainers": [
        {"symbol": "2330", "as_of_session": "2026-06-30"},
        {"symbol": "2454", "as_of_session": "2026-07-09"},
    ]}
    assert list(rp._future_dates(ctx, ANCHOR)) == [
        ("top_gainers[1].as_of_session", "2026-07-09"),
    ]


def test_future_dates_ignores_free_text_that_happens_to_hold_a_date():
    """Only fields named like a session stamp are judged. A persona
    writing "看好到 2026-08-01" is an opinion, not leaked data."""
    ctx = {"reasoning": "目標價看到 2026-08-01 前後", "note": "2026-12-31"}
    assert list(rp._future_dates(ctx, ANCHOR)) == []


def test_future_dates_ignores_unparseable_stamps():
    ctx = {"block": {"as_of": "not-a-date"}, "other": {"ts": ""}}
    assert list(rp._future_dates(ctx, ANCHOR)) == []


def test_future_dates_handles_iso_timestamps():
    ctx = {"block": {"as_of": "2026-07-05T13:30:00+08:00"}}
    assert list(rp._future_dates(ctx, ANCHOR)) == [
        ("block.as_of", "2026-07-05T13:30:00+08:00"),
    ]


# ── _trading_sessions ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_trading_sessions_reads_real_sessions_not_a_calendar(
    db_session: AsyncSession,
):
    """Holidays are excluded by observation. 2026-07-02 is deliberately
    absent from the archive and must not be returned."""
    class _CM:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *exc):
            return False

    for day in (date(2026, 7, 1), date(2026, 7, 3), date(2026, 7, 6)):
        db_session.add(OhlcvDaily(
            market="TW", symbol="_TAIEX_TR", ts=day,
            open=100.0, high=101.0, low=99.0, close=100.0,
            volume=0, source="test",
        ))
    await db_session.commit()

    with patch.object(rp, "AsyncSessionLocal", return_value=_CM()):
        out = await rp._trading_sessions(2, date(2026, 7, 6))

    assert out == [date(2026, 7, 3), date(2026, 7, 6)]


# ── as_of threading ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_replay_marks_rows_as_backtests_and_skips_the_daily_push():
    """A replayed session must carry `as_of_date` — that is what routes
    the context through the clamped path and the verifier through the
    archive — and must not fire the "今日 AI 選股完成" notification sixty
    times."""
    from tasks import auto_run_discussion as ar

    cfg = type("Cfg", (), {})()
    cfg.user_id = uuid.uuid4()
    cfg.topic = "t"
    cfg.rules = "r"
    cfg.persona_ids = ["market_analyst"]
    cfg.market = "TW"
    cfg.send_email = False
    cfg.strategy_run_counts = {"general": 1, "chip_quality": 0, "price_signal": 0}

    anchor = date(2026, 5, 20)
    slot = AsyncMock()
    db = AsyncMock()
    db.scalar = AsyncMock(return_value=None)

    with patch.object(ar, "load_candidate_rows", AsyncMock(return_value=[])) as load, \
         patch.object(ar, "_run_strategy_slot", slot), \
         patch.object(ar, "_notify_daily_ready", AsyncMock()) as notify:
        await ar._run_for_user(db, cfg, as_of=anchor)

    assert load.await_args.kwargs["as_of"] == anchor
    assert slot.await_args.kwargs["as_of"] == anchor
    # run_date is the replayed session, not today
    assert slot.await_args.args[4] == anchor
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_mode_still_notifies_and_carries_no_anchor():
    from tasks import auto_run_discussion as ar

    cfg = type("Cfg", (), {})()
    cfg.user_id = uuid.uuid4()
    cfg.topic = "t"
    cfg.rules = "r"
    cfg.persona_ids = ["market_analyst"]
    cfg.market = "TW"
    cfg.send_email = False
    cfg.strategy_run_counts = {"general": 1, "chip_quality": 0, "price_signal": 0}

    slot = AsyncMock()
    db = AsyncMock()
    db.scalar = AsyncMock(return_value=None)

    with patch.object(ar, "load_candidate_rows", AsyncMock(return_value=[])) as load, \
         patch.object(ar, "_run_strategy_slot", slot), \
         patch.object(ar, "_notify_daily_ready", AsyncMock()) as notify:
        await ar._run_for_user(db, cfg)

    assert load.await_args.kwargs["as_of"] is None
    assert slot.await_args.kwargs["as_of"] is None
    notify.assert_awaited_once()


# ── CLI ───────────────────────────────────────────────────────────


class _SessionCM:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *exc):
        return False


def _cfg():
    cfg = type("Cfg", (), {})()
    cfg.user_id = uuid.uuid4()
    cfg.strategy_run_counts = {"general": 1, "chip_quality": 1, "price_signal": 1}
    return cfg


@pytest.mark.asyncio
async def test_dry_run_prices_the_work_and_runs_nothing(
    db_session: AsyncSession, capsys,
):
    for day in (date(2026, 7, 1), date(2026, 7, 3)):
        db_session.add(OhlcvDaily(
            market="TW", symbol="_TAIEX_TR", ts=day,
            open=100.0, high=101.0, low=99.0, close=100.0,
            volume=0, source="test",
        ))
    await db_session.commit()

    run = AsyncMock()
    with patch.object(rp, "AsyncSessionLocal", return_value=_SessionCM(db_session)), \
         patch.object(
             rp.discussion_auto_run_config_service, "list_enabled",
             AsyncMock(return_value=[_cfg()]),
         ), \
         patch.object(rp, "_run_for_user", run):
        rc = await rp._main(["--sessions", "5", "--end", "2026-07-06", "--dry-run"])

    assert rc == 0
    run.assert_not_awaited()
    out = capsys.readouterr().out
    # 2 sessions x 3 strategies x $1.58
    assert "6 discussions" in out
    assert "US$9" in out


@pytest.mark.asyncio
async def test_no_enabled_config_is_an_error(db_session: AsyncSession):
    db_session.add(OhlcvDaily(
        market="TW", symbol="_TAIEX_TR", ts=date(2026, 7, 1),
        open=100.0, high=101.0, low=99.0, close=100.0,
        volume=0, source="test",
    ))
    await db_session.commit()

    with patch.object(rp, "AsyncSessionLocal", return_value=_SessionCM(db_session)), \
         patch.object(
             rp.discussion_auto_run_config_service, "list_enabled",
             AsyncMock(return_value=[]),
         ):
        rc = await rp._main(["--sessions", "5", "--end", "2026-07-06"])
    assert rc == 1


@pytest.mark.asyncio
async def test_no_archived_sessions_is_an_error(db_session: AsyncSession):
    with patch.object(rp, "AsyncSessionLocal", return_value=_SessionCM(db_session)):
        rc = await rp._main(["--sessions", "5", "--end", "2020-01-01"])
    assert rc == 1


@pytest.mark.asyncio
async def test_budget_ceiling_stops_before_overspending(
    db_session: AsyncSession, capsys,
):
    """The ceiling is a hard stop checked before each session, not a
    warning printed after the money is gone."""
    for day in (date(2026, 7, 1), date(2026, 7, 3), date(2026, 7, 6)):
        db_session.add(OhlcvDaily(
            market="TW", symbol="_TAIEX_TR", ts=day,
            open=100.0, high=101.0, low=99.0, close=100.0,
            volume=0, source="test",
        ))
    await db_session.commit()

    # Spend accrues as sessions run, the way the real ledger does.
    ledger = {"usd": 0.0}
    def _charge(*_a, **_k):
        ledger["usd"] += 4.74
        return True

    run = AsyncMock(side_effect=_charge)

    async def _spent():
        return ledger["usd"]

    with patch.object(rp, "AsyncSessionLocal", return_value=_SessionCM(db_session)), \
         patch.object(
             rp.discussion_auto_run_config_service, "list_enabled",
             AsyncMock(return_value=[_cfg()]),
         ), \
         patch.object(rp, "_run_for_user", run), \
         patch.object(rp, "_spent_usd", _spent):
        # One session costs 3 x 1.58 = 4.74; a 5.0 ceiling allows one.
        rc = await rp._main([
            "--sessions", "5", "--end", "2026-07-06", "--budget-usd", "5",
        ])

    assert rc == 0
    assert run.await_count == 1
    assert "ceiling" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_verify_only_reports_violations_and_exits_nonzero(
    db_session: AsyncSession, capsys,
):
    db_session.add(OhlcvDaily(
        market="TW", symbol="_TAIEX_TR", ts=date(2026, 7, 1),
        open=100.0, high=101.0, low=99.0, close=100.0,
        volume=0, source="test",
    ))
    await db_session.commit()

    with patch.object(rp, "AsyncSessionLocal", return_value=_SessionCM(db_session)), \
         patch.object(
             rp.discussion_auto_run_config_service, "list_enabled",
             AsyncMock(return_value=[_cfg()]),
         ), \
         patch.object(rp, "_verify_no_lookahead", AsyncMock(return_value=2)):
        rc = await rp._main([
            "--sessions", "5", "--end", "2026-07-06", "--verify-only",
        ])

    assert rc == 1
    assert "look-ahead violations: 2" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_sessions_with_a_live_run_are_not_replayed(
    db_session: AsyncSession, capsys,
):
    """A session that already produced a real auto-run must be left
    alone.

    The slot is unique on `(owner, auto_run_date, strategy, sequence)`,
    so a replay of such a session creates nothing — and the first
    version of this script keyed its own bookkeeping on `as_of_date`
    (NULL on live rows), asked for those sessions anyway, and printed
    "replayed (3/3)" over nine discussions that were never created.
    A replay would also double-count the session in every rate the
    scoreboard computes.
    """
    from models.discussion import Discussion
    from models.user import User, UserRole

    owner = User(id=uuid.uuid4(), email=f"rp-{uuid.uuid4().hex[:6]}@example.com",
                 hashed_password="x", role=UserRole.viewer, is_active=True)
    db_session.add(owner)
    for day in (date(2026, 7, 1), date(2026, 7, 3)):
        db_session.add(OhlcvDaily(
            market="TW", symbol="_TAIEX_TR", ts=day,
            open=100.0, high=101.0, low=99.0, close=100.0,
            volume=0, source="test",
        ))
    # A live auto-run on 07-03: no `as_of_date`, but the slot is taken.
    db_session.add(Discussion(
        id=uuid.uuid4(), owner_id=owner.id, topic="t", rules="r",
        persona_ids=["x"], market="TW", status="done", current_round=5,
        auto_run=True, auto_run_date=date(2026, 7, 3),
        auto_run_strategy="general", auto_run_sequence=1,
    ))
    await db_session.commit()

    cfg = _cfg()
    cfg.user_id = owner.id
    run = AsyncMock(return_value=True)
    with patch.object(rp, "AsyncSessionLocal", return_value=_SessionCM(db_session)), \
         patch.object(
             rp.discussion_auto_run_config_service, "list_enabled",
             AsyncMock(return_value=[cfg]),
         ), \
         patch.object(rp, "_run_for_user", run):
        rc = await rp._main(["--sessions", "5", "--end", "2026-07-06"])

    assert rc == 0
    assert run.await_count == 1
    assert run.await_args.kwargs["as_of"] == date(2026, 7, 1)
    assert "1 already covered" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_a_run_that_creates_nothing_exits_nonzero(
    db_session: AsyncSession, capsys,
):
    """Silence is not success. The caller is about to spend hundreds of
    dollars on the strength of this smoke test."""
    db_session.add(OhlcvDaily(
        market="TW", symbol="_TAIEX_TR", ts=date(2026, 7, 1),
        open=100.0, high=101.0, low=99.0, close=100.0,
        volume=0, source="test",
    ))
    await db_session.commit()

    with patch.object(rp, "AsyncSessionLocal", return_value=_SessionCM(db_session)), \
         patch.object(
             rp.discussion_auto_run_config_service, "list_enabled",
             AsyncMock(return_value=[_cfg()]),
         ), \
         patch.object(rp, "_run_for_user", AsyncMock(return_value=False)):
        rc = await rp._main(["--sessions", "5", "--end", "2026-07-06"])

    assert rc == 1
    assert "produced nothing" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_fully_covered_window_is_a_clean_no_op(
    db_session: AsyncSession, capsys,
):
    from models.discussion import Discussion
    from models.user import User, UserRole

    owner = User(id=uuid.uuid4(), email=f"rp-{uuid.uuid4().hex[:6]}@example.com",
                 hashed_password="x", role=UserRole.viewer, is_active=True)
    db_session.add(owner)
    db_session.add(OhlcvDaily(
        market="TW", symbol="_TAIEX_TR", ts=date(2026, 7, 1),
        open=100.0, high=101.0, low=99.0, close=100.0,
        volume=0, source="test",
    ))
    db_session.add(Discussion(
        id=uuid.uuid4(), owner_id=owner.id, topic="t", rules="r",
        persona_ids=["x"], market="TW", status="done", current_round=5,
        auto_run=True, auto_run_date=date(2026, 7, 1),
        auto_run_strategy="general", auto_run_sequence=1,
    ))
    await db_session.commit()

    cfg = _cfg()
    cfg.user_id = owner.id
    run = AsyncMock(return_value=True)
    with patch.object(rp, "AsyncSessionLocal", return_value=_SessionCM(db_session)), \
         patch.object(
             rp.discussion_auto_run_config_service, "list_enabled",
             AsyncMock(return_value=[cfg]),
         ), \
         patch.object(rp, "_run_for_user", run):
        rc = await rp._main(["--sessions", "5", "--end", "2026-07-06"])

    assert rc == 0
    run.assert_not_awaited()
    assert "nothing to replay" in capsys.readouterr().out
