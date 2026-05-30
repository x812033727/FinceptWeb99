"""Tests for `services.discussion.freshness`.

The freshness module decides what trading session a discussion's ctx
data actually represents. The phase classification drives the prompt
preamble, the conclusion badge, and the per-row `as_of_session`
stamps that downstream blocks attach — so it has to be defensively
correct across the TWSE calendar:

  - Pre-open (00:00-09:00 Taipei)        → prev trading day
  - Intraday (09:00-13:30 Taipei)        → prev trading day
                                           (`STOCK_DAY_ALL` hasn't
                                            refreshed today yet)
  - Post-close, pre-publish              → prev trading day
    (13:30-14:30 Taipei)
  - Post-publish (>= 14:30 Taipei)       → today (`STOCK_DAY_ALL` is
                                           live)
  - Weekend                              → most recent weekday

Backtest mode (`as_of` set) collapses to a single phase regardless of
wall-clock NOW.
"""
from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
import pytz

from services.discussion.freshness import (
    resolve_captured_session,
    tw_index_is_intraday,
    tw_index_session,
    tw_quote_session,
    us_quote_is_intraday,
    us_quote_session,
)

_TW = pytz.timezone("Asia/Taipei")
_ET = pytz.timezone("America/New_York")


def _at_tw(y: int, m: int, d: int, hh: int, mm: int = 0) -> datetime:
    """Build a Taipei-local datetime then convert to UTC — `freshness`
    helpers all accept tz-aware UTC and re-localise internally, so
    this is the same surface the production call sites use."""
    local = _TW.localize(datetime(y, m, d, hh, mm))
    return local.astimezone(UTC)


def _at_et(y: int, m: int, d: int, hh: int, mm: int = 0) -> datetime:
    local = _ET.localize(datetime(y, m, d, hh, mm))
    return local.astimezone(UTC)


# ── resolve_captured_session: backtest mode ────────────────────────


def test_backtest_mode_anchors_on_as_of_ignoring_wall_clock():
    """`as_of != None` short-circuits all wall-clock derivation —
    sweep replays at as_of=2025-06-01 must produce the same label
    regardless of when the replay runs (1 year later, midnight, etc.).
    Otherwise an old backtest reopened today would carry today's
    phase, leaking the future."""
    anchor = date(2025, 6, 1)
    sess = resolve_captured_session(
        market="TW", as_of=anchor,
        now=_at_tw(2026, 5, 28, 11),
    )
    assert sess["session_date"] == "2025-06-01"
    assert sess["phase"] == "backtest"
    assert sess["is_intraday"] is False
    assert "回測" in sess["hint_zh"]
    assert "2025-06-01" in sess["hint_zh"]


def test_backtest_mode_session_date_uses_info_cutoff_decision_date_is_as_of():
    """When the builder passes `info_cutoff` (the previous trading day),
    `session_date` reflects the cutoff — the latest session the personas
    may see — while `decision_date` stays `as_of` (the entry / grading
    day). The hint must name both so a persona predicting `as_of` knows
    it's looking at the prior session and entering at as_of's open. This
    is the look-ahead guard: a backtest for 4/1 may only see ≤ 3/31."""
    sess = resolve_captured_session(
        market="TW",
        as_of=date(2026, 4, 1),
        info_cutoff=date(2026, 3, 31),
        now=_at_tw(2026, 5, 28, 11),
    )
    assert sess["session_date"] == "2026-03-31"
    assert sess["decision_date"] == "2026-04-01"
    assert sess["phase"] == "backtest"
    hint = sess["hint_zh"]
    assert "2026-03-31" in hint, "hint must name the info cutoff (prior session)"
    assert "2026-04-01" in hint, "hint must name the decision / entry day"
    assert "前一交易日" in hint


# ── resolve_captured_session: TW live mode phase classification ────


@pytest.mark.parametrize("hh,mm,expected_phase,expected_session", [
    # Wed 2026-05-27 weekday. Prev trading day = Tue 2026-05-26.
    (3, 0, "pre_open_today", "2026-05-26"),       # 03:00 — pre-open
    (8, 59, "pre_open_today", "2026-05-26"),      # 08:59 — pre-open
    (9, 0, "intraday", "2026-05-26"),             # 09:00 — open bell
    (11, 30, "intraday", "2026-05-26"),           # mid-session
    (13, 29, "intraday", "2026-05-26"),           # 1 min before close
    (13, 30, "between_close_and_publish", "2026-05-26"),  # close
    (14, 29, "between_close_and_publish", "2026-05-26"),  # awaiting publish
    (14, 30, "today_close_published", "2026-05-27"),      # publish
    (18, 0, "today_close_published", "2026-05-27"),       # evening
    (23, 59, "today_close_published", "2026-05-27"),      # late night
])
def test_tw_live_mode_phase_by_hour_of_day(hh, mm, expected_phase, expected_session):
    """TW live mode must classify every hour of a weekday into the
    correct STOCK_DAY_ALL-aware phase. Phase boundaries:
    09:00 / 13:30 / 14:30 Taipei. Off-by-one errors here mean the
    persona prompt either swears today's data is final when it isn't
    (false positive) or claims yesterday's data is fresh well into
    the post-publish window (false negative)."""
    sess = resolve_captured_session(
        market="TW", as_of=None,
        now=_at_tw(2026, 5, 27, hh, mm),
    )
    assert sess["phase"] == expected_phase, (
        f"At Taipei {hh:02d}:{mm:02d} on Wed expected "
        f"phase={expected_phase!r}, got {sess['phase']!r}"
    )
    assert sess["session_date"] == expected_session


def test_tw_live_weekend_resolves_to_most_recent_weekday():
    """Saturday auto-run (rare but possible) must anchor on Friday's
    close, not Saturday's empty calendar slot. Same for Sunday."""
    sat = resolve_captured_session(
        market="TW", as_of=None,
        now=_at_tw(2026, 5, 30, 10),  # 2026-05-30 = Sat
    )
    assert sat["phase"] == "weekend_or_holiday"
    assert sat["session_date"] == "2026-05-29"  # Fri

    sun = resolve_captured_session(
        market="TW", as_of=None,
        now=_at_tw(2026, 5, 31, 10),  # 2026-05-31 = Sun
    )
    assert sun["phase"] == "weekend_or_holiday"
    assert sun["session_date"] == "2026-05-29"  # Fri


def test_tw_live_intraday_hint_mentions_today_and_session():
    """The hint string in `intraday` phase MUST explicitly mention
    both today's date (so the persona knows the wall-clock day) AND
    the actual session anchor (so the persona doesn't refer to
    "today's RSI" when RSI was computed against yesterday's close).
    This text is the strongest defence against the original bug
    where personas confused captured_at with session_date."""
    sess = resolve_captured_session(
        market="TW", as_of=None,
        now=_at_tw(2026, 5, 27, 11, 30),  # Wed mid-session
    )
    assert sess["phase"] == "intraday"
    hint = sess["hint_zh"]
    assert "2026-05-27" in hint, "intraday hint must name today's date"
    assert "2026-05-26" in hint, "intraday hint must name the session anchor"
    assert "盤中" in hint or "RSI" in hint


# ── per-block helpers: tw_quote_session / tw_index_session ─────────


def test_tw_quote_session_matches_captured_session_for_stock_day_all_blocks():
    """`tw_quote_session()` is what screener / focus_briefs.quote
    stamp onto their rows. It must match `captured_session.session_date`
    for STOCK_DAY_ALL-driven phases (intraday / between /
    today_close_published) so the per-row stamp doesn't disagree
    with the top-level anchor."""
    # Intraday: prev trading day for both
    now = _at_tw(2026, 5, 27, 11)
    top = resolve_captured_session(market="TW", as_of=None, now=now)
    assert top["session_date"] == "2026-05-26"
    assert tw_quote_session(now=now) == "2026-05-26"

    # Post-publish: today for both
    now2 = _at_tw(2026, 5, 27, 15)
    top2 = resolve_captured_session(market="TW", as_of=None, now=now2)
    assert top2["session_date"] == "2026-05-27"
    assert tw_quote_session(now=now2) == "2026-05-27"


def test_tw_index_session_diverges_from_quote_session_during_intraday():
    """`tw_index_session()` is for `MI_5MINS`-backed TAIEX value —
    intraday it IS today (MI_5MINS publishes 5-min ticks), whereas
    STOCK_DAY_ALL-backed `tw_quote_session()` is still yesterday.
    This divergence is intentional and is what lets TAIEX show
    today's live value alongside individual-stock quotes that are
    stuck on yesterday's close."""
    now = _at_tw(2026, 5, 27, 11)  # Wed intraday
    assert tw_quote_session(now=now) == "2026-05-26"
    assert tw_index_session(now=now) == "2026-05-27"


def test_tw_index_is_intraday_only_inside_regular_session():
    """`is_intraday=True` exactly in 09:00 <= now < 13:30 on a
    weekday. The exact 13:30 boundary is False — the close bell
    just rang, MI_5MINS still updates but the bar is final."""
    weekday = 2026, 5, 27  # Wed
    assert not tw_index_is_intraday(now=_at_tw(*weekday, 8, 59))
    assert tw_index_is_intraday(now=_at_tw(*weekday, 9, 0))
    assert tw_index_is_intraday(now=_at_tw(*weekday, 11, 30))
    assert tw_index_is_intraday(now=_at_tw(*weekday, 13, 29))
    assert not tw_index_is_intraday(now=_at_tw(*weekday, 13, 30))
    assert not tw_index_is_intraday(now=_at_tw(*weekday, 18, 0))
    # Weekend never intraday
    assert not tw_index_is_intraday(now=_at_tw(2026, 5, 30, 11))


# ── US-side parallel helpers ───────────────────────────────────────


def test_us_quote_session_and_is_intraday_use_eastern_time():
    """US helpers must convert to America/New_York before classifying.
    A discussion fired 22:00 UTC on a US trading day = 18:00 ET, post-
    publish, so session = today; the same UTC instant in Taipei is
    06:00 next day, but the US classification must NOT slide a day
    forward."""
    # 2026-05-27 Wed. 18:00 ET (post 16:30 EOD publish).
    now = _at_et(2026, 5, 27, 18)
    assert us_quote_session(now=now) == "2026-05-27"
    assert us_quote_is_intraday(now=now) is False

    # 14:00 ET — NYSE in session
    now2 = _at_et(2026, 5, 27, 14)
    assert us_quote_session(now=now2) == "2026-05-26"
    assert us_quote_is_intraday(now=now2) is True


# ── shapes / unknown markets ───────────────────────────────────────


def test_unknown_market_falls_back_to_generic_label():
    """Unknown markets (e.g. a future `JP` add before the helper is
    extended) must NOT raise — they get a generic live label so the
    prompt template still renders."""
    sess = resolve_captured_session(
        market="JP", as_of=None,
        now=_at_tw(2026, 5, 27, 11),
    )
    assert sess["session_date"] is not None
    assert sess["phase"] == "live_jp"
    assert sess["is_intraday"] is False


def test_resolve_captured_session_always_returns_complete_shape():
    """Every code path must return all four keys so the prompt
    template + frontend badge can read them defensively without
    `.get(...) or X` chains."""
    cases = [
        ("TW", None, _at_tw(2026, 5, 27, 11)),
        ("TW", date(2025, 1, 1), _at_tw(2026, 5, 27, 11)),
        ("US", None, _at_et(2026, 5, 27, 14)),
        ("CRYPTO", None, _at_tw(2026, 5, 30, 0)),  # weekend OK for crypto
        ("GLOBAL", None, _at_tw(2026, 5, 27, 11)),
    ]
    for market, as_of, now in cases:
        sess = resolve_captured_session(market=market, as_of=as_of, now=now)
        assert {
            "session_date", "phase", "is_intraday", "hint_zh",
        } <= set(sess.keys()), f"market={market} as_of={as_of} missing keys: {sess}"
        assert isinstance(sess["is_intraday"], bool)
        assert isinstance(sess["hint_zh"], str)
        assert sess["hint_zh"]  # non-empty
        # Backtest mode adds `decision_date` (the entry / grading day)
        # alongside `session_date` (the info cutoff); live modes omit it.
        assert ("decision_date" in sess) == (as_of is not None)
