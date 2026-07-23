"""Per-symbol "focus brief" mini analyst-report builders.

A focus brief is the bundle of evidence ``_ask_persona`` lifts into
``ctx["focus_briefs"]`` so personas reasoning about a topic-mentioned
ticker have actual data instead of just headlines:

  - ``quote``         — latest price + change %
  - ``technicals``    — MA20 / 60 / 120, 52w range, 5d / 20d / 60d
                        perf, RSI14
  - ``fundamentals``  — PE / PB / yield / EPS (+ market cap on US)
  - ``revenue_trend`` — last 6 months YoY / MoM (TW only — Taiwan
                        listed companies file monthly revenue)
  - ``chip_5d``       — net foreign / SITC / dealer over 5 trading
                        days (TW only)
  - ``margin_latest`` — latest 融資 / 融券 balance (TW only)
  - ``peers``         — same-industry comparables off the cached
                        screener (TW only, capped at 3)

Three flavours:

  - ``_build_tw_focus_brief``           — live mode, full TW shape
  - ``_build_tw_focus_brief_backtest``  — ``as_of``-aware, reads only
    from ``ohlcv_daily`` so no live data leaks back from the future.
    Live-only blocks (fundamentals / revenue / chip / peers) are
    skipped in v1 — they show as null and personas read that as
    "data not available in backtest mode".
  - ``_build_us_focus_brief``           — quote + technicals +
    fundamentals only. No revenue / chip / peers (the underlying
    data tier doesn't have parity).

Plus the entry point ``_assemble_focus_briefs`` that fans out the
per-symbol builders concurrently and respects the ``_MAX_FOCUS_
SYMBOLS`` token-budget cap, and the helper ``_get_tw_peers`` that
draws same-industry comparables off the cached TW screener.

Each sub-call is wrapped in its own try / except so a single
connector outage doesn't blank the whole brief — the failed block
is just None / [] and the persona reasons with what remained.

discussion_service.py re-exports every public name + the two
``_FOCUS_BRIEF_*`` constants so:
  - ``gather_market_context`` (still in monolith) continues to call
    ``_assemble_focus_briefs`` via its name in the parent module
  - ``discussion/context/blocks/http.py`` lazy-imports
    ``_assemble_focus_briefs`` from ``services.discussion_service``
  - ``tests/test_discussion_context_blocks.py`` and
    ``tests/test_corporate_announcements.py`` patch
    ``services.discussion_service._assemble_focus_briefs`` by
    attribute — patches keep working because the lazy import in
    blocks/http.py reads the (potentially patched) attribute at
    call time.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from typing import Any

from services.discussion.screener_utils import _is_speculative_etf
from services.discussion.symbols import _MAX_FOCUS_SYMBOLS, _crypto_universe
from services.discussion.technicals import (
    _FOCUS_BRIEF_CHIP_DAYS,
    _FOCUS_BRIEF_REVENUE_MONTHS,
    _bar_close,
    _compute_technicals,
    _summarize_institutional,
    _summarize_margin,
    _summarize_revenue,
)

log = logging.getLogger(__name__)


_FOCUS_BRIEF_HISTORY_MONTHS = 12         # enough for 52w high/low
_FOCUS_BRIEF_PEER_COUNT = 3


async def _get_tw_peers(
    *, symbol: str, industry: str | None, limit: int = _FOCUS_BRIEF_PEER_COUNT,
) -> list[dict[str, Any]]:
    """Same-industry comparable set drawn from the cached screener.

    Picks the most-traded peers (proxy for liquidity / market cap)
    and returns a compact `{symbol, name, price, change_pct, pe}`
    record per peer. Empty list when the industry is unknown or the
    screener cache is cold / failing.
    """
    if not industry:
        return []
    try:
        from services import tw_market_service
        rows = await tw_market_service.get_screener(
            limit=400, min_volume=500_000,
        )
    except Exception:
        return []
    candidates = []
    for r in rows:
        sym = r.get("symbol")
        if not sym or sym == symbol:
            continue
        if _is_speculative_etf(sym):
            continue
        if tw_market_service.get_industry(sym) != industry:
            continue
        candidates.append(r)
    candidates.sort(
        key=lambda r: (r.get("volume") or 0), reverse=True,
    )
    out: list[dict[str, Any]] = []
    for r in candidates[:limit]:
        sym = r.get("symbol")
        out.append({
            "symbol":     sym,
            "name":       r.get("name_zh") or tw_market_service.get_company_name(sym),
            "price":      r.get("price"),
            "change_pct": r.get("change_pct"),
            "pe":         r.get("pe_ratio"),
            "data_source": r.get("data_source", "unknown"),
            "as_of":       r.get("actual_session"),
        })
    return out


async def _build_tw_focus_brief(
    symbol: str, *, as_of: date | None = None,
) -> dict[str, Any]:
    """Per-TW-symbol mini analyst report. Each sub-call is wrapped in
    its own try so a single connector outage doesn't blank the whole
    brief — the persona just sees "fundamentals: null" and reasons
    with what remained.

    Doesn't take a `db` session: every sub-call goes through the
    `tw_market_service` autosession variants which open + close
    their own connections. The `db` parameter used to be threaded
    through (and was unused) — dropped in PR #220 so the
    concurrency contract is unambiguous: this builder is safe to
    fan out alongside the parallel `gather_market_context` tasks
    that DO touch the shared `db`.

    `as_of` (PR #224): backtest mode. When set, routes to
    `_build_tw_focus_brief_backtest` which reads only from
    `ohlcv_daily` with `ts <= as_of`. Live-only blocks
    (fundamentals / revenue / chip / peers) are skipped in v1.
    """
    if as_of is not None:
        return await _build_tw_focus_brief_backtest(symbol, as_of=as_of)
    from services import tw_market_service

    brief: dict[str, Any] = {
        "symbol":         symbol,
        "name_zh":        tw_market_service.get_company_name(symbol),
        "industry":       tw_market_service.get_industry(symbol),
        "quote":          None,
        "technicals":     None,
        "fundamentals":   None,
        "revenue_trend":  [],
        "chip_5d":        None,
        "margin_latest":  None,
        "peers":          [],
    }

    # Quote — cached behind Redis 15s, safe to call even mid-round.
    # `get_quote` returns its own `as_of_session` + `is_intraday`
    # stamped from the waterfall tier that actually served the read
    # (MIS intraday / STOCK_DAY_ALL EOD / FinMind fallback / DB
    # snapshot). Forward both through so the persona can see whether
    # the price is true intraday (`is_intraday=True`) or a session
    # close anchored to a date in the past.
    try:
        from services.discussion.freshness import tw_quote_session
        q = await tw_market_service.get_quote(symbol)
        brief["quote"] = {
            "price":      q.get("price"),
            "change_pct": q.get("change_pct"),
            "volume":     q.get("volume"),
            "prev_close": q.get("prev_close"),
            "as_of_session": q.get("as_of_session") or tw_quote_session(),
            "is_intraday": bool(q.get("is_intraday")),
            "data_source": q.get("data_source", "unavailable"),
        }
    except Exception as exc:
        log.warning("focus_brief.quote.failed",
                    extra={"symbol": symbol, "error": str(exc)})

    # History → technicals. 12 months is enough for 52w stats + 60d MA.
    # The bar series feeds `_compute_technicals`; we additionally tag
    # the technicals payload with `as_of_session` = the latest bar's
    # date so personas can tell whether the RSI / MA values are
    # computed against today's close or a prior session.
    try:
        bars = await tw_market_service.get_history(
            symbol, months=_FOCUS_BRIEF_HISTORY_MONTHS,
        )
        technicals = _compute_technicals(bars or [])
        if technicals is not None and bars:
            technicals["as_of_session"] = str(bars[-1].get("time") or "")[:10]
            technicals["is_intraday"] = False
            technicals["data_source"] = bars[-1].get("data_source", "unknown")
        brief["technicals"] = technicals
    except Exception as exc:
        log.warning("focus_brief.history.failed",
                    extra={"symbol": symbol, "error": str(exc)})

    # Fundamentals.
    try:
        f = await tw_market_service.get_fundamentals(symbol)
        if isinstance(f, dict):
            brief["fundamentals"] = {
                "pe":             f.get("pe_ratio"),
                "pb":             f.get("pb_ratio"),
                "dividend_yield": f.get("dividend_yield"),
                "eps":            f.get("eps"),
                "data_source":    f.get("data_source", "unavailable"),
                "as_of":          f.get("fetched_at"),
            }
    except Exception as exc:
        log.warning("focus_brief.fundamentals.failed",
                    extra={"symbol": symbol, "error": str(exc)})

    # Revenue trend (TW-only data — Taiwan listed companies file monthly).
    try:
        rev = await tw_market_service.get_revenue(
            symbol, months=_FOCUS_BRIEF_REVENUE_MONTHS,
        )
        brief["revenue_trend"] = _summarize_revenue(rev or [])
    except Exception as exc:
        log.warning("focus_brief.revenue.failed",
                    extra={"symbol": symbol, "error": str(exc)})

    # Chip metrics (法人 + 融資融券).
    try:
        inst = await tw_market_service.get_institutional(
            symbol, days=_FOCUS_BRIEF_CHIP_DAYS,
        )
        brief["chip_5d"] = _summarize_institutional(inst or [])
    except Exception as exc:
        log.warning("focus_brief.institutional.failed",
                    extra={"symbol": symbol, "error": str(exc)})

    try:
        margin = await tw_market_service.get_margin(
            symbol, days=_FOCUS_BRIEF_CHIP_DAYS,
        )
        brief["margin_latest"] = _summarize_margin(margin or [])
    except Exception as exc:
        log.warning("focus_brief.margin.failed",
                    extra={"symbol": symbol, "error": str(exc)})

    # Peer set — best-effort, off the cached screener.
    try:
        brief["peers"] = await _get_tw_peers(
            symbol=symbol, industry=brief["industry"],
        )
    except Exception as exc:
        log.warning("focus_brief.peers.failed",
                    extra={"symbol": symbol, "error": str(exc)})

    return brief


async def _build_tw_focus_brief_backtest(
    symbol: str, *, as_of: date,
) -> dict[str, Any]:
    """Backtest variant of `_build_tw_focus_brief` (PR #224).

    Reads ONLY from `ohlcv_daily` with `ts <= as_of` so no live data
    leaks back from the future. The synthetic `quote` block carries
    the close on `as_of` (or the most recent bar before it) plus the
    1-day change vs the prior bar. Technicals (MA / 52w / RSI / perf
    %) are computed from the as_of-truncated history.

    Fundamentals ARE included: `fundamentals_snapshots` is keyed on
    `as_of` and `backfill_fundamentals_history` populates past
    sessions point-in-time (valuations from TWSE's dated `BWIBBU_d`,
    statement fields from the quarters closed on/before each day), so
    the historical rows are what was public then. The v1 note that
    "readers don't have an as_of-aware path yet" no longer holds.

    This matters more than it looks: with the block missing, every
    replayed session lost a whole dimension of the panel's reasoning.
    A 2026-05-26 replay abstained citing "fundamentals 與 revenue_trend
    五檔候選股全數空值" while the archive held snapshots for four of
    the five — the emptiness was the reader, not the market. Replays
    were therefore biased toward abstention.

    Chip metrics are included for the same reason: the institutional
    and margin ledgers are published daily by TWSE and never restated,
    so `ts <= as_of` is point-in-time by construction. `chip_5d` is
    load-bearing rather than decorative — it is the core dimension of
    the chip_quality strategy, so replaying that strategy without it
    was grading a panel that could not see its own thesis.

    Still skipped: revenue trend and peers. Revenue is deliberate
    rather than pending — a later backfill can restate
    `tw_revenue_monthly.revenue_yoy` against revised baselines, which
    is why `read_top_revenue_growers` masks it in backtest mode too;
    reading it per-symbol here would reintroduce exactly that leak.
    Peers need live screener state that can't be reconstructed. Those
    show as null, and the personas read them as "not available in
    backtest mode" — better than fabricating values that never
    existed on `as_of`.
    """
    from services import tw_market_service
    from services.ingest.repository import read_ohlcv_range_autosession

    brief: dict[str, Any] = {
        "symbol":         symbol,
        "name_zh":        tw_market_service.get_company_name(symbol),
        "industry":       tw_market_service.get_industry(symbol),
        "quote":          None,
        "technicals":     None,
        "fundamentals":   None,
        "revenue_trend":  [],
        "chip_5d":        None,
        "margin_latest":  None,
        "peers":          [],
        "_backtest":      True,   # marker the prompt template can show
        "_as_of":         as_of.isoformat(),
    }

    try:
        # ~12 months of bars ending at as_of so 52w / MA120 have
        # enough lookback. Slight overshoot fine — `_compute_technicals`
        # is a pure function over closes.
        start = as_of - timedelta(days=400)
        bars = await read_ohlcv_range_autosession("TW", symbol, start, as_of)
    except Exception as exc:
        log.warning("focus_brief.backtest.history.failed",
                    extra={"symbol": symbol, "as_of": as_of.isoformat(), "error": str(exc)})
        bars = []

    if bars:
        technicals = _compute_technicals(bars)
        if technicals is not None:
            technicals["as_of_session"] = str(bars[-1].get("time") or "")[:10]
            technicals["is_intraday"] = False
        brief["technicals"] = technicals
        # Synthetic quote: last close as price; change_pct vs prior bar.
        last = bars[-1]
        prev = bars[-2] if len(bars) >= 2 else None
        last_close = _bar_close(last)
        prev_close = _bar_close(prev) if prev else None
        change_pct = (
            ((last_close - prev_close) / prev_close * 100.0)
            if last_close is not None and prev_close not in (None, 0)
            else None
        )
        brief["quote"] = {
            "price":      last_close,
            "change_pct": round(change_pct, 2) if change_pct is not None else None,
            "volume":     int(last.get("volume") or 0),
            "prev_close": prev_close,
            "as_of_session": str(last.get("time") or "")[:10],
            "is_intraday": False,
        }

    # Fundamentals as they stood on `as_of`. `snap_as_of` is carried
    # through so a persona can see how stale the snapshot is — on a
    # session the ingest job never covered, the nearest earlier row is
    # returned rather than nothing.
    try:
        from services.ingest.repository import read_fundamentals_as_of_autosession

        f = await read_fundamentals_as_of_autosession("TW", symbol, as_of=as_of)
        if isinstance(f, dict):
            brief["fundamentals"] = {
                "pe":             f.get("pe_ratio"),
                "pb":             f.get("pb_ratio"),
                "dividend_yield": f.get("dividend_yield"),
                "eps":            f.get("eps"),
                "data_source":    f.get("data_source", "unavailable"),
                "as_of":          f.get("as_of"),
            }
    except Exception as exc:
        log.warning("focus_brief.backtest.fundamentals.failed",
                    extra={"symbol": symbol, "as_of": as_of.isoformat(),
                           "error": str(exc)})

    # Chip metrics as of that session. Institutional and margin ledgers
    # are published daily by TWSE and never restated, so reading them
    # at `ts <= as_of` is point-in-time by construction — the property
    # that rules out `revenue_yoy`, which a later backfill recomputes.
    #
    # Note on the parameter: callers pass `info_cutoff` here, i.e.
    # `prev_trading_day(discussion.as_of_date)`, not the session being
    # predicted (see `context.builder`). So `end=as_of` already stops a
    # day short of the graded session — the ledger for the session
    # itself is never in range.
    #
    # `chip_5d` is not a nice-to-have here: it is the core dimension of
    # the chip_quality strategy, so replaying that strategy without it
    # was grading a panel that couldn't see its own thesis.
    chip_start = as_of - timedelta(days=_FOCUS_BRIEF_CHIP_DAYS * 3)
    try:
        from services.ingest.repository import (
            read_institutional_range_autosession,
        )
        inst = await read_institutional_range_autosession(
            "TW", symbol, chip_start, as_of,
        )
        # Same tail-window the live path takes, applied after the
        # as_of clamp so the calendar-day padding above can't leak.
        brief["chip_5d"] = _summarize_institutional(
            (inst or [])[-_FOCUS_BRIEF_CHIP_DAYS:],
        )
    except Exception as exc:
        log.warning("focus_brief.backtest.institutional.failed",
                    extra={"symbol": symbol, "as_of": as_of.isoformat(),
                           "error": str(exc)})

    try:
        from services.ingest.repository import read_margin_range_autosession

        margin = await read_margin_range_autosession(
            "TW", symbol, chip_start, as_of,
        )
        brief["margin_latest"] = _summarize_margin(
            (margin or [])[-_FOCUS_BRIEF_CHIP_DAYS:],
        )
    except Exception as exc:
        log.warning("focus_brief.backtest.margin.failed",
                    extra={"symbol": symbol, "as_of": as_of.isoformat(),
                           "error": str(exc)})
    return brief


async def _build_us_focus_brief(symbol: str) -> dict[str, Any]:
    """US-side equivalent — quote + technicals + fundamentals only.
    No revenue / chip / peers because the underlying data tier
    doesn't have parity with TW (no monthly revenue feed, no
    foreign-investor ledger, no industry-tagged screener)."""
    from services import us_market_service

    brief: dict[str, Any] = {
        "symbol":       symbol,
        "name":         None,
        "industry":     None,
        "quote":        None,
        "technicals":   None,
        "fundamentals": None,
    }
    try:
        from services.discussion.freshness import (
            us_quote_is_intraday,
            us_quote_session,
        )
        q = await us_market_service.get_quote(symbol)
        brief["quote"] = {
            "price":      q.get("price"),
            "change_pct": q.get("change_pct"),
            "volume":     q.get("volume"),
            "prev_close": q.get("prev_close"),
            # US live quote via yfinance / Polygon / Stooq / Finnhub
            # waterfall — intraday when NYSE is in session, otherwise
            # last close.
            "as_of_session": us_quote_session(),
            "is_intraday": us_quote_is_intraday(),
        }
    except Exception as exc:
        log.warning("focus_brief.us_quote.failed",
                    extra={"symbol": symbol, "error": str(exc)})

    try:
        bars = await us_market_service.get_history(symbol, period="1y", interval="1d")
        technicals = _compute_technicals(bars or [])
        if technicals is not None and bars:
            technicals["as_of_session"] = str(bars[-1].get("time") or "")[:10]
            technicals["is_intraday"] = False
        brief["technicals"] = technicals
    except Exception as exc:
        log.warning("focus_brief.us_history.failed",
                    extra={"symbol": symbol, "error": str(exc)})

    try:
        f = await us_market_service.get_fundamentals(symbol)
        if isinstance(f, dict):
            brief["name"] = f.get("name")
            brief["industry"] = f.get("industry") or f.get("sector")
            brief["fundamentals"] = {
                "pe":             f.get("pe_ratio"),
                "pb":             f.get("pb_ratio"),
                "dividend_yield": f.get("dividend_yield"),
                "eps":            f.get("eps"),
                "market_cap":     f.get("market_cap"),
            }
    except Exception as exc:
        log.warning("focus_brief.us_fundamentals.failed",
                    extra={"symbol": symbol, "error": str(exc)})

    return brief


async def _assemble_focus_briefs(
    *, market: str, symbols: list[str], as_of: date | None = None,
) -> list[dict[str, Any]]:
    """Fan out per-symbol brief assembly concurrently. Cap at
    `_MAX_FOCUS_SYMBOLS` for token-budget protection.

    No `db` param: both `_build_tw_focus_brief` and
    `_build_us_focus_brief` use their respective service modules'
    autosession helpers, so this fan-out is safe to run alongside
    the shared-`db` reads in `gather_market_context` (PR #222
    cleanup; the dead param was removed for a clearer concurrency
    contract).

    `as_of` (PR #224): backtest mode. TW route uses the historical
    `_build_tw_focus_brief_backtest` variant. US route currently
    has no backtest variant — backtests on US discussions return
    empty briefs in v1.
    """
    if not symbols:
        return []
    syms = symbols[:_MAX_FOCUS_SYMBOLS]
    if market == "TW":
        coros = [_build_tw_focus_brief(s, as_of=as_of) for s in syms]
    elif market == "US":
        # No US backtest variant in v1 — degrade to empty briefs.
        if as_of is not None:
            return []
        coros = [_build_us_focus_brief(s) for s in syms]
    else:
        # GLOBAL — fall back to US shape for ASCII-letter symbols, TW
        # shape for digit-only. Crypto symbols (BTC/ETH/...) would
        # also land in the US branch but their fundamentals path
        # doesn't apply; the personas already see them via the
        # crypto news block, so we skip them here to avoid faking
        # equity-style PE/PB.
        coros = []
        for s in syms:
            if s.isdigit():
                coros.append(_build_tw_focus_brief(s, as_of=as_of))
            elif s in _crypto_universe():
                continue
            elif as_of is None:
                # US backtest currently unavailable — skip in v1.
                coros.append(_build_us_focus_brief(s))
    if not coros:
        return []
    results = await asyncio.gather(*coros, return_exceptions=True)
    out: list[dict[str, Any]] = []
    for r in results:
        if isinstance(r, Exception):
            log.warning("focus_brief.fan_out.failed", extra={"error": str(r)})
            continue
        out.append(r)
    return out
