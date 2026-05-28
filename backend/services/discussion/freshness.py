"""Data-freshness labelling for discussion context.

Resolves a single "what session does this ctx actually represent" label
so personas can never confuse `ctx["captured_at"]` (wall-clock NOW) with
the trading session the underlying numbers came from. Two failure modes
this prevents:

  1. Manual NEW discussion fired intraday (e.g. 11:00 Taipei Wed) — most
     blocks read from TWSE `STOCK_DAY_ALL` / `ohlcv_daily`, both of
     which only refresh after the TW EOD publish (~14:30 Taipei). So
     the persona sees Tuesday's close labelled as if it were Wednesday.
  2. Daily auto-run fired 04:00 Taipei (20:00 UTC) — the auto-run code
     already anchors `as_of_date = prev_trading_day` so the data IS
     yesterday's close. But the prompt template was silent on this, so
     personas referenced yesterday's prices as "today's" anyway.

The fix is structural: ctx carries an explicit `captured_session` block
the prompt template surfaces near the top, and every quote-bearing
sub-block tags rows with `as_of_session` + `is_intraday` so the LLM
can't accidentally re-anchor to "today" when reading a row whose price
is from yesterday.

Pure helpers — no DB / Redis / HTTP. Backed by `pytz` only for the
Taipei conversion that already lives in `tw_trading_calendar`.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytz

from services.tw_trading_calendar import prev_trading_day_estimate

_TW = pytz.timezone("Asia/Taipei")
_ET = pytz.timezone("America/New_York")

# TWSE trading session bounds (Taipei local).
_TW_OPEN = time(9, 0)
_TW_CLOSE = time(13, 30)
# `STOCK_DAY_ALL` (the EOD all-stocks endpoint that drives quote /
# screener / focus_briefs.quote) refreshes ~14:30 Taipei. Before then,
# the endpoint still serves the previous trading day's close.
_TW_STOCK_DAY_ALL_PUBLISH = time(14, 30)

# US equity regular session (NYSE / NASDAQ).
_US_OPEN = time(9, 30)
_US_CLOSE = time(16, 0)
# Polygon / yfinance EOD bars settle within a few minutes of close.
_US_EOD_PUBLISH = time(16, 30)


def _tw_latest_complete_session(now_tw: datetime) -> tuple[date, str]:
    """Return (session_date, phase) for the most recent FULLY-PUBLISHED
    TWSE session relative to `now_tw` (Taipei-local datetime).

    Phases:
      - `today_close_published` — weekday, now >= 14:30 Taipei. Today's
        STOCK_DAY_ALL has refreshed; session_date = today.
      - `intraday`               — weekday, 09:00 <= now < 13:30. TW
        market is OPEN but EOD hasn't published; session_date = the
        prior trading day (the freshest *complete* session).
      - `between_close_and_publish` — weekday, 13:30 <= now < 14:30.
        Market closed, awaiting STOCK_DAY_ALL refresh; session_date =
        prior trading day.
      - `pre_open_today`         — weekday, now < 09:00. session_date =
        prior trading day.
      - `weekend_or_holiday`     — non-weekday. session_date = most
        recent weekday (best-effort, holidays still count as weekday
        here; downstream `ohlcv_daily` clamping handles real misses).
    """
    today = now_tw.date()
    t = now_tw.time()
    if today.weekday() >= 5:
        d = today
        while d.weekday() >= 5:
            d = d - timedelta(days=1)
        return d, "weekend_or_holiday"
    if t >= _TW_STOCK_DAY_ALL_PUBLISH:
        return today, "today_close_published"
    prev = prev_trading_day_estimate(today)
    if t >= _TW_CLOSE:
        return prev, "between_close_and_publish"
    if t >= _TW_OPEN:
        return prev, "intraday"
    return prev, "pre_open_today"


def _us_latest_complete_session(now_et: datetime) -> tuple[date, str]:
    """US-side equivalent of `_tw_latest_complete_session`. Same phase
    vocabulary swapped to NYSE hours (09:30-16:00 ET, EOD bars settle
    by ~16:30 ET). Weekday-only — federal holidays aren't tracked here;
    `ohlcv_daily` clamping handles real misses downstream.
    """
    today = now_et.date()
    t = now_et.time()
    if today.weekday() >= 5:
        d = today
        while d.weekday() >= 5:
            d = d - timedelta(days=1)
        return d, "weekend_or_holiday"
    if t >= _US_EOD_PUBLISH:
        return today, "today_close_published"
    prev = prev_trading_day_estimate(today)
    if t >= _US_CLOSE:
        return prev, "between_close_and_publish"
    if t >= _US_OPEN:
        return prev, "intraday"
    return prev, "pre_open_today"


def resolve_captured_session(
    *,
    market: str,
    as_of: date | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the `captured_session` block injected into every discussion
    ctx. Three modes:

      - **Backtest** (`as_of` set) — session_date = as_of, phase =
        `backtest`. The hint tells personas the entire ctx is clamped
        to a historical anchor.
      - **TW live** (`market == 'TW'`, `as_of=None`) — derive from the
        Taipei calendar via `_tw_latest_complete_session`. The hint
        spells out exactly what's stale during each phase so personas
        don't re-anchor to wall-clock today.
      - **US / GLOBAL / CRYPTO live** — minimal label for now; the bug
        report scope is TW. Phase 4 will extend US once we add the
        Eastern-time helper.

    Output shape (always populated for any call):
      ```
      {
        "session_date": "2026-05-27" | null,
        "phase": "intraday" | "today_close_published" | "backtest" | ...,
        "is_intraday": bool,
        "hint_zh": "資料截至 2026-05-27 收盤...",
      }
      ```
    """
    if as_of is not None:
        return {
            "session_date": as_of.isoformat(),
            "phase": "backtest",
            "is_intraday": False,
            "hint_zh": (
                f"回測模式。本次 ctx 內所有報價、技術指標、籌碼、"
                f"新聞情緒皆以 {as_of.isoformat()} 為錨點（`ts <= as_of`），"
                "不含該日之後的任何資料。"
            ),
        }

    if market == "TW":
        n = (now or datetime.now(UTC)).astimezone(_TW)
        sess, phase = _tw_latest_complete_session(n)
        today = n.date()
        hints = {
            "today_close_published": (
                f"資料截至 {sess.isoformat()} 收盤。TWSE 已於 14:30 "
                "公告今日完整收盤檔，多數區塊已含今日數據。"
            ),
            "between_close_and_publish": (
                f"TWSE 今日 ({today.isoformat()}) 已收盤但尚未發布"
                f"完整 STOCK_DAY_ALL；報價／技術指標仍以 "
                f"{sess.isoformat()} 為錨。"
            ),
            "intraday": (
                f"TWSE 盤中 ({today.isoformat()})。多數區塊報價、"
                f"漲跌幅、5/20 日報酬仍反映 {sess.isoformat()} 收盤；"
                "只有 `index.value` 是真正盤中即時數值。"
                "**短線技術指標 (RSI / MACD / gap_pct) 是基於昨日收盤計算**，"
                "今日盤中走勢尚未納入。"
            ),
            "pre_open_today": (
                f"TWSE 尚未開盤 ({today.isoformat()} 09:00 前)。"
                f"資料截至 {sess.isoformat()} 收盤；本次討論屬於"
                "盤前推論，今日開盤後可能修正。"
            ),
            "weekend_or_holiday": (
                f"非交易日 ({today.isoformat()})。資料截至 "
                f"{sess.isoformat()} 收盤。"
            ),
        }
        return {
            "session_date": sess.isoformat(),
            "phase": phase,
            "is_intraday": False,
            "hint_zh": hints[phase],
        }

    if market == "US":
        n = (now or datetime.now(UTC)).astimezone(_ET)
        sess, phase = _us_latest_complete_session(n)
        today = n.date()
        hints = {
            "today_close_published": (
                f"US data as of {sess.isoformat()} close. NYSE/NASDAQ "
                "regular session已收盤。"
            ),
            "between_close_and_publish": (
                f"NYSE 今日 ({today.isoformat()}) 已收盤但 EOD bar 尚未"
                f"沉澱完成；多數區塊仍以 {sess.isoformat()} 為錨。"
            ),
            "intraday": (
                f"NYSE 盤中 ({today.isoformat()})。即時報價反映今日盤中，"
                f"但 ohlcv_daily 衍生的技術指標仍以 {sess.isoformat()} "
                "為錨。"
            ),
            "pre_open_today": (
                f"NYSE 尚未開盤。資料截至 {sess.isoformat()} 收盤。"
            ),
            "weekend_or_holiday": (
                f"非交易日。資料截至 {sess.isoformat()} 收盤。"
            ),
        }
        return {
            "session_date": sess.isoformat(),
            "phase": phase,
            "is_intraday": False,
            "hint_zh": hints[phase],
        }

    if market == "CRYPTO":
        n = now or datetime.now(UTC)
        return {
            "session_date": n.date().isoformat(),
            "phase": "crypto_24_7",
            "is_intraday": True,
            "hint_zh": (
                "Crypto 24/7 連續交易，報價反映 Kraken 即時撮合"
                "（WS 推送，秒級延遲）。"
            ),
        }

    n = now or datetime.now(UTC)
    return {
        "session_date": n.date().isoformat(),
        "phase": f"live_{market.lower()}",
        "is_intraday": False,
        "hint_zh": f"{market} live mode。",
    }


def tw_quote_session(now: datetime | None = None) -> str:
    """Helper for sub-blocks that ingest from TWSE `STOCK_DAY_ALL` (quote
    / screener / focus_briefs.quote). Returns the ISO date of the
    session whose close those reads actually carry — same logic as the
    top-level `captured_session.session_date` for TW live mode, exposed
    separately so individual rows can stamp `as_of_session` without
    threading the whole `captured_session` dict through every call
    site.
    """
    n = (now or datetime.now(UTC)).astimezone(_TW)
    sess, _ = _tw_latest_complete_session(n)
    return sess.isoformat()


def tw_index_is_intraday(now: datetime | None = None) -> bool:
    """True when TWSE is in regular session (09:00 <= now < 13:30 on a
    weekday). `index.value` (`MI_5MINS`) IS true-intraday during this
    window — it's the one TW block where today's wall-clock price is
    real.
    """
    n = (now or datetime.now(UTC)).astimezone(_TW)
    if n.date().weekday() >= 5:
        return False
    return _TW_OPEN <= n.time() < _TW_CLOSE


def tw_index_session(now: datetime | None = None) -> str:
    """Session date for the TAIEX `MI_5MINS`-backed `index.value`.

    Different from `tw_quote_session()` because `MI_5MINS` IS intraday
    — during 09:00-13:30 Taipei it carries today's live TAIEX, and
    after the 13:30 close it carries today's final close (without
    waiting for the STOCK_DAY_ALL 14:30 publish window). Pre-open and
    weekend fall back to the previous trading day (`MI_5MINS` returns
    yesterday's last value at those times).
    """
    n = (now or datetime.now(UTC)).astimezone(_TW)
    today = n.date()
    if today.weekday() >= 5:
        d = today
        while d.weekday() >= 5:
            d = d - timedelta(days=1)
        return d.isoformat()
    if n.time() >= _TW_OPEN:
        return today.isoformat()
    return prev_trading_day_estimate(today).isoformat()


def us_quote_is_intraday(now: datetime | None = None) -> bool:
    """True when NYSE is in regular session (09:30 <= now < 16:00 ET on
    a weekday). US live quotes via yfinance / Polygon are intraday
    during this window.
    """
    n = (now or datetime.now(UTC)).astimezone(_ET)
    if n.date().weekday() >= 5:
        return False
    return _US_OPEN <= n.time() < _US_CLOSE


def us_quote_session(now: datetime | None = None) -> str:
    """US-side parallel of `tw_quote_session` — returns the ISO date
    of the latest fully-published NYSE session.
    """
    n = (now or datetime.now(UTC)).astimezone(_ET)
    sess, _ = _us_latest_complete_session(n)
    return sess.isoformat()
