"""
Taiwan market service — owns all caching and waterfall fallback logic.
Data source priority (all preceded by Redis + the local ohlcv_daily /
tw_*_daily archive; the orders below are for the live upstream tiers):
  quote/OHLCV    : token set → FinMind → TWSE ; else TWSE → FinMind
                   (intraday TWSE MIS stays Tier 0 — FinMind is EOD-only)
  institutional  : FinMind → TWSE today-only
  margin         : FinMind → TWSE today-only
  monthly revenue: FinMind → MOPS scraper
  financials     : FinMind only

FinMind-first for quote/OHLCV is gated on a configured FinMind token
(`_finmind_preferred`): a paid sponsor key serves cleaner EOD data (no
STOCK_DAY_ALL publication lag), while tokenless / free deploys keep TWSE
first to protect the limited free hourly quota.

Timezone: Taiwan is UTC+8, no DST. All API responses are tagged with
tz="Asia/Taipei" so the frontend can display correct local labels.
"""
import json
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytz

import data.tw.finmind_connector as finmind
import data.tw.mops_connector as mops
import data.tw.twse_connector as twse
import data.tw.twse_mis_connector as twse_mis
from cache.redis_cache import (
    cache_get,
    cache_set,
    key_history,
    key_institutional,
    key_margin,
    key_quote,
    key_revenue,
)

log = logging.getLogger(__name__)

_TW = pytz.timezone("Asia/Taipei")

# TTLs live in `cache.cache_ttls` so all three market services tune
# from the same place. Aliases preserve the existing call-site shape
# inside this module.
from cache.cache_ttls import (  # noqa: E402
    TTL_FUNDAMENTALS,
    TTL_HISTORY_DAILY as TTL_HISTORY,
    TTL_INSTITUTIONAL,
    TTL_MARGIN,
    TTL_QUOTE_TW as TTL_QUOTE,
    TTL_REVENUE,
    TTL_SCREENER,
)

# Symbol-map state + lookups live in `tw_symbol_service` (PR-A split).
# Re-exported here so external callers that grab `_exchange_map` /
# `_industry_map` / `_name_map` directly off this module
# (`tasks/ingest_ohlcv_tw.py`, `tasks/ingest_revenue_tw_slow.py`,
# `scripts/backfill_ohlcv_tw.py`, `api/tw_market/router.py`) keep
# working unchanged. The dicts are mutated in place over there so
# the identity these callers captured at import time stays live
# across every refresh.
from services.tw_symbol_service import (  # noqa: E402, F401
    _ETF_CODE,
    _SYMBOL_MAP_CACHE_KEY,
    _SYMBOL_MAP_CACHE_TTL,
    _exchange_map,
    _industry_map,
    _name_map,
    _persist_symbol_map_to_cache,
    _persist_symbol_map_to_db,
    find_symbol_by_name_in_text,
    find_symbols_by_names_in_text,
    get_company_name,
    get_exchange,
    get_industry,
    get_industry_peers,
    is_etf,
    load_symbol_map_from_cache,
    load_symbol_map_from_db,
    refresh_symbol_map,
)


def _is_tw_market_open() -> bool:
    now = datetime.now(_TW)
    if now.weekday() >= 5:
        return False
    open_  = now.replace(hour=9,  minute=0,  second=0, microsecond=0)
    close_ = now.replace(hour=13, minute=30, second=0, microsecond=0)
    return open_ <= now < close_


def _today_str() -> str:
    return date.today().isoformat()


def _start_date(months: int = 6) -> str:
    return (date.today() - timedelta(days=months * 30)).isoformat()




# ── Quote ─────────────────────────────────────────────────────────

# prev_close resolution lives in `tw_prev_close` (PR-B split). Re-exported
# here so external callers (notably `test_tw_market_service_waterfall.py`
# which invokes `svc._resolve_prev_close` directly) and the in-module
# consumer (`fetch_quote_waterfall` below) keep their existing import
# paths. `fetch_quote_waterfall` itself stays in this module because the
# test suite patches `_is_tw_market_open` / `_finmind_preferred` via
# `tw_market_service` and a cross-module move would invalidate ~5
# patch.object call sites that drive the get_quote / get_history flows.
from services.tw_prev_close import (  # noqa: E402, F401
    _archive_last2_closes,
    _finmind_prev_close,
    _pick_prev_from_pairs,
    _resolve_prev_close,
)


async def _finmind_preferred() -> bool:
    """True when a FinMind token is configured (= paid sponsor key) — the
    operator wants FinMind as the preferred upstream for quote / history
    because the sponsor EOD data is cleaner than TWSE STOCK_DAY_ALL (no
    14:30 publication lag, no KY-stock Change-field +992% quirk). Tokenless
    / free deploys return False and keep the TWSE-first order so they don't
    burn the limited free hourly quota on every read. All-error → False so
    a transient resolver outage can't flip the order unexpectedly."""
    try:
        return bool(await finmind._get_token())
    except Exception:
        return False


async def fetch_quote_waterfall(symbol: str) -> tuple[dict | None, str]:
    """Run the TWSE MIS intraday → TWSE STOCK_DAY_ALL → FinMind 7-day
    fallback waterfall.

    Returns (raw, source). Pulled out of get_quote() so the background
    WS-publish task can reuse the same fallback chain — without this the
    polling task only tried TWSE and gave up, freezing every subscriber's
    live price whenever TWSE OpenAPI hiccupped.

    Tier 0 (MIS intraday) only fires during TWSE regular session
    (09:00-13:30 Taipei) — the endpoint returns stale ticks pre-open /
    weekend and we'd burn an HTTP RTT for nothing. Outside the session
    the next tier (STOCK_DAY_ALL) serves the previous session's close,
    which is the correct anchor for pre-open / post-close discussions.
    """
    raw = None
    source = "unavailable"
    # ── Tier 0: TWSE MIS intraday (only during regular session) ──
    # Delayed real-time (~20s lag). Skipped outside 09:00-13:30 Taipei
    # because MIS returns yesterday's last value pre-open and the
    # existing tiers cover that case more cleanly. Defensive: any
    # failure (network, 403 from missing JSESSIONID cookie warm-up,
    # non-zero rtcode) falls through silently to Tier 1.
    if _is_tw_market_open():
        try:
            mis_raw = await twse_mis.get_realtime_quote(
                symbol, exchange=get_exchange(symbol),
            )
            if mis_raw and mis_raw.get("close") is not None:
                raw = mis_raw
                source = "twse_mis"
        except Exception as exc:
            log.warning("tw.quote.mis_failed",
                        extra={"symbol": symbol, "error": str(exc)})

    # ── EOD upstream tiers: TWSE STOCK_DAY_ALL + FinMind 7-day bar ──
    # Order flips on FinMind token presence (`_finmind_preferred`): a paid
    # sponsor key makes FinMind the preferred upstream (cleaner EOD data,
    # no STOCK_DAY_ALL publication lag / KY-stock Change-field quirk);
    # tokenless / free deploys keep TWSE first to protect the limited free
    # hourly quota. Each tier is a soft-fail attempt returning (raw, source).
    async def _try_twse_eod() -> tuple[dict | None, str]:
        try:
            r = await twse.get_realtime_quote(symbol)
            if r:
                return r, "twse"
        except Exception as exc:
            log.warning("tw.quote.twse_failed",
                        extra={"symbol": symbol, "error": str(exc)})
        return None, "unavailable"

    async def _try_finmind_eod() -> tuple[dict | None, str]:
        # Latest close from the last few days. End-of-day data, not
        # realtime — the UI flags it via data_source="finmind". Pull the
        # prior-day close so change / change_pct can be computed downstream
        # (without it the watchlist UI shows a blank 漲跌 column).
        try:
            start = (date.today() - timedelta(days=7)).isoformat()
            bars = await finmind.get_daily_ohlcv(symbol, start)
            if bars:
                latest = bars[-1]
                prev_close = bars[-2]["close"] if len(bars) >= 2 else None
                latest_close = latest["close"]
                change = (
                    latest_close - prev_close
                    if (latest_close is not None and prev_close is not None)
                    else None
                )
                return {
                    "symbol": symbol, "name_zh": "",
                    "close": latest_close,
                    "prev_close": prev_close,
                    "change": change,
                    "volume": latest["volume"],
                    "open": latest["open"], "high": latest["high"],
                    "low": latest["low"],
                }, "finmind"
        except Exception as exc:
            log.warning("tw.quote.finmind_failed",
                        extra={"symbol": symbol, "error": str(exc)})
        return None, "unavailable"

    if not raw:
        order = (
            [_try_finmind_eod, _try_twse_eod]
            if await _finmind_preferred()
            else [_try_twse_eod, _try_finmind_eod]
        )
        for _attempt in order:
            raw, source = await _attempt()
            if raw:
                break

    if not raw:
        log.warning("tw.quote.all_sources_failed", extra={"symbol": symbol})
        return raw, source

    # Backfill prev_close from the OHLCV archive when upstream didn't
    # provide one (TWSE realtime never does; FinMind already pre-fills
    # via bars[-2] above). Lets `_normalize_quote` derive change from
    # the archive's prior-day close instead of the upstream's
    # potentially-wrong `Change` field — the +992% / +996% bug we've
    # been bandaging with sanity bounds is the symptom of trusting
    # that field for stocks where TWSE serves it incorrectly.
    if not raw.get("prev_close"):
        # Pass upstream's close so the resolver can tell whether this
        # is a fresh intraday tick (use archive[-1] as prev) vs a
        # stale weekend/holiday snapshot of the last session (use
        # archive[-2] as prev). Without the comparison, weekend
        # views would show 漲幅 0% — TWSE serves Friday's close on
        # Saturday and we'd compare it against Friday's archive bar.
        upstream_close = raw.get("close")
        try:
            upstream_close_f = (
                float(upstream_close) if upstream_close is not None else None
            )
        except (TypeError, ValueError):
            upstream_close_f = None
        prev = await _resolve_prev_close(symbol, upstream_close_f)
        if prev is not None:
            raw["prev_close"] = prev

    return raw, source


async def get_quote(
    symbol: str, *, bypass_cache: bool = False,
    as_of: date | None = None,
) -> dict[str, Any]:
    """Read TW quote. Hits Redis cache first unless `bypass_cache` is
    set — caller asks for fresh data when the user explicitly opens
    a view that promises live prices (e.g. watchlist page on mount).
    Cache TTL is 15 s so the bypass mostly matters off-hours when the
    cache otherwise persists for a full TTL.

    `as_of` (PR #226): backtest mode. When set, bypasses live waterfall
    and reads close/prev_close from `ohlcv_daily` at the most recent bar
    with `ts <= as_of`. Returns the live-mode shape so callers don't
    branch. Empty dict when the archive has no bar in range (caller
    already treats blank quotes as "no signal")."""
    from services.ingest.repository import (
        read_latest_quote_autosession,
        read_ohlcv_range_autosession,
    )

    if as_of is not None:
        from datetime import timedelta as _td
        bars = await read_ohlcv_range_autosession(
            "TW", symbol, as_of - _td(days=10), as_of,
        )
        if not bars:
            return {}
        last = bars[-1]
        prev = bars[-2]["close"] if len(bars) >= 2 else None
        result = _normalize_quote(symbol, {
            "close":      last.get("close"),
            "volume":     last.get("volume", 0),
            "prev_close": prev,
        })
        result["data_source"] = "ohlcv_daily"
        result["as_of"] = last["time"]
        # Backtest: the read is clamped to `ts <= as_of`, so the actual
        # session is the last bar's date. Surfaced under both names so
        # the freshness convention matches live mode + downstream
        # focus_briefs.quote stamping reads the same key.
        result["as_of_session"] = str(last["time"])[:10]
        result["is_intraday"] = False
        return result

    key = key_quote("tw", symbol)
    if not bypass_cache:
        cached = await cache_get(key)
        if cached:
            return json.loads(cached)

    # Closed-market settled-session self-heal (mirrors the screener
    # fast-path at `get_screener`): when TW is not trading and
    # `ohlcv_daily` already holds the latest complete session, serve
    # that settled close instead of leaning on the live STOCK_DAY_ALL
    # feed, whose recency depends on end-of-day publication lag. This
    # makes pre-market / after-hours / weekend quotes deterministic
    # (the value matches the live close anyway, just guaranteed) so a
    # 04:00-Taipei discussion run reads the prior session without a
    # backtest anchor. Falls through to the live waterfall when the
    # archive hasn't caught up yet (ingest lag) — that's the genuine
    # "missing today's data → fall back to live" path.
    if not _is_tw_market_open():
        ohlcv_latest = await get_latest_ohlcv_session()
        if ohlcv_latest is not None and ohlcv_latest >= _latest_complete_session():
            bars = await read_ohlcv_range_autosession(
                "TW", symbol, ohlcv_latest - timedelta(days=10), ohlcv_latest,
            )
            if bars:
                last = bars[-1]
                prev = bars[-2]["close"] if len(bars) >= 2 else None
                result = _normalize_quote(symbol, {
                    "close":      last.get("close"),
                    "volume":     last.get("volume", 0),
                    "prev_close": prev,
                })
                result["data_source"] = "ohlcv_daily_today"
                result["as_of_session"] = str(last["time"])[:10]
                result["is_intraday"] = False
                if result.get("price"):
                    await cache_set(key, json.dumps(result), TTL_QUOTE)
                return result

    raw, source = await fetch_quote_waterfall(symbol)
    if not raw:
        # ── Tier 3: recent DB snapshot when upstream is fully down ──
        # Refresh task writes one row per active symbol every minute, so
        # a 5-minute window catches at most a few stale ticks during a
        # transient TWSE+FinMind outage instead of returning blank state.
        snap = await read_latest_quote_autosession("TW", symbol, max_age_seconds=300)
        if snap is not None:
            result = _normalize_quote(symbol, {
                "close": snap["price"],
                "volume": snap.get("volume", 0),
                "prev_close": snap.get("prev_close"),
            })
            result["data_source"] = snap.get("data_source") or "db"
            # Re-validate the snapshot's stored change_pct: rows
            # written before the sanity bound landed (PR #142) carry
            # the +992% / +996% junk values that motivated the bound
            # in the first place. Without this re-check the snapshot
            # tier silently leaks them back to the UI whenever upstream
            # is briefly unreachable.
            result["change_pct"] = _sanitize_change_pct(
                symbol, snap.get("change_pct"),
            )
            # Snapshot rows are written by the WS pump / refresh tasks
            # at whatever cadence they ran; we don't know if the tick
            # came from MIS or STOCK_DAY_ALL. Use the conservative
            # STOCK_DAY_ALL anchor so personas don't over-trust a
            # potentially stale 5-min-old reading as "intraday".
            _stamp_quote_freshness(result, "db", None)
            log.info("tw.quote.served_db_snapshot", extra={"symbol": symbol})
            return result

    result = _normalize_quote(symbol, raw or {})
    result["data_source"] = source
    _stamp_quote_freshness(result, source, raw)
    # Don't cache the zero-state (TWSE + FinMind both failed) — keeps the
    # next request retrying instead of locking a 60-second blank quote.
    if result.get("price"):
        await cache_set(key, json.dumps(result), TTL_QUOTE)
    return result


def _stamp_quote_freshness(
    result: dict, source: str, raw: dict | None,
) -> None:
    """Attach `is_intraday` + `as_of_session` to a live-mode quote
    dict so downstream callers (focus_briefs, watchlist, UI) can
    surface the actual session anchor instead of relying on
    wall-clock NOW.

    Per source:
      - `twse_mis`: true-intraday during TWSE regular session.
        `as_of_session = today` (Taipei).
      - `twse` (STOCK_DAY_ALL): EOD cross-section. `as_of_session =
        tw_quote_session()` — yesterday's close during intraday /
        pre-publish, today's close after 14:30.
      - `finmind`: 7-day bar fallback. Use bars[-1]["time"] if
        present on the raw dict, else `tw_quote_session()`.
      - `db` / `unavailable`: 5-min DB snapshot or full outage —
        fall back to the `STOCK_DAY_ALL` semantic since that's the
        closest in-practice anchor; the data_source field still
        tells the caller about the degraded tier.
    """
    from services.discussion.freshness import tw_quote_session
    if source == "twse_mis":
        result["is_intraday"] = True
        result["as_of_session"] = _today_str_tw()
    elif source == "finmind" and raw:
        ts = raw.get("time") or raw.get("date")
        result["is_intraday"] = False
        result["as_of_session"] = (
            str(ts)[:10] if ts else tw_quote_session()
        )
    else:
        result["is_intraday"] = False
        result["as_of_session"] = tw_quote_session()


def _today_str_tw() -> str:
    """Today's date in Taipei (so a discussion fired 00:30 UTC = 08:30
    Taipei sees the right calendar day for intraday stamping).
    """
    return datetime.now(_TW).date().isoformat()


# Sanity bound moved to `services._quote_helpers` so US can share it
# (Polygon / yfinance hit the same upstream-garbage failure modes the
# TW KY-listed +992% bug came from). Local alias keeps the existing
# call-site shape inside this module.
from services._quote_helpers import sanitize_change_pct as _shared_sanitize_change_pct  # noqa: E402


def _sanitize_change_pct(
    symbol: str, chg_pct: float | None,
) -> float | None:
    return _shared_sanitize_change_pct(symbol, "TW", chg_pct)


def _normalize_quote(symbol: str, raw: dict) -> dict[str, Any]:
    close = raw.get("close") or raw.get("price", 0) or 0
    prev  = raw.get("prev_close", 0) or 0
    # Compute change strictly from prev_close (set by upstream OR
    # backfilled from the OHLCV archive / FinMind in
    # `fetch_quote_waterfall`). NEVER fall back to upstream's `change`
    # field: TWSE's STOCK_DAY_ALL stuffs the prior-day close into the
    # `Change` slot for some KY-listed stocks (4958, 2455, …), so
    # trusting it derives prev = close - prior_close ≈ today's move,
    # producing +992% / +996% headlines. Better to surface a blank
    # delta + blank 昨收 (truthful "we don't know yet") than to leak
    # garbage past the sanity bound.
    if prev and close:
        chg: float | None = close - prev
    else:
        chg = None
    chg_pct = round(chg / prev * 100, 4) if (chg is not None and prev) else None
    chg_pct = _sanitize_change_pct(symbol, chg_pct)
    if chg_pct is None:
        # Drop the raw delta too — keeping `change` populated while
        # `change_pct` is None would let the screener render an
        # un-percentaged delta that doesn't match the rest of the row.
        chg = None
    return {
        "symbol":        symbol,
        "market":        "TW",
        "exchange":      get_exchange(symbol),
        "name_zh":       raw.get("name_zh", ""),
        "price":         close,
        "change":        chg,
        "change_pct":    chg_pct,
        "volume":        raw.get("volume", 0),
        "open":          raw.get("open"),
        "high":          raw.get("high"),
        "low":           raw.get("low"),
        "currency":      "TWD",
        "ts":            int(datetime.now(UTC).timestamp() * 1000),
        "tz":            "Asia/Taipei",
        "is_market_open": _is_tw_market_open(),
        "is_etf":        is_etf(symbol),
    }


# ── History ───────────────────────────────────────────────────────


def _expected_history_session() -> date:
    """Most recent TW trading session whose bar SHOULD be in
    `ohlcv_daily` by now. After TWSE's 14:30 Taipei publish (and
    after `ingest_ohlcv_tw` 06:30 UTC = 14:30 Taipei runs), today's
    bar is expected; before, the freshest expected bar is the
    previous trading day.

    Distinct from `tw_quote_session()` in `discussion/freshness`
    semantically — that helper anchors `STOCK_DAY_ALL`-served quotes,
    this one anchors `ohlcv_daily` archive completeness. They happen
    to coincide because both pivot on the 14:30 Taipei publish, but
    the intent is different.
    """
    from services.discussion.freshness import tw_quote_session
    return date.fromisoformat(tw_quote_session())


def _db_bars_are_fresh(
    bars: list[dict[str, Any]], today: date,
) -> bool:
    """True iff `bars[-1]` covers the most recent expected trading
    session.

    Pre-Phase-3 this allowed up to `_DB_HISTORY_FRESHNESS_DAYS=5`
    of staleness — which silently masked the bug where a delayed
    `ingest_ohlcv_tw` cron made today's discussion run against
    yesterday's close. The tightened rule forces a fall-through to
    the TWSE month-by-month tier whenever the archive is missing
    the expected session, so today's bar lands within one extra
    TWSE call instead of waiting for the next cron tick.

    Weekends + holidays still pass cleanly: `_expected_history_
    session()` already walks back to the previous weekday on
    non-trading days, so the archive's "freshest weekday close"
    matches.

    `today` is accepted for backwards compatibility with the
    existing call signature — the actual freshness check uses the
    derived expected session, not `today` directly.
    """
    if not bars:
        return False
    last = bars[-1].get("time")
    if not last:
        return False
    try:
        last_date = date.fromisoformat(str(last)[:10])
    except ValueError:
        return False
    expected = _expected_history_session()
    return last_date >= expected


async def get_history(symbol: str, months: int = 12) -> list[dict[str, Any]]:
    from services.ingest.repository import (
        OhlcvBar,
        read_ohlcv_range_autosession,
        upsert_ohlcv_bars_autosession,
    )

    key = key_history("tw", symbol, "1d", range_token=f"{months}mo")
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)

    today = date.today()
    start = today - timedelta(days=months * 30)

    # Synthetic indices (`_TAIEX`, `_TAIEX_TR`) live only in the
    # `ohlcv_daily` archive — they're populated by dedicated cron
    # jobs, not the per-symbol upstream waterfall. Falling through
    # to TWSE / FinMind for these would 404 (TWSE OpenAPI doesn't
    # accept index codes via `STOCK_DAY`, FinMind's per-symbol
    # `TaiwanStockPrice` doesn't either). Short-circuit + return
    # whatever's in the archive.
    if symbol.startswith("_"):
        index_bars = await read_ohlcv_range_autosession("TW", symbol, start, today)
        if index_bars:
            await cache_set(key, json.dumps(index_bars), TTL_HISTORY)
        return index_bars

    # ── Tier 2: Postgres archive ────────────────────────────────
    db_bars = await read_ohlcv_range_autosession("TW", symbol, start, today)
    if _db_bars_are_fresh(db_bars, today):
        await cache_set(key, json.dumps(db_bars), TTL_HISTORY)
        return db_bars

    # ── Tier 3: upstream waterfall ──
    # Order flips on FinMind token presence (`_finmind_preferred`): with a
    # paid sponsor key FinMind goes first — one range call returns the whole
    # window vs TWSE's month-by-month loop (up to 12 calls), so it's both the
    # cleaner source and the cheaper one. Tokenless / free deploys keep TWSE
    # first to protect the limited free hourly quota.
    async def _twse_months() -> tuple[list[dict], str]:
        out: list[dict] = []
        for i in range(months):
            d = today.replace(day=1) - timedelta(days=i * 30)
            month_bars = await twse.get_daily_ohlcv(symbol, d)
            out = month_bars + out
        return out, "twse"

    async def _finmind_range() -> tuple[list[dict], str]:
        return await finmind.get_daily_ohlcv(symbol, start.isoformat()), "finmind"

    order = (
        [_finmind_range, _twse_months]
        if await _finmind_preferred()
        else [_twse_months, _finmind_range]
    )
    bars: list[dict] = []
    upstream_source = "twse"
    for _fetch in order:
        try:
            bars, upstream_source = await _fetch()
        except Exception:
            bars = []
        if bars:
            break

    if bars:
        await cache_set(key, json.dumps(bars), TTL_HISTORY)
        # Best-effort write-back into the archive so subsequent cache
        # misses serve from DB and reduce upstream load.
        ohlcv_bars = [
            b for b in (
                OhlcvBar.from_connector_row("TW", symbol, upstream_source, r)
                for r in bars
            )
            if b is not None
        ]
        await upsert_ohlcv_bars_autosession(ohlcv_bars)
        return bars

    # ── Tier 4: stale DB beats nothing during full upstream outage ──
    if db_bars:
        log.info("tw.history.served_stale_db",
                 extra={"symbol": symbol, "rows": len(db_bars)})
        return db_bars

    return []


# ── Institutional investors ───────────────────────────────────────

async def get_institutional(
    symbol: str, days: int = 30, *, as_of: date | None = None,
) -> list[dict[str, Any]]:
    """
    法人買賣超 read tier:
        Redis cache  →  DB (tw_institutional_daily, populated by
        the daily TWSE ingest)  →  live FinMind per-symbol  →
        live TWSE today-only fallback.

    DB tier serves the typical 30-day query in one indexed range scan
    so the per-stock detail page and the discussion subsystem don't
    burn FinMind quota for every read.

    `as_of` (backtest mode): when set, anchor the window at `as_of`
    instead of today, bypass Redis (per-as_of cache key would explode),
    and skip the live FinMind / TWSE fallbacks (those would return
    today's data, polluting a backtest with future information).
    Returns whatever the DB archive holds, or [] when the historical
    range is empty.
    """
    from services.ingest.repository import read_institutional_range

    if as_of is None:
        key = key_institutional(symbol)
        cached = await cache_get(key)
        if cached:
            return json.loads(cached)

    end = as_of or date.today()
    start = end - timedelta(days=days)

    # ── Tier 2: Postgres archive ────────────────────────────────
    try:
        from db.session import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            db_rows = await read_institutional_range(
                db, "TW", symbol, start, end,
            )
        if db_rows:
            if as_of is None:
                await cache_set(
                    key_institutional(symbol),
                    json.dumps(db_rows),
                    TTL_INSTITUTIONAL,
                )
            return db_rows
    except Exception:
        # Fall through to live waterfall — never let DB outage hide
        # data that the upstream can still produce.
        pass

    if as_of is not None:
        # Backtest mode: live tiers would leak future data — return
        # whatever the archive had (possibly empty) and let the
        # caller treat it as "no signal in window".
        return []

    result: list[dict] = []

    # TWSE returns all stocks for one day; we'd need to call per day
    # so default to FinMind which returns per-symbol range
    try:
        result = await finmind.get_institutional(symbol, start.isoformat())
    except Exception:
        pass

    if not result:
        # TWSE fallback: today only
        try:
            all_rows = await twse.get_institutional()
            result = [r for r in all_rows if r.get("symbol") == symbol]
        except Exception:
            pass

    if result:
        await cache_set(
            key_institutional(symbol), json.dumps(result), TTL_INSTITUTIONAL,
        )
    return result


# ── Margin balance ────────────────────────────────────────────────

async def get_margin(
    symbol: str, days: int = 30, *, as_of: date | None = None,
) -> list[dict[str, Any]]:
    """融資融券 read tier — same shape as `get_institutional`:
    Redis → Postgres `tw_margin_daily` → FinMind → TWSE today-only.

    `as_of` (backtest mode): same semantics as `get_institutional` —
    anchor at `as_of`, bypass cache, skip live fallbacks (would leak
    future data), return DB-archive rows or [] on empty range.
    """
    from services.ingest.repository import read_margin_range

    if as_of is None:
        key = key_margin(symbol)
        cached = await cache_get(key)
        if cached:
            return json.loads(cached)

    end = as_of or date.today()
    start = end - timedelta(days=days)

    try:
        from db.session import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            db_rows = await read_margin_range(db, "TW", symbol, start, end)
        if db_rows:
            if as_of is None:
                await cache_set(
                    key_margin(symbol), json.dumps(db_rows), TTL_MARGIN,
                )
            return db_rows
    except Exception:
        pass

    if as_of is not None:
        return []

    result: list[dict] = []
    try:
        result = await finmind.get_margin(symbol, start.isoformat())
    except Exception:
        pass

    if not result:
        try:
            all_rows = await twse.get_margin()
            result = [r for r in all_rows if r.get("symbol") == symbol]
        except Exception:
            pass

    if result:
        await cache_set(key_margin(symbol), json.dumps(result), TTL_MARGIN)
    return result


# ── Monthly revenue (月營收) ──────────────────────────────────────

async def get_revenue(symbol: str, months: int = 12) -> list[dict[str, Any]]:
    """月營收 read tier:
        Redis cache  →  DB (tw_revenue_monthly, populated by the
        daily FinMind market-wide ingest)  →  live FinMind per-
        symbol  →  MOPS scrape (current month only).

    DB tier saves the per-stock detail page and discussion
    aggregator from burning FinMind quota every read. Sourced from
    the same FinMind dataset, so DB and live values are equivalent.
    """
    from services.ingest.repository import read_revenue_range

    key = key_revenue(symbol)
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)

    today = date.today()
    start = today - timedelta(days=months * 31)

    # ── Tier 2: Postgres archive ────────────────────────────────
    try:
        from db.session import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            db_rows = await read_revenue_range(db, "TW", symbol, start, today)
        if db_rows:
            await cache_set(key, json.dumps(db_rows), TTL_REVENUE)
            return db_rows
    except Exception:
        pass

    result: list[dict] = []
    try:
        result = await finmind.get_monthly_revenue(symbol, start.isoformat())
    except Exception:
        pass

    if not result:
        # MOPS fallback: hits the new SPA backend for whatever monthly
        # history that server feels like returning (typically trailing
        # 12-24 months in one call).
        try:
            result = await mops.get_monthly_revenue_recent(symbol)
        except Exception:
            pass

    # FinMind connector no longer fakes growth rates (PR #211 — the
    # upstream `revenue_year` / `revenue_month` were period integers,
    # not YoY/MoM percentages). Stamp `revenue_yoy=None, revenue_mom=None`
    # on every FinMind row so the API response shape stays consistent
    # for callers that expect these keys. MOPS rows already carry real
    # yoy/mom from "去年同月增減百分比" so we leave those untouched.
    for row in result:
        row.setdefault("revenue_yoy", None)
        row.setdefault("revenue_mom", None)

    if result:
        await cache_set(key, json.dumps(result), TTL_REVENUE)
    return result


# ── Financials ────────────────────────────────────────────────────

async def get_fundamentals(symbol: str) -> dict[str, Any]:
    """PE, PB, dividend yield from TWSE BWIBBU_d + exchange info.

    Read tier: Redis → DB (within 7 days) → TWSE upstream → DB (any age).
    The 7-day window matches BWIBBU's typical refresh cadence — older
    rows are still better than blank state during a TWSE outage.
    """
    from services.ingest.repository import (
        FundamentalsSnapshotRow,
        read_latest_fundamentals_autosession,
        upsert_fundamentals_snapshots_autosession,
    )

    key = f"tw:fundamentals:{symbol}"
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)

    base = {
        "symbol": symbol,
        "market": "TW",
        "exchange": get_exchange(symbol),
    }

    # ── Tier 2: recent DB snapshot ─────────────────────────────────
    db_snap = await read_latest_fundamentals_autosession(
        "TW", symbol, max_age_days=7,
    )
    if db_snap is not None:
        result = {**base, **{
            "pe_ratio":       db_snap.get("pe_ratio"),
            "pb_ratio":       db_snap.get("pb_ratio"),
            "dividend_yield": db_snap.get("dividend_yield"),
            "fetched_at":     db_snap.get("as_of"),
            "data_source":    db_snap.get("data_source") or "db",
        }}
        await cache_set(key, json.dumps(result), TTL_FUNDAMENTALS)
        return result

    # ── Tier 3: TWSE upstream ──────────────────────────────────────
    have_ratios = False
    try:
        ratios = await twse.get_valuation_ratios(symbol)
        if ratios:
            base.update(ratios)
            have_ratios = True
    except Exception:
        pass

    if have_ratios:
        await cache_set(key, json.dumps(base), TTL_FUNDAMENTALS)
        # Best-effort write-back so the next request serves from DB.
        await upsert_fundamentals_snapshots_autosession([
            FundamentalsSnapshotRow(
                market="TW", symbol=symbol, as_of=date.today(),
                pe_ratio=base.get("pe_ratio"),
                pb_ratio=base.get("pb_ratio"),
                dividend_yield=base.get("dividend_yield"),
                eps=None, revenue=None, payload=None,
                source="twse",
            )
        ])
        return base

    # ── Tier 4: stale DB beats nothing during a TWSE outage ────────
    stale = await read_latest_fundamentals_autosession(
        "TW", symbol, max_age_days=365,
    )
    if stale is not None:
        log.info("tw.fundamentals.served_stale_db", extra={"symbol": symbol})
        return {**base, **{
            "pe_ratio":       stale.get("pe_ratio"),
            "pb_ratio":       stale.get("pb_ratio"),
            "dividend_yield": stale.get("dividend_yield"),
            "fetched_at":     stale.get("as_of"),
            "data_source":    "db_stale",
        }}

    return base


async def get_financials(
    symbol: str, *, as_of: date | None = None,
) -> list[dict[str, Any]]:
    """TW financial-statement rows from FinMind (`{date, type, value}`).

    `as_of` (backtest mode): bypass Redis (per-as_of cache keys would
    explode), fetch the full series, then return only rows with
    `date <= as_of`. Filing dates in FinMind use the period-end date
    (e.g. "2024-09-30" for Q3 2024 filings), so `as_of=2024-10-15`
    correctly includes Q3 2024 (filed mid-October) and excludes Q4.
    """
    if as_of is None:
        key = f"tw:financials:{symbol}"
        cached = await cache_get(key)
        if cached:
            return json.loads(cached)
        result = await finmind.get_financials(symbol)
        await cache_set(key, json.dumps(result), TTL_FUNDAMENTALS)
        return result

    # Backtest mode — direct FinMind call, filter by row date.
    rows = await finmind.get_financials(symbol)
    cutoff = as_of.isoformat()
    return [r for r in rows if str(r.get("date", "")) <= cutoff]


# ── Financial health (財務體質) ───────────────────────────────────
#
# Pivots FinMind rows ({date, type, value}) into per-period dicts, then
# derives margin / leverage / liquidity ratios and assigns red/yellow/green
# lights for the four StatementDog-style categories.

# FinMind type-field aliases — different report formats use different names.
_INCOME_ALIASES = {
    "revenue":         ("Revenue", "OperatingRevenue", "NetSales", "TotalRevenue"),
    "gross_profit":    ("GrossProfit", "GrossProfitFromOperatingActivities"),
    "operating_income": ("OperatingIncome", "OperatingProfit", "IncomeFromOperations"),
    "net_income":      (
        "NetIncome", "NetIncomeAttributableToOwnersOfParent",
        "IncomeAfterTax", "ProfitAfterTax",
    ),
    "eps":             ("EPS", "BasicEPS", "EarningsPerShare"),
}

_BALANCE_ALIASES = {
    "total_assets":         ("TotalAssets", "Assets"),
    "total_liabilities":    ("TotalLiabilities", "Liabilities"),
    "total_equity":         (
        "Equity", "TotalEquity", "EquityAttributableToOwnersOfParent",
    ),
    "current_assets":       ("CurrentAssets",),
    "current_liabilities":  ("CurrentLiabilities",),
}

_CASHFLOW_ALIASES = {
    "operating_cf":  (
        "CashFlowsFromOperatingActivities",
        "NetCashProvidedByOperatingActivities",
    ),
    "investing_cf":  (
        "CashFlowsFromInvestingActivities",
        "NetCashUsedInInvestingActivities",
    ),
    "capex":         (
        "AcquisitionOfPropertyPlantAndEquipment",
        "PurchaseOfPropertyPlantAndEquipment",
    ),
}


def _pivot_by_period(rows: list[dict]) -> dict[str, dict[str, float]]:
    """{date: {type: value}}. Last-write-wins for duplicate (date,type)."""
    out: dict[str, dict[str, float]] = {}
    for r in rows:
        d = r.get("date")
        t = r.get("type")
        v = r.get("value")
        if not d or not t or v is None:
            continue
        try:
            out.setdefault(d, {})[t] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def _pick(period: dict[str, float], names: tuple[str, ...]) -> float | None:
    for n in names:
        if n in period and period[n] is not None:
            return period[n]
    return None


def _safe_div(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return a / b


def _light(value: float | None, *, green: float, yellow: float, higher_better: bool = True) -> str:
    """Map a metric to red/yellow/green. Thresholds in original units (eg 15 = 15%)."""
    if value is None:
        return "gray"
    if higher_better:
        if value >= green:
            return "green"
        if value >= yellow:
            return "yellow"
        return "red"
    if value <= green:
        return "green"
    if value <= yellow:
        return "yellow"
    return "red"


async def get_health(symbol: str, periods: int = 8) -> dict[str, Any]:
    """
    Returns a structured StatementDog-style financial-health snapshot:

      {
        "symbol": ...,
        "periods":  [{"date", revenue, gross_margin, operating_margin,
                      net_margin, debt_ratio, current_ratio, eps,
                      operating_cf, free_cf}, ...],   # newest last
        "summary":  {"latest_roe", "latest_debt_ratio",
                     "revenue_yoy", "operating_cf_positive_streak"},
        "lights":   {"profitability", "safety", "growth", "cash_flow"}
      }
    """
    key = f"tw:health:{symbol}"
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)

    try:
        income_rows = await finmind.get_financials(symbol)
    except Exception:
        income_rows = []
    try:
        bs_rows = await finmind.get_balance_sheet(symbol)
    except Exception:
        bs_rows = []
    try:
        cf_rows = await finmind.get_cash_flow(symbol)
    except Exception:
        cf_rows = []

    income = _pivot_by_period(income_rows)
    bs     = _pivot_by_period(bs_rows)
    cf     = _pivot_by_period(cf_rows)

    all_dates = sorted(set(income) | set(bs) | set(cf))[-periods:]

    out_periods: list[dict[str, Any]] = []
    for d in all_dates:
        ip = income.get(d, {})
        bp = bs.get(d, {})
        cp = cf.get(d, {})

        revenue          = _pick(ip, _INCOME_ALIASES["revenue"])
        gross_profit     = _pick(ip, _INCOME_ALIASES["gross_profit"])
        operating_income = _pick(ip, _INCOME_ALIASES["operating_income"])
        net_income       = _pick(ip, _INCOME_ALIASES["net_income"])
        eps              = _pick(ip, _INCOME_ALIASES["eps"])

        total_assets        = _pick(bp, _BALANCE_ALIASES["total_assets"])
        total_liabilities   = _pick(bp, _BALANCE_ALIASES["total_liabilities"])
        total_equity        = _pick(bp, _BALANCE_ALIASES["total_equity"])
        current_assets      = _pick(bp, _BALANCE_ALIASES["current_assets"])
        current_liabilities = _pick(bp, _BALANCE_ALIASES["current_liabilities"])

        operating_cf = _pick(cp, _CASHFLOW_ALIASES["operating_cf"])
        capex        = _pick(cp, _CASHFLOW_ALIASES["capex"])

        gross_margin     = _safe_div(gross_profit, revenue)
        operating_margin = _safe_div(operating_income, revenue)
        net_margin       = _safe_div(net_income, revenue)
        debt_ratio       = _safe_div(total_liabilities, total_assets)
        current_ratio    = _safe_div(current_assets, current_liabilities)
        free_cf          = (operating_cf + capex) if (operating_cf is not None and capex is not None) else None

        out_periods.append({
            "date":             d,
            "revenue":          revenue,
            "net_income":       net_income,
            "eps":              eps,
            "gross_margin":     round(gross_margin * 100, 2) if gross_margin is not None else None,
            "operating_margin": round(operating_margin * 100, 2) if operating_margin is not None else None,
            "net_margin":       round(net_margin * 100, 2) if net_margin is not None else None,
            "debt_ratio":       round(debt_ratio * 100, 2) if debt_ratio is not None else None,
            "current_ratio":    round(current_ratio, 2) if current_ratio is not None else None,
            "operating_cf":     operating_cf,
            "free_cf":          free_cf,
            "total_equity":     total_equity,
        })

    # ── Summary metrics ─────────────────────────────────────────
    latest = out_periods[-1] if out_periods else {}

    # ROE on a TTM basis: sum of last 4 quarters' net income / latest equity.
    ttm_net_income: float | None = None
    last_4 = [p for p in out_periods[-4:] if p.get("net_income") is not None]
    if len(last_4) >= 1:
        ttm_net_income = sum(p["net_income"] for p in last_4)
    latest_equity = latest.get("total_equity")
    latest_roe = (
        round(ttm_net_income / latest_equity * 100, 2)
        if (ttm_net_income is not None and latest_equity)
        else None
    )

    # Operating cash-flow positive streak in last 4 periods.
    cf_streak = sum(
        1 for p in out_periods[-4:]
        if p.get("operating_cf") is not None and p["operating_cf"] > 0
    )

    # Revenue YoY: pull latest from the monthly_revenue series (already
    # YoY-computed). Avoids re-deriving from quarterly snapshots.
    revenue_yoy: float | None = None
    try:
        rev_rows = await get_revenue(symbol, months=3)
        if rev_rows:
            revenue_yoy = rev_rows[-1].get("revenue_yoy")
    except Exception:
        pass

    summary = {
        "latest_roe":       latest_roe,
        "latest_debt_ratio": latest.get("debt_ratio"),
        "latest_gross_margin": latest.get("gross_margin"),
        "latest_net_margin": latest.get("net_margin"),
        "revenue_yoy":      revenue_yoy,
        "cf_positive_streak_4q": cf_streak,
    }

    # ── Traffic-light scoring ───────────────────────────────────
    lights = {
        "profitability": _light(latest_roe, green=15, yellow=5, higher_better=True),
        "safety":        _light(latest.get("debt_ratio"), green=50, yellow=70, higher_better=False),
        "growth":        _light(revenue_yoy, green=10, yellow=0, higher_better=True),
        "cash_flow":     ("green" if cf_streak >= 4 else
                          "yellow" if cf_streak >= 2 else
                          "red" if out_periods else "gray"),
    }

    result = {
        "symbol":  symbol,
        "market":  "TW",
        "periods": out_periods,
        "summary": summary,
        "lights":  lights,
    }
    await cache_set(key, json.dumps(result), TTL_FUNDAMENTALS)
    return result


# ── Screener ──────────────────────────────────────────────────────

async def _get_etf_yields_cached() -> dict[str, float]:
    """TTM dividend yield for every TW ETF; populated by the daily
    refresh_tw_etf_yields scheduler job. Falls back to a 30-day
    last-known-good copy so the screener keeps filtering ETFs through
    transient FinMind quota / TWSE outages instead of silently dropping
    every high-yield ETF from results."""
    for key in ("tw:etf_yields_all", "tw:etf_yields_all:last_known"):
        cached = await cache_get(key)
        if not cached:
            continue
        try:
            return json.loads(cached)
        except (TypeError, ValueError):
            continue
    return {}


async def _get_all_valuations_cached() -> dict[str, dict[str, float | None]]:
    """
    Bulk PE/PB/dividend_yield for every TWSE-listed security.

    BWIBBU_ALL covers individual stocks only — it returns nothing for ETFs.
    We merge the daily-refreshed ETF yield map on top so the screener's
    `min_dividend_yield` filter applies uniformly across stocks and ETFs.
    """
    key = "tw:valuations:all"
    cached = await cache_get(key)
    if cached:
        data = json.loads(cached)
    else:
        try:
            data = await twse.get_all_valuation_ratios()
        except Exception:
            data = {}
        if data:
            await cache_set(key, json.dumps(data), TTL_FUNDAMENTALS)

    etf_yields = await _get_etf_yields_cached()
    if etf_yields:
        for sym, y in etf_yields.items():
            entry = data.setdefault(sym, {"pe_ratio": None, "pb_ratio": None, "dividend_yield": None})
            entry["dividend_yield"] = y

    return data


# ── Screener staleness detection ──────────────────────────────────
#
# `STOCK_DAY_ALL` is supposed to refresh ~14:30 Taipei but in practice
# can lag — discussions fired hours after close occasionally still see
# yesterday's row. `ingest_ohlcv_tw` writes today's bars into
# `ohlcv_daily` at 06:30 UTC (= 14:30 Taipei) and finishes within ~40
# min, so `ohlcv_daily` is a more reliable "today's close" source than
# live TWSE for any discussion run post-15:10 Taipei.
#
# Two helpers below let `get_screener` (a) prefer DB when it has today,
# and (b) detect when TWSE's live response actually carries an older
# session so the row's `actual_session` stamp reflects reality.

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


def _latest_complete_session() -> date:
    """The latest TW trading session whose close data SHOULD be
    available right now, per the freshness resolver. Returns a date
    for any wall-clock time:
      - Post-14:30 Taipei weekday → today
      - Pre-open / intraday / between_close_and_publish → prev trading day
      - Weekend → most recent weekday
    Used as the single staleness reference so the screener and the
    captured_session block agree on what "stale" means — the original
    `_expected_today_session()` only returned a date for
    `today_close_published`, which silently disabled the yfinance
    recovery the other 88% of the day.
    """
    from services.discussion.freshness import _tw_latest_complete_session
    now_tw = datetime.now(UTC).astimezone(_TW)
    sess, _phase = _tw_latest_complete_session(now_tw)
    return sess

# Per-pod cache for ohlcv_daily latest-session lookups. The daily
# ingest cron writes once at 06:30 UTC and a stale read up to 60s old
# is harmless (it just means the screener may stay on the slow path
# 60s past the cron finishing). Value is (cache_expires_at_epoch,
# session_date | None).
_ohlcv_latest_cache: tuple[float, date | None] | None = None
_OHLCV_LATEST_TTL_SECONDS = 60


async def get_latest_ohlcv_session() -> date | None:
    """Latest TW session present in `ohlcv_daily` (excluding the
    synthetic `_TAIEX` index symbol). 60s in-process cache — the daily
    ingest cron writes once per day so a stale read is harmless and
    saves a DB hit per discussion round."""
    global _ohlcv_latest_cache
    import time as _time
    now = _time.time()
    if _ohlcv_latest_cache is not None and _ohlcv_latest_cache[0] > now:
        return _ohlcv_latest_cache[1]

    from sqlalchemy import func, select

    from db.session import AsyncSessionLocal
    from models.ohlcv_daily import OhlcvDaily
    try:
        async with AsyncSessionLocal() as db:
            stmt = select(func.max(OhlcvDaily.ts)).where(
                OhlcvDaily.market == "TW",
                ~OhlcvDaily.symbol.startswith("_"),
            )
            value = (await db.execute(stmt)).scalar()
    except Exception as exc:
        log.warning("tw.screener.ohlcv_latest_lookup_failed",
                    extra={"error": str(exc)})
        value = None

    _ohlcv_latest_cache = (now + _OHLCV_LATEST_TTL_SECONDS, value)
    return value


async def _bellwether_ohlcv_closes(
    *, symbol: str, lookback_days: int = 10,
) -> list[tuple[date, float]]:
    """Last `lookback_days` of `(ts, close)` for `symbol` in
    `ohlcv_daily`, sorted ascending. Used by the screener staleness
    detector to compare the TWSE bellwether close against recent DB
    closes."""
    from sqlalchemy import select

    from db.session import AsyncSessionLocal
    from models.ohlcv_daily import OhlcvDaily
    try:
        async with AsyncSessionLocal() as db:
            stmt = (
                select(OhlcvDaily.ts, OhlcvDaily.close)
                .where(
                    OhlcvDaily.market == "TW",
                    OhlcvDaily.symbol == symbol,
                )
                .order_by(OhlcvDaily.ts.desc())
                .limit(lookback_days)
            )
            rows = list((await db.execute(stmt)).all())
    except Exception as exc:
        log.warning("tw.screener.bellwether_lookup_failed",
                    extra={"symbol": symbol, "error": str(exc)})
        return []
    return sorted(
        ((r[0], float(r[1])) for r in rows if r[1] is not None),
        key=lambda x: x[0],
    )


async def _detect_stock_day_all_session(
    raw_rows: list[dict[str, Any]],
) -> date | None:
    """Compare the bellwether row in a fresh `STOCK_DAY_ALL` response
    against recent `ohlcv_daily` closes for the same symbol. Return
    the matched session date, or `None` when the data store can't
    confirm (bellwether missing, no DB rows, or no close match within
    a small epsilon).

    Match epsilon is 0.01 TWD (TWSE quotes 2 decimals max for
    sub-100 stocks, so any larger gap is a different day's close, not
    a rounding artifact).
    """
    bell_row = next(
        (
            r for r in raw_rows
            if (r.get("Code") or r.get("證券代號") or "").strip()
            == _STALENESS_BELLWETHER
        ),
        None,
    )
    if not bell_row:
        return None
    bell_close = twse._tw_num(
        bell_row.get("收盤價") or bell_row.get("ClosingPrice"),
    )
    if bell_close is None:
        return None

    recent = await _bellwether_ohlcv_closes(
        symbol=_STALENESS_BELLWETHER, lookback_days=5,
    )
    for ts, db_close in reversed(recent):
        if abs(db_close - bell_close) < 0.01:
            return ts
    return None


def _yf_ticker_for(symbol: str, exchange: str) -> str:
    """Map a TW symbol to its Yahoo Finance ticker. TWSE listed →
    `XXXX.TW`, TPEx → `XXXX.TWO`. Used by the yfinance recovery path
    only; the rest of the codebase keeps the bare 4-digit symbol."""
    suffix = ".TWO" if exchange == "TPEx" else ".TW"
    return f"{symbol}{suffix}"


# `_yfinance_session_is_fresh` was merged into the shared
# `_independent_source_is_fresh` helper (defined below) when Part F
# added the FinMind recovery tier — both pipelines now share the
# same bellwether check shape.


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


async def _independent_source_is_fresh(
    candidate_2330_close: float | None,
    detected_session: date,
) -> bool:
    """Shared "is this independent pipeline ALSO stale?" check used by
    both FinMind and yfinance recovery tiers. Returns False if
    `candidate_2330_close` matches DB's 2330 close on `detected_session`
    within 0.01 TWD — the source is reporting the same session that
    TWSE's live tier already showed us was stale.

    Returns True when no match is found (the source has a value that
    isn't in our archive yet → presumably a newer session).
    """
    if candidate_2330_close is None:
        return False
    recent = await _bellwether_ohlcv_closes(
        symbol=_STALENESS_BELLWETHER, lookback_days=5,
    )
    for ts, db_close in recent:
        if ts == detected_session and abs(db_close - candidate_2330_close) < 0.01:
            return False
    return True


def _passes_fundamental_filters(
    v: dict[str, float | None],
    *,
    min_pe: float | None,
    max_pe: float | None,
    min_pb: float | None,
    max_pb: float | None,
    min_dividend_yield: float | None,
) -> bool:
    pe = v.get("pe_ratio")
    pb = v.get("pb_ratio")
    dy = v.get("dividend_yield")
    if min_pe is not None and (pe is None or pe < min_pe):
        return False
    if max_pe is not None and (pe is None or pe > max_pe):
        return False
    if min_pb is not None and (pb is None or pb < min_pb):
        return False
    if max_pb is not None and (pb is None or pb > max_pb):
        return False
    if min_dividend_yield is not None and (dy is None or dy < min_dividend_yield):
        return False
    return True


async def get_screener(
    exchange: str | None = None,    # "TWSE" | "TPEx" | None (both)
    min_volume: int | None = None,
    min_pe: float | None = None,
    max_pe: float | None = None,
    min_pb: float | None = None,
    max_pb: float | None = None,
    min_dividend_yield: float | None = None,
    include_etf: bool = True,
    etf_only: bool = False,
    limit: int = 100,
    as_of: date | None = None,
    diagnostic: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    `as_of` (PR #226): backtest mode. When set, builds the screener
    from `ohlcv_daily` snapshot instead of the live TWSE feed.
    Valuation filters (PE/PB/yield) are skipped in backtest because
    the per-day valuation archive doesn't extend back. Result is
    NOT cached in backtest mode (per-as_of cache key would explode).

    `diagnostic` (Part F): when callers pass a mutable dict, the live
    path populates it with the recovery-tier attempts + final source
    so the JSON view can surface exactly which tier produced the
    rendered rows. None for other callers means zero overhead.
    """
    if as_of is not None:
        return await _get_screener_backtest(
            as_of=as_of,
            exchange=exchange,
            min_volume=min_volume,
            include_etf=include_etf,
            etf_only=etf_only,
            limit=limit,
        )

    # Cache key includes `ohlcv_latest` so when the daily ingest cron
    # advances the archive, the existing 10-min cache slot is invalidated
    # automatically — otherwise a stale entry written before today's
    # bar landed would shadow a now-correct fast-path result for the
    # remainder of the TTL window.
    needs_valuations_early = any(
        v is not None for v in (min_pe, max_pe, min_pb, max_pb, min_dividend_yield)
    )
    ohlcv_latest = await get_latest_ohlcv_session()
    freshness_session = _latest_complete_session()
    _seed_diagnostic(
        diagnostic,
        freshness_session=freshness_session,
        ohlcv_latest=ohlcv_latest,
    )
    ohlcv_tag = ohlcv_latest.isoformat() if ohlcv_latest else "none"
    key = (
        f"tw:screener:{exchange}:{min_volume}:"
        f"{min_pe}:{max_pe}:{min_pb}:{max_pb}:{min_dividend_yield}:"
        f"{include_etf}:{etf_only}:{limit}:{ohlcv_tag}"
    )
    cached = await cache_get(key)
    if cached:
        _set_final_data_source(diagnostic, "cache_hit")
        return json.loads(cached)

    # Staleness fast-path: if `ohlcv_daily` already holds the latest
    # complete session per the freshness resolver, prefer it. Fires
    # post-publish (14:30 Taipei +) when DB has today, AND pre-open
    # / intraday / weekend when DB has the latest weekday close — the
    # backtest path is the right behaviour in all cases since
    # `freshness_session` is exactly the session we want anchored on.
    if (
        not needs_valuations_early
        and ohlcv_latest is not None
        and ohlcv_latest >= freshness_session
    ):
        result = await _get_screener_backtest(
            as_of=ohlcv_latest,
            exchange=exchange,
            min_volume=min_volume,
            include_etf=include_etf,
            etf_only=etf_only,
            limit=limit,
        )
        for row in result:
            row["actual_session"] = ohlcv_latest.isoformat()
            row["data_source"] = "ohlcv_daily_today"
            row["is_stale"] = False
        _record_attempt(diagnostic, "ohlcv_fast_path", "fired")
        _set_final_data_source(diagnostic, "ohlcv_daily_today")
        if result:
            await cache_set(key, json.dumps(result), TTL_SCREENER)
        return result
    _record_attempt(diagnostic, "ohlcv_fast_path", "skipped")

    try:
        all_stocks = await twse.get_all_twse_symbols()
    except Exception as exc:
        log.warning("tw.screener.twse_symbols_failed", extra={"error": str(exc)})
        all_stocks = []

    # Sort by volume desc so the most-traded names surface first. The TWSE
    # endpoint orders by symbol ascending, which means the ~250 ETFs (00xxx)
    # bury every regular stock past the limit cap.
    all_stocks.sort(
        key=lambda s: twse._tw_int(s.get("成交股數") or s.get("TradeVolume", "0")),
        reverse=True,
    )

    needs_valuations = any(
        v is not None for v in (min_pe, max_pe, min_pb, max_pb, min_dividend_yield)
    )
    valuations = await _get_all_valuations_cached() if needs_valuations else {}

    result = []
    for s in all_stocks:
        code = (s.get("Code") or s.get("證券代號") or "").strip()
        if not code:
            continue
        symbol_is_etf = is_etf(code)
        if etf_only and not symbol_is_etf:
            continue
        if not include_etf and symbol_is_etf:
            continue
        vol = twse._tw_int(s.get("成交股數") or s.get("TradeVolume", "0"))
        if min_volume and vol < min_volume:
            continue
        exch = _exchange_map.get(code, "TWSE")
        if exchange and exch != exchange:
            continue

        v = valuations.get(code, {}) if valuations else {}
        if needs_valuations and not _passes_fundamental_filters(
            v,
            min_pe=min_pe, max_pe=max_pe,
            min_pb=min_pb, max_pb=max_pb,
            min_dividend_yield=min_dividend_yield,
        ):
            continue

        price = twse._tw_num(s.get("收盤價") or s.get("ClosingPrice"))
        change = twse._tw_num(s.get("漲跌價差") or s.get("Change"))
        change_pct = round(change / (price - change) * 100, 4) if (
            change is not None and price is not None and (price - change)
        ) else None
        # Reuse the shared sanity bound (`_sanitize_change_pct`). Drop
        # both `change` and `change_pct` together so the rendered row
        # stays consistent rather than showing a raw delta with no %.
        sanitized = _sanitize_change_pct(code, change_pct)
        if sanitized is None and change_pct is not None:
            change = None
        change_pct = sanitized

        result.append({
            "symbol":         code,
            "market":         "TW",
            "exchange":       exch,
            "name_zh":        s.get("Name") or s.get("證券名稱", ""),
            "price":          price,
            "change":         change,
            "change_pct":     change_pct,
            "volume":         vol,
            "pe_ratio":       v.get("pe_ratio") if v else None,
            "pb_ratio":       v.get("pb_ratio") if v else None,
            "dividend_yield": v.get("dividend_yield") if v else None,
            "data_source":    "twse",
        })
        if len(result) >= limit:
            break

    # Sentinel-based staleness check: compare 2330's STOCK_DAY_ALL
    # close to recent `ohlcv_daily` closes. When TWSE's live endpoint
    # is lagging (still serving yesterday's row hours after close), the
    # detector returns yesterday's date and rows get stamped
    # accordingly so downstream callers don't treat the snapshot as
    # today. When `ohlcv_daily` already holds a newer bar than the
    # detected session, recompute via the backtest path so the actual
    # close gets returned instead of a stale TWSE row.
    is_stale_unrecoverable = False
    detected_session = await _detect_stock_day_all_session(all_stocks)
    if diagnostic is not None and detected_session is not None:
        diagnostic["detected_session"] = detected_session.isoformat()
    if detected_session is not None:
        if (
            ohlcv_latest is not None
            and ohlcv_latest > detected_session
            and not needs_valuations
        ):
            recovered = await _get_screener_backtest(
                as_of=ohlcv_latest,
                exchange=exchange,
                min_volume=min_volume,
                include_etf=include_etf,
                etf_only=etf_only,
                limit=limit,
            )
            for row in recovered:
                row["actual_session"] = ohlcv_latest.isoformat()
                row["data_source"] = "ohlcv_daily_recovered"
                row["is_stale"] = False
            _record_attempt(diagnostic, "ohlcv_recovery", "recovered")
            _set_final_data_source(diagnostic, "ohlcv_daily_recovered")
            if recovered:
                await cache_set(key, json.dumps(recovered), TTL_SCREENER)
            return recovered
        # Compare against the freshness resolver's session so stale
        # is recognised at any hour of the day — the previous
        # `expected_today` short-circuit silently disabled recovery
        # outside the 14:30-onwards window.
        stale = detected_session < freshness_session
        # Two-tier independent recovery: FinMind sponsor first
        # (paid + market-wide single call), then yfinance (free
        # + independent). Both check 2330 against ohlcv_daily so
        # they bail when their own pipeline is also stuck on the
        # same session TWSE returned.
        if stale and not needs_valuations:
            recovered = await _recover_screener_via_finmind(
                candidates=result,
                detected_session=detected_session,
                expected_today=freshness_session,
                limit=limit,
                diagnostic=diagnostic,
            )
            if recovered is not None:
                _set_final_data_source(diagnostic, "finmind_recovery")
                await cache_set(key, json.dumps(recovered), TTL_SCREENER)
                return recovered
            recovered = await _recover_screener_via_yfinance(
                candidates=result,
                detected_session=detected_session,
                expected_today=freshness_session,
                limit=limit,
                diagnostic=diagnostic,
            )
            if recovered is not None:
                _set_final_data_source(diagnostic, "yfinance_recovery")
                await cache_set(key, json.dumps(recovered), TTL_SCREENER)
                return recovered
        is_stale_unrecoverable = stale
        for row in result:
            row["actual_session"] = detected_session.isoformat()
            row["is_stale"] = stale
        if stale:
            _set_final_data_source(diagnostic, "twse_stale")
        else:
            _set_final_data_source(diagnostic, "twse")
    else:
        # Detector inconclusive (bellwether missing, DB empty, no close
        # match). Stamp with the freshness resolver's session so the
        # row carries something — downstream tagging still works, just
        # without staleness assertion.
        for row in result:
            row["actual_session"] = freshness_session.isoformat()
            row["is_stale"] = None
        _set_final_data_source(diagnostic, "twse_unverified")

    # Don't cache an empty screener for 10 min — mirrors the US service so
    # a transient TWSE OpenAPI failure doesn't lock the page into "no
    # results" until the next background refresh.
    # When the screener returned stale, cache for 60s only so the next
    # call within a minute is fast but anything after retries a fresh
    # recovery — a full 10-min lock would shadow a TWSE refresh.
    if result:
        ttl = _TTL_SCREENER_STALE if is_stale_unrecoverable else TTL_SCREENER
        await cache_set(key, json.dumps(result), ttl)
    else:
        log.warning("tw.screener.empty_result")
    return result


async def _get_screener_backtest(
    *,
    as_of: date,
    exchange: str | None,
    min_volume: int | None,
    include_etf: bool,
    etf_only: bool,
    limit: int,
) -> list[dict[str, Any]]:
    """Backtest mode for `get_screener`. Reads `ohlcv_daily` for the
    10-day window ending at `as_of`, picks the latest bar per symbol,
    computes change_pct against the prior bar, applies the same volume
    / exchange / ETF filters as live mode. Output shape mirrors live
    so callers don't branch.
    """
    from db.session import AsyncSessionLocal
    from models.ohlcv_daily import OhlcvDaily
    from sqlalchemy import select

    start = as_of - timedelta(days=10)
    try:
        async with AsyncSessionLocal() as db:
            stmt = (
                select(OhlcvDaily)
                .where(
                    OhlcvDaily.market == "TW",
                    OhlcvDaily.ts >= start,
                    OhlcvDaily.ts <= as_of,
                )
                .order_by(OhlcvDaily.symbol, OhlcvDaily.ts)
            )
            rows = list((await db.scalars(stmt)).all())
    except Exception as exc:
        log.warning("tw.screener.backtest_db_error",
                    extra={"as_of": as_of.isoformat(), "error": str(exc)})
        return []

    by_symbol: dict[str, list[Any]] = {}
    for r in rows:
        # Skip the synthetic _TAIEX index row — it's not a stock.
        if r.symbol.startswith("_"):
            continue
        by_symbol.setdefault(r.symbol, []).append(r)

    result: list[dict[str, Any]] = []
    for symbol, bars in by_symbol.items():
        if etf_only and not is_etf(symbol):
            continue
        if not include_etf and is_etf(symbol):
            continue
        exch = _exchange_map.get(symbol, "TWSE")
        if exchange and exch != exchange:
            continue
        last = bars[-1]
        close = float(last.close) if last.close is not None else None
        vol = int(last.volume or 0)
        if min_volume and vol < min_volume:
            continue
        prev = (
            float(bars[-2].close)
            if len(bars) >= 2 and bars[-2].close is not None
            else None
        )
        change = (close - prev) if (close is not None and prev is not None) else None
        change_pct = (
            round(change / prev * 100, 4)
            if (change is not None and prev) else None
        )
        change_pct = _sanitize_change_pct(symbol, change_pct)
        if change_pct is None and change is not None:
            change = None
        result.append({
            "symbol":         symbol,
            "market":         "TW",
            "exchange":       exch,
            "name_zh":        _name_map.get(symbol, ""),
            "price":          close,
            "change":         change,
            "change_pct":     change_pct,
            "volume":         vol,
            "pe_ratio":       None,
            "pb_ratio":       None,
            "dividend_yield": None,
            "data_source":    "ohlcv_daily",
            "as_of":          last.ts.isoformat(),
        })

    result.sort(key=lambda r: r["volume"] or 0, reverse=True)
    return result[:limit]


# ── TAIEX index ───────────────────────────────────────────────────

async def get_index(
    *, history_days: int = 0, as_of: date | None = None,
) -> dict[str, Any]:
    """TAIEX current quote, optionally with a recent daily history line.

    `history_days=0` (default) preserves the legacy "single quote"
    shape — Redis cache, one TWSE call. Pass a positive value to
    additionally include `history` (a list of `{time, close}` rows
    for the last N trading days) read from `ohlcv_daily` where
    `_TAIEX` is populated by `tasks/ingest_taiex_history`. The
    discussion subsystem uses `history_days=30` so personas can see
    大盤型態 (e.g. "TAIEX 連跌 5 日近 5% 修正") with real numbers.

    History is read from DB only — no live waterfall — so a fresh
    deploy without ingest data returns `history=[]` rather than
    burning a TWSE call per request.

    `as_of` (PR #226): backtest mode. Both the quote portion and
    the history window come from `ohlcv_daily` clamped to
    `ts <= as_of`. Live cache is bypassed so the historical read
    doesn't poison the live key.
    """
    if as_of is not None:
        return await _get_index_backtest(
            as_of=as_of, history_days=history_days,
        )

    key = "tw:index:taiex"
    cached = await cache_get(key)
    if cached:
        result = json.loads(cached)
    else:
        result = await twse.get_taiex()
        await cache_set(key, json.dumps(result), TTL_QUOTE)

    if history_days > 0:
        from services.ingest.repository import read_ohlcv_range_autosession
        from services.tw_trading_calendar import utcnow_tw_date
        end = utcnow_tw_date()  # Taipei-local; date.today() is UTC → off-by-one pre-09:00 Taipei
        start = end - timedelta(days=history_days * 2)  # widen for weekends
        bars = await read_ohlcv_range_autosession(
            "TW", "_TAIEX", start, end,
        )
        history = [
            {"time": b["time"], "close": b["close"]}
            for b in bars[-history_days:]
        ]
        # Phase 3: if the archive is missing today's TAIEX bar but
        # `MI_5MINS` (already in `result.value`) carries an intraday
        # or just-closed value for today, splice a synthetic today
        # entry onto the tail. This bridges the window between TWSE
        # publish (~13:30 Taipei) and `ingest_taiex_history` cron
        # (07:10 UTC = 15:10 Taipei) where personas would otherwise
        # see a 30-day line ending YESTERDAY despite today's value
        # sitting right there in `result["value"]`.
        # Weekend guard: `MI_5MINS` returns cached Friday's last value
        # on Sat/Sun — splicing a "Saturday bar = Friday's close"
        # would mis-label the timeline. Only augment on weekdays
        # where today is an actual (or just-finished) session.
        last_bar_date = (
            date.fromisoformat(str(history[-1]["time"])[:10])
            if history else None
        )
        live_value = result.get("value") if isinstance(result, dict) else None
        if (
            isinstance(live_value, (int, float))
            and last_bar_date is not None
            and last_bar_date < end
            and end.weekday() < 5
        ):
            history.append({"time": end.isoformat(), "close": live_value})
        result = {**result, "history": history}
    return result


async def _get_index_backtest(
    *, as_of: date, history_days: int,
) -> dict[str, Any]:
    """Backtest mode for `get_index`. Reads `_TAIEX` from `ohlcv_daily`
    with `ts <= as_of`, returns the same shape as live mode (price /
    change / change_pct / history). Empty dict when the archive has
    no bars in range."""
    from services.ingest.repository import read_ohlcv_range_autosession

    # Pull a generous window so weekends / holidays still leave us with
    # the latest bar + the prior bar for change_pct computation, plus
    # the requested history_days slice.
    window = max(history_days * 2, 14)
    start = as_of - timedelta(days=window)
    bars = await read_ohlcv_range_autosession("TW", "_TAIEX", start, as_of)
    if not bars:
        return {}

    last = bars[-1]
    prev_close = bars[-2]["close"] if len(bars) >= 2 else None
    close = last.get("close")
    change = (
        close - prev_close
        if (close is not None and prev_close is not None) else None
    )
    change_pct = (
        round(change / prev_close * 100, 4)
        if (change is not None and prev_close) else None
    )
    result: dict[str, Any] = {
        "symbol":      "TAIEX",
        "name":        "TAIEX",
        "price":       close,
        "prev_close":  prev_close,
        "change":      change,
        "change_pct":  change_pct,
        "data_source": "ohlcv_daily",
        "as_of":       last["time"],
    }
    if history_days > 0:
        result["history"] = [
            {"time": b["time"], "close": b["close"]}
            for b in bars[-history_days:]
        ]
    return result


# ── News ──────────────────────────────────────────────────────────

from cache.cache_ttls import TTL_NEWS  # noqa: E402


async def _google_news_rss(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """
    Hit Google News RSS for TW-localized headlines. RSS XML is plain text,
    no API key required. Each <item> has title / link / pubDate / source.
    Links are Google News redirect URLs that resolve to the original article
    when clicked.
    """
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime

    import httpx

    url = "https://news.google.com/rss/search"
    params = {
        "q":    query,
        "hl":   "zh-TW",
        "gl":   "TW",
        "ceid": "TW:zh-Hant",
    }
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as c:
        r = await c.get(url, params=params)
        r.raise_for_status()
        xml_text = r.text

    root = ET.fromstring(xml_text)
    items: list[dict[str, Any]] = []
    for el in root.findall(".//item")[:limit]:
        title = (el.findtext("title") or "").strip()
        link = (el.findtext("link") or "").strip()
        pub_date_raw = el.findtext("pubDate") or ""
        source_el = el.find("source")
        publisher = source_el.text.strip() if (source_el is not None and source_el.text) else ""
        try:
            published_at = parsedate_to_datetime(pub_date_raw).isoformat()
        except (TypeError, ValueError):
            published_at = pub_date_raw
        if not title or not link:
            continue
        items.append({
            "title":        title,
            "publisher":    publisher,
            "link":         link,
            "published_at": published_at,
            "thumbnail":    None,
        })
    return items


async def _yfinance_news_fallback(symbol: str, limit: int) -> list[dict[str, Any]]:
    """Legacy yfinance path. Often returns [] for TW symbols."""
    import asyncio
    from datetime import datetime
    loop = asyncio.get_running_loop()

    def _fetch():
        import yfinance as yf
        t = yf.Ticker(f"{symbol}.TW")
        raw = t.news or []
        items = []
        for n in raw[:limit]:
            thumbnail = None
            if t_data := n.get("thumbnail"):
                resolutions = t_data.get("resolutions", [])
                thumbnail = resolutions[0].get("url") if resolutions else None
            items.append({
                "title":        n.get("title", ""),
                "publisher":    n.get("publisher", ""),
                "link":         n.get("link", ""),
                "published_at": datetime.fromtimestamp(
                    n.get("providerPublishTime", 0), tz=UTC
                ).isoformat(),
                "thumbnail":    thumbnail,
            })
        return items

    return await loop.run_in_executor(None, _fetch)


async def get_news(symbol: str, limit: int = 10) -> list[dict[str, Any]]:
    """
    TW news read tier:
        Redis cache  →  DB (FinMind ingest archive)  →
        Google News RSS (live scrape)  →  yfinance fallback

    The DB tier serves recent FinMind articles populated by the hourly
    `ingest_news_tw` cron — no upstream call needed for cache misses.
    Google News RSS stays as the live scrape fallback because FinMind's
    coverage tags articles by `stock_id` so per-symbol matching may
    miss broad-market coverage.
    """
    from services.ingest.repository import read_recent_news_autosession

    key = f"tw:news:{symbol.upper()}"
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)

    # ── Tier 2: Postgres archive ────────────────────────────────
    db_items = await read_recent_news_autosession(
        "TW", symbol=symbol.upper(), limit=limit, max_age_days=14,
    )
    if db_items:
        await cache_set(key, json.dumps(db_items), TTL_NEWS)
        return db_items

    # ── Tier 3: live Google News RSS ────────────────────────────
    name = ""
    try:
        q_cached = await cache_get(key_quote("tw", symbol))
        if q_cached:
            name = json.loads(q_cached).get("name_zh", "") or ""
    except Exception:
        pass

    query = f"{symbol} {name}".strip() if name else f"{symbol} 台股"

    items: list[dict[str, Any]] = []
    try:
        items = await _google_news_rss(query, limit=limit)
    except Exception:
        items = []

    if not items:
        try:
            items = await _yfinance_news_fallback(symbol, limit)
        except Exception:
            items = []

    if items:
        await cache_set(key, json.dumps(items), TTL_NEWS)
    return items


# ── Valuation band (本益比 / 股價淨值比 河流圖) ──────────────────
#
# Derived on-demand from existing inputs — daily price history + quarterly
# EPS and total equity from FinMind. No daily snapshot table; the result
# is cached 24h. This trades a slightly stale cache (refreshes once a
# day) for zero schema/migration cost.

from cache.cache_ttls import TTL_VALUATION_BAND  # noqa: E402


def _ttm_eps_at(period_end: str, eps_history: list[tuple[str, float]]) -> float | None:
    """Sum EPS over the last ≤4 reported quarters with period-end ≤ given date."""
    last_4 = [v for d, v in eps_history if d <= period_end][-4:]
    return sum(last_4) if last_4 else None


def _bvps_at(period_end: str,
             equity_history: list[tuple[str, float]],
             shares_history: list[tuple[str, float]]) -> float | None:
    """Latest equity / latest shares-outstanding estimate ≤ given date."""
    eq = next((v for d, v in reversed(equity_history) if d <= period_end), None)
    sh = next((v for d, v in reversed(shares_history) if d <= period_end), None)
    if eq is None or sh is None or sh <= 0:
        return None
    return eq / sh


def _stats(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"mean": None, "std": None, "min": None, "max": None,
                "p10": None, "p25": None, "p50": None, "p75": None, "p90": None}
    sorted_v = sorted(values)
    n = len(sorted_v)
    mean = sum(sorted_v) / n
    var = sum((v - mean) ** 2 for v in sorted_v) / n
    std = var ** 0.5

    def _pct(p: float) -> float:
        if n == 1:
            return sorted_v[0]
        rank = p * (n - 1)
        lo = int(rank)
        frac = rank - lo
        hi = min(lo + 1, n - 1)
        return sorted_v[lo] * (1 - frac) + sorted_v[hi] * frac

    return {
        "mean": round(mean, 3),
        "std":  round(std, 3),
        "min":  round(sorted_v[0], 3),
        "max":  round(sorted_v[-1], 3),
        "p10":  round(_pct(0.10), 3),
        "p25":  round(_pct(0.25), 3),
        "p50":  round(_pct(0.50), 3),
        "p75":  round(_pct(0.75), 3),
        "p90":  round(_pct(0.90), 3),
    }


async def get_valuation_band(
    symbol: str,
    metric: str = "pe",       # "pe" | "pb"
    years: int = 5,
) -> dict[str, Any]:
    """
    Returns a daily PE or PB time series plus mean/std/percentile bands.
    PE  = close / TTM EPS  (negative or zero EPS → series gap)
    PB  = close / BVPS, where BVPS = total_equity / (NetIncome/EPS)
    """
    if metric not in ("pe", "pb"):
        raise ValueError(f"metric must be 'pe' or 'pb', got {metric!r}")

    key = f"tw:valuation_band:{symbol}:{metric}:{years}"
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)

    # ── Inputs ──────────────────────────────────────────────────
    bars = await get_history(symbol, months=years * 12)

    income_rows  = []
    balance_rows = []
    try:
        income_rows = await finmind.get_financials(symbol)
    except Exception:
        pass
    if metric == "pb":
        try:
            balance_rows = await finmind.get_balance_sheet(symbol)
        except Exception:
            pass

    income = _pivot_by_period(income_rows)
    bs     = _pivot_by_period(balance_rows)

    # Per-quarter EPS series (sorted ascending by date).
    eps_history: list[tuple[str, float]] = sorted(
        (d, _pick(p, _INCOME_ALIASES["eps"]))
        for d, p in income.items()
    )
    eps_history = [(d, v) for d, v in eps_history if v is not None]

    # Shares-outstanding series — derived as NetIncome / EPS where both
    # are reported. Unit ends up as "shares" if both are in absolute units;
    # since we only divide later by equity in the same unit system, the
    # per-share metric is consistent regardless of whether values are
    # stored in 千元 or 元 (units cancel as long as they match across rows).
    shares_history: list[tuple[str, float]] = []
    if metric == "pb":
        for d, p in sorted(income.items()):
            ni  = _pick(p, _INCOME_ALIASES["net_income"])
            eps = _pick(p, _INCOME_ALIASES["eps"])
            if ni is None or eps is None or eps == 0:
                continue
            shares_history.append((d, ni / eps))

    equity_history: list[tuple[str, float]] = sorted(
        (d, _pick(p, _BALANCE_ALIASES["total_equity"]))
        for d, p in bs.items()
    )
    equity_history = [(d, v) for d, v in equity_history if v is not None]

    # ── Build daily series ──────────────────────────────────────
    series: list[dict[str, Any]] = []
    for bar in bars:
        d = bar.get("time")
        close = bar.get("close")
        if not d or close is None:
            continue
        if metric == "pe":
            eps_ttm = _ttm_eps_at(d, eps_history)
            value = (close / eps_ttm) if (eps_ttm is not None and eps_ttm > 0) else None
        else:
            bvps = _bvps_at(d, equity_history, shares_history)
            value = (close / bvps) if (bvps is not None and bvps > 0) else None
        series.append({"date": d, "value": round(value, 3) if value is not None else None})

    # ── Stats over the non-null portion ─────────────────────────
    values = [p["value"] for p in series if p["value"] is not None]
    stats = _stats(values)
    current = next((p["value"] for p in reversed(series) if p["value"] is not None), None)
    current_z = (
        round((current - stats["mean"]) / stats["std"], 3)
        if (current is not None and stats["std"] and stats["std"] > 0)
        else None
    )

    result = {
        "symbol":  symbol,
        "metric":  metric,
        "series":  series,
        "stats":   {**stats, "current": current, "current_z": current_z},
    }
    if values:
        await cache_set(key, json.dumps(result), TTL_VALUATION_BAND)
    return result


# ── Dividends + ETF holdings ──────────────────────────────────────

from cache.cache_ttls import TTL_DIVIDENDS  # noqa: E402
TTL_ETF_HOLDINGS = 24 * 3600


def _normalize_dividend(r: dict) -> dict[str, Any]:
    """
    FinMind TaiwanStockDividend rows vary year-over-year. Common keys:
      date / CashEarningsDistribution / StockEarningsDistribution
      / CashStatutorySurplus / StockStatutorySurplus
      / CashExDividendTradingDate / StockExDividendTradingDate
    Collapse into a single (date, cash_dividend, stock_dividend) row.
    """
    cash = (
        (r.get("CashEarningsDistribution") or 0)
        + (r.get("CashStatutorySurplus") or 0)
    )
    stock = (
        (r.get("StockEarningsDistribution") or 0)
        + (r.get("StockStatutorySurplus") or 0)
    )
    return {
        "date":            r.get("date") or r.get("CashExDividendTradingDate"),
        "ex_date":         r.get("CashExDividendTradingDate") or r.get("StockExDividendTradingDate"),
        "cash_dividend":   round(float(cash), 4) if cash else 0.0,
        "stock_dividend":  round(float(stock), 4) if stock else 0.0,
    }


async def get_dividends(symbol: str) -> list[dict[str, Any]]:
    """Cash + stock dividend history, normalized and oldest-first."""
    key = f"tw:dividends:{symbol}"
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)

    rows: list[dict] = []
    try:
        rows = await finmind.get_dividends(symbol)
    except Exception:
        rows = []

    out = [_normalize_dividend(r) for r in rows]
    out = [r for r in out if r["date"] and (r["cash_dividend"] or r["stock_dividend"])]
    out.sort(key=lambda r: r["date"])

    await cache_set(key, json.dumps(out), TTL_DIVIDENDS)
    return out


async def get_etf_holdings(symbol: str) -> dict[str, Any]:
    """
    Top constituents with weights, latest snapshot only. Returns
    {"as_of": None, "holdings": []} if FinMind doesn't expose holdings
    for this ETF (free-tier restriction or not-an-ETF). Frontend
    renders an empty state in that case.
    """
    empty: dict[str, Any] = {"as_of": None, "holdings": []}
    if not is_etf(symbol):
        return empty

    key = f"tw:etf_holdings:{symbol}"
    cached = await cache_get(key)
    if cached:
        return json.loads(cached)

    rows: list[dict] = []
    try:
        rows = await finmind.get_etf_holdings(symbol)
    except Exception:
        rows = []

    if not rows:
        return empty

    latest_date = max(r.get("date", "") for r in rows)
    snapshot = [r for r in rows if r.get("date") == latest_date]

    out: list[dict[str, Any]] = []
    for r in snapshot:
        # FinMind field names vary: stock_id / SecurityCode and
        # weight / Weight / shares_per. Try several.
        sym = r.get("stock_id") or r.get("SecurityCode") or r.get("symbol", "")
        weight = (
            r.get("weight")
            or r.get("Weight")
            or r.get("shares_per")
            or r.get("HoldingPct")
        )
        try:
            weight_f = float(weight) if weight is not None else None
        except (TypeError, ValueError):
            weight_f = None
        if not sym or weight_f is None:
            continue
        out.append({
            "symbol":   sym,
            "name_zh":  r.get("stock_name") or r.get("name_zh") or "",
            "weight":   round(weight_f, 4),
        })

    out.sort(key=lambda r: r["weight"], reverse=True)
    result = {"as_of": latest_date, "holdings": out}
    await cache_set(key, json.dumps(result), TTL_ETF_HOLDINGS)
    return result
