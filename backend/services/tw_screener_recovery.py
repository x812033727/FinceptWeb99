"""TW screener last-ditch recovery — pulled out of ``tw_market_service``
(PR-D of the long-running TW-service split).

Owns the two independent-source recovery paths that fire when
``STOCK_DAY_ALL`` returns a stale session that ``ohlcv_daily`` already
has the right close for:

  ``_recover_screener_via_finmind``    Tier 1 — FinMind sponsor's
                                       ``TaiwanStockPrice`` (one market-
                                       wide call) when a paid token is
                                       configured
  ``_recover_screener_via_yfinance``   Tier 2 — Yahoo chart endpoint, an
                                       independent pipeline that doesn't
                                       share TWSE's warehouse so it
                                       usually carries today's close
                                       even when STOCK_DAY_ALL lags

Plus the small ergonomic surface around them:

  ``_STALENESS_BELLWETHER``  the per-tier sanity probe (2330 TSMC)
  ``_TTL_SCREENER_STALE``    short TTL for stale results so a 60 s
                             window of bad data can't shadow the rest
                             of the ``TTL_SCREENER`` 10-min cache
  ``_yf_ticker_for``         symbol → ``.TW``/``.TWO`` mapping for
                             yfinance only
  ``_record_attempt`` /      diagnostic sink helpers consumed by
  ``_set_final_data_source`` / ``fetch_screener`` to surface the
  ``_seed_diagnostic``       recovery trail on the discussion ctx

Why ``_bellwether_ohlcv_closes`` / ``_detect_stock_day_all_session`` /
``_independent_source_is_fresh`` / ``get_latest_ohlcv_session`` /
``_latest_complete_session`` stay in ``tw_market_service``: the
test suite ``test_tw_market_service_staleness.py`` patches them via
``patch.object(svc, "_bellwether_ohlcv_closes", ...)`` to mock the DB
underneath ``get_screener``. A cross-module move would break those
patches — the patched binding in ``svc``'s namespace wouldn't reach
the call site in this module. The recovery orchestrators here
lazy-import ``_independent_source_is_fresh`` so its mocked
``_bellwether_ohlcv_closes`` dependency still resolves at call time.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

import data.tw.finmind_connector as finmind

log = logging.getLogger(__name__)


# Bellwether for STOCK_DAY_ALL staleness checks: 2330 (TSMC). Always
# traded, never halted in practice. If 2330's close in the TWSE
# response equals yesterday's close in ohlcv_daily, the entire
# response is one trading day behind.
_STALENESS_BELLWETHER = "2330"

# Stale screener results cache for 60s rather than the full
# `TTL_SCREENER` (10 min). With 10 min, a single stale fetch shadows
# every subsequent call for the rest of the window — even after TWSE
# refreshes or `ohlcv_daily` catches up. 60s keeps recovery attempts
# cheap (no thundering herd) without locking in bad data.
_TTL_SCREENER_STALE = 60


def _yf_ticker_for(symbol: str, exchange: str) -> str:
    """Map a TW symbol to its Yahoo Finance ticker. TWSE listed →
    `XXXX.TW`, TPEx → `XXXX.TWO`. Used by the yfinance recovery path
    only; the rest of the codebase keeps the bare 4-digit symbol."""
    suffix = ".TWO" if exchange == "TPEx" else ".TW"
    return f"{symbol}{suffix}"


# ── Diagnostic sink helpers ───────────────────────────────────────


def _record_attempt(
    diagnostic: dict[str, Any] | None, tier: str, outcome: str,
) -> None:
    """Append an attempt record to the screener diagnostic sink (if
    provided). Centralised so every recovery tier records in the same
    shape — `fetch_screener` reads this back to surface on the ctx."""
    if diagnostic is None:
        return
    diagnostic.setdefault("attempts", []).append(
        {"tier": tier, "outcome": outcome},
    )


def _set_final_data_source(
    diagnostic: dict[str, Any] | None, source: str,
) -> None:
    if diagnostic is None:
        return
    diagnostic["final_data_source"] = source


def _seed_diagnostic(
    diagnostic: dict[str, Any] | None,
    *,
    freshness_session: date,
    ohlcv_latest: date | None,
) -> None:
    if diagnostic is None:
        return
    diagnostic.setdefault("freshness_session", freshness_session.isoformat())
    diagnostic.setdefault(
        "ohlcv_latest",
        ohlcv_latest.isoformat() if ohlcv_latest else None,
    )
    diagnostic.setdefault("attempts", [])


# ── Recovery orchestrators ────────────────────────────────────────


async def _recover_screener_via_yfinance(
    *,
    candidates: list[dict[str, Any]],
    detected_session: date,
    expected_today: date,
    limit: int,
    diagnostic: dict[str, Any] | None = None,
) -> list[dict[str, Any]] | None:
    """Last-ditch screener recovery using Yahoo's chart endpoint.

    Yahoo's pipeline is independent of the TWSE OpenAPI data
    warehouse, so when STOCK_DAY_ALL and STOCK_DAY both lag (and the
    daily cron is consequently stuck), yfinance usually still has
    today's close. We:

    1. Build a `.TW`/`.TWO` ticker list from the top-by-|change_pct|
       candidates so the batch call is bounded.
    2. Probe 2330 to confirm Yahoo isn't also lagging the same
       session. If it is, return None and let the caller fall back to
       the labelled-stale path.
    3. Patch the matched rows' price / change / change_pct / volume
       from Yahoo's response, drop rows Yahoo couldn't price (halts,
       delistings), re-rank, and return.

    Returns None on any failure path so the caller's fallback always
    runs — yfinance is recovery, never load-bearing.
    """
    if not candidates:
        return None
    from data.us import yfinance_connector as yf

    # `_independent_source_is_fresh` and `_sanitize_change_pct` live in
    # `tw_market_service` (the staleness suite mocks the former's DB
    # dependency `_bellwether_ohlcv_closes` via `patch.object(svc, ...)`);
    # lazy-importing here keeps that monkey-patch reach intact.
    from services.tw_market_service import (
        _independent_source_is_fresh,
        _sanitize_change_pct,
    )

    # Take the top candidates by |change_pct| from each end plus the
    # bellwether 2330 (always included so the freshness check below
    # can run even if 2330 didn't make either end's cut).
    by_abs = sorted(
        (
            c for c in candidates
            if isinstance(c.get("change_pct"), (int, float))
        ),
        key=lambda c: abs(c["change_pct"]),
        reverse=True,
    )
    pool: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in by_abs:
        sym = row.get("symbol")
        if not sym or sym in seen:
            continue
        pool.append(row)
        seen.add(sym)
        if len(pool) >= max(limit, 50):
            break
    # Force the bellwether in for the freshness probe even if it's
    # outside the top-|change_pct| pool.
    if _STALENESS_BELLWETHER not in seen:
        for row in candidates:
            if row.get("symbol") == _STALENESS_BELLWETHER:
                pool.append(row)
                break

    tickers = [
        _yf_ticker_for(r["symbol"], r.get("exchange") or "TWSE")
        for r in pool
    ]
    try:
        quotes = await yf.get_batch_quotes(tickers)
    except Exception as exc:
        log.warning("tw.screener.yfinance_batch_failed",
                    extra={"error": str(exc)})
        _record_attempt(diagnostic, "yfinance", "error")
        return None
    if not quotes:
        _record_attempt(diagnostic, "yfinance", "empty_response")
        return None

    bell_ticker = _yf_ticker_for(_STALENESS_BELLWETHER, "TWSE")
    yf_2330 = quotes.get(bell_ticker)
    yf_2330_close = (
        float(yf_2330.get("price")) if yf_2330 and yf_2330.get("price") is not None
        else None
    )
    if yf_2330_close is None:
        _record_attempt(diagnostic, "yfinance", "bellwether_missing")
        return None
    if not await _independent_source_is_fresh(yf_2330_close, detected_session):
        log.info("tw.screener.yfinance_also_stale",
                 extra={"detected_session": detected_session.isoformat()})
        _record_attempt(diagnostic, "yfinance", "also_stale")
        return None

    session_stamp = expected_today.isoformat()
    recovered: list[dict[str, Any]] = []
    for row in candidates:
        sym = row.get("symbol")
        if not sym:
            continue
        ticker = _yf_ticker_for(sym, row.get("exchange") or "TWSE")
        yq = quotes.get(ticker)
        if not yq or yq.get("price") is None:
            # Yahoo couldn't price this one (halt, untracked, etc.).
            # Drop it — keeping the stale row would dilute the
            # re-ranking with phantom moves.
            continue
        new_price = float(yq["price"])
        new_change_pct = (
            float(yq["change_pct"])
            if yq.get("change_pct") is not None else None
        )
        new_change = None
        if new_change_pct is not None:
            prev = new_price / (1 + new_change_pct / 100.0) if (
                1 + new_change_pct / 100.0
            ) else None
            new_change = new_price - prev if prev else None
        sanitized = _sanitize_change_pct(sym, new_change_pct)
        if sanitized is None and new_change_pct is not None:
            new_change = None
        new_change_pct = sanitized

        patched = dict(row)
        patched["price"] = new_price
        patched["change"] = new_change
        patched["change_pct"] = new_change_pct
        new_vol = yq.get("volume")
        if new_vol is not None:
            patched["volume"] = int(new_vol)
        patched["actual_session"] = session_stamp
        patched["data_source"] = "yfinance_recovery"
        patched["is_stale"] = False
        recovered.append(patched)

    if not recovered:
        _record_attempt(diagnostic, "yfinance", "no_symbols_matched")
        return None
    # Re-rank by volume desc (same order the live path produces) so
    # downstream gainers/losers logic, which sorts by change_pct,
    # picks the same head/tail it would have on a healthy day.
    recovered.sort(key=lambda r: r.get("volume") or 0, reverse=True)
    _record_attempt(diagnostic, "yfinance", "recovered")
    return recovered[:limit]


async def _recover_screener_via_finmind(
    *,
    candidates: list[dict[str, Any]],
    detected_session: date,
    expected_today: date,
    limit: int,
    diagnostic: dict[str, Any] | None = None,
) -> list[dict[str, Any]] | None:
    """Independent-pipeline recovery via FinMind sponsor's
    `TaiwanStockPrice` market-wide call. Same bellwether-based
    freshness check as the yfinance path — bails when FinMind's 2330
    close matches DB's `detected_session` close (FinMind is also
    stuck on the same session) or when the response is empty (no
    sponsor token / quota exhausted).

    Ordered ahead of yfinance because the user has paid for the
    sponsor tier and FinMind's pipeline is typically more reliable
    for TW data than Yahoo's third-party feed. One TaiwanStockPrice
    call returns ~1700 rows so quota cost is one request regardless
    of candidate count.
    """
    if not candidates:
        _record_attempt(diagnostic, "finmind", "no_candidates")
        return None

    from services.tw_market_service import (
        _independent_source_is_fresh,
        _sanitize_change_pct,
    )

    sess_iso = expected_today.isoformat()
    try:
        rows = await finmind.get_daily_ohlcv_market_wide(sess_iso, sess_iso)
    except Exception as exc:
        log.warning("tw.screener.finmind_batch_failed",
                    extra={"error": str(exc)})
        _record_attempt(diagnostic, "finmind", "error")
        return None
    if not rows:
        _record_attempt(diagnostic, "finmind", "empty_response")
        return None

    by_symbol = {r["stock_id"]: r for r in rows if r.get("stock_id")}
    bell_row = by_symbol.get(_STALENESS_BELLWETHER)
    bell_close = (
        float(bell_row["close"]) if bell_row and bell_row.get("close") is not None
        else None
    )
    if bell_close is None:
        _record_attempt(diagnostic, "finmind", "bellwether_missing")
        return None

    if not await _independent_source_is_fresh(bell_close, detected_session):
        _record_attempt(diagnostic, "finmind", "also_stale")
        return None

    session_stamp = expected_today.isoformat()
    recovered: list[dict[str, Any]] = []
    for row in candidates:
        sym = row.get("symbol")
        if not sym:
            continue
        fm = by_symbol.get(sym)
        if not fm or fm.get("close") is None:
            continue
        new_price = float(fm["close"])
        new_open = float(fm["open"]) if fm.get("open") is not None else None
        new_change = (
            (new_price - new_open) if new_open is not None else None
        )
        new_change_pct = (
            round((new_price - new_open) / new_open * 100, 4)
            if new_open else None
        )
        sanitized = _sanitize_change_pct(sym, new_change_pct)
        if sanitized is None and new_change_pct is not None:
            new_change = None
        new_change_pct = sanitized

        patched = dict(row)
        patched["price"] = new_price
        patched["change"] = new_change
        patched["change_pct"] = new_change_pct
        if fm.get("volume") is not None:
            patched["volume"] = int(fm["volume"])
        patched["actual_session"] = session_stamp
        patched["data_source"] = "finmind_recovery"
        patched["is_stale"] = False
        recovered.append(patched)

    if not recovered:
        _record_attempt(diagnostic, "finmind", "no_symbols_matched")
        return None
    recovered.sort(key=lambda r: r.get("volume") or 0, reverse=True)
    _record_attempt(diagnostic, "finmind", "recovered")
    return recovered[:limit]
