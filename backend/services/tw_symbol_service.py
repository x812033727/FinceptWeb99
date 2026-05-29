"""TW symbol / industry / company-name lookups.

Carved out of ``tw_market_service`` (PR-A of the long-running TW-service
split). Owns three in-memory maps:

  * ``_exchange_map``  symbol → ``"TWSE" | "TPEx"``
  * ``_industry_map``  symbol → 產業別 (e.g. ``"半導體業"``)
  * ``_name_map``      symbol → 公司簡稱 (e.g. ``"台積電"``)

Plus the three-tier warm-up path that keeps them populated across pod
restarts:

  1. ``load_symbol_map_from_cache`` — read Redis snapshot (fastest, 7-day TTL)
  2. ``load_symbol_map_from_db``    — read ``tw_company_info`` if Redis empty
  3. ``refresh_symbol_map``         — fetch TWSE / TPEx upstream, write back
                                      to both Redis + DB so the next pod has
                                      a recovery path

Invariant preserved across the split: the three module-level dicts keep
their object identity. Every refresher mutates them in place via
``.clear() + .update()`` rather than rebinding ``_exchange_map = new_dict``,
so re-exports from ``tw_market_service`` (and the external callers that
import these dicts directly — ``tasks/ingest_ohlcv_tw.py``,
``tasks/ingest_revenue_tw_slow.py``, ``scripts/backfill_ohlcv_tw.py``,
``api/tw_market/router.py``) keep seeing live data after every refresh.
Test fixtures that ``.clear()`` between tests rely on this too.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime

import data.tw.twse_connector as twse

log = logging.getLogger(__name__)

# In-process symbol → exchange map; refreshed by scheduler (Phase 5)
_exchange_map: dict[str, str] = {}   # symbol → "TWSE" | "TPEx"
_industry_map: dict[str, str] = {}   # symbol → 產業別 (e.g. "半導體業")
_name_map: dict[str, str] = {}       # symbol → 公司簡稱 (e.g. "台積電")


# TW ETFs are 4–6-digit codes that start with "00" (e.g. 0050, 0056,
# 00713, 006208). Futures-based ETFs and 反向/槓桿 ETFs (00xxxR / 00xxxL)
# also fit this regex. Regular stocks are 4-digit codes 1xxx–9xxx.
_ETF_CODE = re.compile(r"^00\d{2,4}[A-Z]?$")


def is_etf(symbol: str) -> bool:
    return bool(_ETF_CODE.match(symbol or ""))


# ── Symbol exchange lookup ────────────────────────────────────────


def get_exchange(symbol: str) -> str:
    return _exchange_map.get(symbol, "TWSE")


# Redis-backed snapshot of the three in-memory maps. Survives pod
# restarts so cold-start chip rendering (and search / industry lookups
# everywhere else) doesn't sit blank until the next TWSE refresh —
# and gives multi-pod deployments a consistent view when one pod's
# upstream refresh succeeds and another's gets rate-limited.
_SYMBOL_MAP_CACHE_KEY = "tw:symbol_map:v1"
_SYMBOL_MAP_CACHE_TTL = 7 * 24 * 3600  # 7 days


async def _persist_symbol_map_to_cache(
    exch: dict[str, str],
    industry: dict[str, str],
    name: dict[str, str],
) -> None:
    """Best-effort: snapshot the three maps into Redis so any pod that
    cold-starts after this can warm in instantly. Silent on Redis
    outage — the in-memory maps are still authoritative for this
    process; the cache is a recovery aid, not a hard dependency."""
    try:
        from cache.redis_cache import cache_set
        payload = json.dumps({
            "exchange": exch,
            "industry": industry,
            "name": name,
        })
        await cache_set(_SYMBOL_MAP_CACHE_KEY, payload, _SYMBOL_MAP_CACHE_TTL)
    except Exception as exc:
        log.debug("tw symbol_map cache write skipped: %s", exc)


async def load_symbol_map_from_cache() -> bool:
    """Warm the in-memory maps from Redis. Called once during app
    startup (before ``refresh_symbol_map`` attempts the upstream
    fetch) so a cold-start pod has 公司名稱 / 產業 / 上市櫃 lookups
    available immediately — chips and search don't sit blank for the
    ~10s the TWSE fetch takes (or forever, if TWSE is blocking us).

    Returns True if any of the three maps was populated from cache.
    Empty / missing cache + Redis outages return False so the caller
    can log it but proceed to attempt the upstream refresh either way.
    """
    try:
        from cache.redis_cache import cache_get
        raw = await cache_get(_SYMBOL_MAP_CACHE_KEY)
    except Exception as exc:
        log.debug("tw symbol_map cache read skipped: %s", exc)
        return False
    if not raw:
        return False
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return False
    loaded = False
    exch = payload.get("exchange")
    if isinstance(exch, dict) and exch:
        _exchange_map.clear()
        _exchange_map.update({str(k): str(v) for k, v in exch.items()})
        loaded = True
    industry = payload.get("industry")
    if isinstance(industry, dict) and industry:
        _industry_map.clear()
        _industry_map.update({str(k): str(v) for k, v in industry.items()})
        loaded = True
    name = payload.get("name")
    if isinstance(name, dict) and name:
        _name_map.clear()
        _name_map.update({str(k): str(v) for k, v in name.items()})
        loaded = True
    return loaded


async def load_symbol_map_from_db() -> bool:
    """Postgres-backed fallback for the in-memory maps when Redis is
    empty. Sits between :func:`load_symbol_map_from_cache` and the
    upstream :func:`refresh_symbol_map` so a fresh-deploy pod whose
    Redis key doesn't exist yet (cache eviction or first-deploy of
    this code) AND can't reach TWSE still serves chip names from the
    last persisted snapshot.

    Returns True iff any of the three maps was populated from DB.
    Silent on missing table (alembic not yet run) / connection errors
    — caller falls through to ``refresh_symbol_map``.
    """
    try:
        from sqlalchemy import select

        from db.session import AsyncSessionLocal
        from models.tw_company_info import TwCompanyInfo
    except ImportError:
        return False
    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.scalars(select(TwCompanyInfo))).all()
    except Exception as exc:
        log.debug("tw symbol_map DB read skipped: %s", exc)
        return False
    if not rows:
        return False
    new_exch: dict[str, str] = {}
    new_industry: dict[str, str] = {}
    new_name: dict[str, str] = {}
    for r in rows:
        sym = (r.symbol or "").strip()
        if not sym:
            continue
        new_exch[sym] = (r.exchange or "TWSE").strip()
        if r.industry:
            new_industry[sym] = r.industry
        if r.name_zh:
            new_name[sym] = r.name_zh
    loaded = False
    if new_exch:
        _exchange_map.clear()
        _exchange_map.update(new_exch)
        loaded = True
    if new_industry:
        _industry_map.clear()
        _industry_map.update(new_industry)
        loaded = True
    if new_name:
        _name_map.clear()
        _name_map.update(new_name)
        loaded = True
    return loaded


async def _persist_symbol_map_to_db(
    exch: dict[str, str],
    industry: dict[str, str],
    name: dict[str, str],
) -> None:
    """Best-effort: upsert every populated symbol into ``tw_company_info``
    after a successful TWSE refresh. Silent on errors — Redis snapshot
    remains the primary recovery path; the DB tier is the deploy-
    survival fallback for the case where Redis is also empty."""
    if not (exch or industry or name):
        return
    try:
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        from db.session import AsyncSessionLocal
        from models.tw_company_info import TwCompanyInfo
    except ImportError:
        return
    symbols = set(exch) | set(industry) | set(name)
    payload = [
        {
            "symbol": s,
            "exchange": exch.get(s) or "TWSE",
            "industry": industry.get(s),
            "name_zh": name.get(s),
        }
        for s in symbols
        if s
    ]
    if not payload:
        return
    try:
        async with AsyncSessionLocal() as db:
            dialect = (
                db.bind.dialect.name if db.bind is not None else "postgresql"
            )
            insert_fn = sqlite_insert if dialect == "sqlite" else pg_insert
            chunk_size = 4000
            for i in range(0, len(payload), chunk_size):
                batch = payload[i:i + chunk_size]
                stmt = insert_fn(TwCompanyInfo).values(batch)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["symbol"],
                    set_={
                        "exchange": stmt.excluded.exchange,
                        "industry": stmt.excluded.industry,
                        "name_zh": stmt.excluded.name_zh,
                        "updated_at": datetime.now(UTC),
                    },
                )
                await db.execute(stmt)
            await db.commit()
    except Exception as exc:
        log.debug("tw symbol_map DB write skipped: %s", exc)


async def refresh_symbol_map() -> None:
    """Called by scheduler daily. Refreshes three in-memory maps:

      - `_exchange_map`: symbol → "TWSE" | "TPEx"
      - `_industry_map`: symbol → 產業別 (e.g. "半導體業")
      - `_name_map`: symbol → 公司簡稱 (e.g. "台積電")

    Industry / name come from TWSE `t187ap03_L` for listed
    companies. TPEx companies inherit industry from the upstream's
    `MainSubject` / `IndustryName` column when present, else None.
    Failures on any source are tolerated — partial maps are better
    than blank state.

    After updating in-memory state, snapshots the three maps to Redis
    (key ``tw:symbol_map:v1``, 7-day TTL) so cold-start pods +
    siblings that lose their TWSE call to rate-limiting can warm in
    from cache via :func:`load_symbol_map_from_cache`. The same
    snapshot is also upserted into ``tw_company_info`` via
    :func:`_persist_symbol_map_to_db` so pods that come up with an
    empty Redis (first deploy of this code, or after a cache flush)
    still have a recovery path that survives across deploys.
    """
    new_exch: dict[str, str] = {}
    new_industry: dict[str, str] = {}
    new_name: dict[str, str] = {}

    try:
        for row in await twse.get_all_twse_symbols():
            code = row.get("Code") or row.get("證券代號", "")
            if code:
                new_exch[code.strip()] = "TWSE"
    except Exception:
        pass
    # `resolve_industry_label` translates TWSE/TPEx 產業別 codes (e.g.
    # `"27"`) to canonical Chinese names (`"通信網路業"`) — both
    # endpoints now return codes some of the time. Idempotent on
    # already-resolved values, so it's safe to apply universally.
    from data.tw.industry_codes import resolve_industry_label

    try:
        for row in await twse.get_all_tpex_symbols():
            code = (row.get("SecuritiesCompanyCode") or row.get("代號", "")).strip()
            if not code:
                continue
            new_exch[code] = "TPEx"
            ind = (row.get("MainSubject") or row.get("IndustryName") or "").strip()
            ind_resolved = resolve_industry_label(ind)
            if ind_resolved:
                new_industry[code] = ind_resolved
            nm = (row.get("CompanyName") or row.get("公司名稱") or "").strip()
            if nm:
                new_name[code] = nm
    except Exception:
        pass

    # Industry + name for TWSE-listed comes from t187ap03_L, which is
    # richer than the daily-price endpoint used for the exchange tag
    # alone. Ignore failure; map stays partial.
    try:
        for row in await twse.get_listed_company_industries():
            sym = row.get("symbol", "").strip()
            if not sym:
                continue
            ind_resolved = resolve_industry_label(row.get("industry"))
            if ind_resolved:
                new_industry[sym] = ind_resolved
            nm = row.get("name_zh")
            if nm:
                new_name[sym] = nm
    except Exception:
        pass

    if new_exch:
        _exchange_map.clear()
        _exchange_map.update(new_exch)
    if new_industry:
        _industry_map.clear()
        _industry_map.update(new_industry)
    if new_name:
        _name_map.clear()
        _name_map.update(new_name)

    # Snapshot whatever we have to Redis even on partial-failure: the
    # union of "what we already had in-memory plus what we just
    # fetched" is still a better starting point for a cold-start
    # sibling than nothing at all. Same payload also goes to Postgres
    # so a future cold-start with empty Redis can still warm in.
    if _exchange_map or _industry_map or _name_map:
        await _persist_symbol_map_to_cache(
            _exchange_map, _industry_map, _name_map,
        )
        await _persist_symbol_map_to_db(
            _exchange_map, _industry_map, _name_map,
        )


def get_industry(symbol: str) -> str | None:
    """In-memory accessor — returns the cached industry for `symbol`,
    or None if the map hasn't been populated for this code.
    Refreshed daily by the `tw_symbol_map` cron via
    `refresh_symbol_map()`.

    Defensively passes the cached value through `resolve_industry_label`
    so a process whose `_industry_map` was populated before PR #213
    (when raw codes like `"27"` were stored) still surfaces the
    Chinese name without waiting for the next refresh cycle. Idempotent
    on already-resolved names — safe to apply universally."""
    from data.tw.industry_codes import resolve_industry_label
    return resolve_industry_label(_industry_map.get(symbol))


def get_company_name(symbol: str) -> str | None:
    """In-memory accessor — public-listed company short name."""
    return _name_map.get(symbol)


def get_industry_peers(symbol: str, *, exclude_self: bool = True) -> list[str]:
    """Return every symbol in the same 產業別 as `symbol`, optionally
    excluding `symbol` itself. Returns an empty list when the symbol's
    industry isn't in `_industry_map` (fresh process before the daily
    cron has populated, or a code that 上市 doesn't classify).

    Used by `services.short_term_signals.compute_industry_rs` for
    sector-rotation context — comparing a focus symbol's recent return
    to the median of its peers tells personas whether the move is
    idiosyncratic or industry-wide.

    Resolution goes through `resolve_industry_label` so a `_industry_map`
    populated before PR #213 (raw codes like `"27"`) still groups
    correctly. Idempotent.
    """
    from data.tw.industry_codes import resolve_industry_label
    target = resolve_industry_label(_industry_map.get(symbol))
    if target is None:
        return []
    peers: list[str] = []
    for sym, raw_label in _industry_map.items():
        if exclude_self and sym == symbol:
            continue
        if resolve_industry_label(raw_label) == target:
            peers.append(sym)
    return peers


def find_symbol_by_name_in_text(text: str) -> str | None:
    """Reverse-lookup: scan `text` for any TW listed company short name
    and return the matching symbol. First match wins, with longer
    names tried first so e.g. "中華電" matches before "中華" (which
    is a substring of the former — same prefix collision exists for
    "台積電" vs "台積").

    Returns None when:
      - `text` is empty
      - `_name_map` hasn't been populated yet (fresh deploy, ingest
        cron hasn't run)
      - no name matches

    Used by `ingest_news_tw._to_row` to tag articles like
    "台積電法說超預期 - 經濟日報" (no 4-digit code in title) as
    `symbol="2330"` so per-symbol news lookups actually find them.
    The same in-memory `_name_map` populated by `refresh_symbol_map`
    is reused — no extra fetch.

    Cost: O(N) substring checks per call where N ≈ 1500 listed
    companies. Each substring check is O(len(text)) ≈ 50 chars, so
    total ~75K character compares per article — well under 1ms.
    """
    if not text or not _name_map:
        return None
    # Sort once per call. The map only changes daily on the symbol-
    # refresh cron, but we trade a tiny per-call sort for not having
    # to invalidate a cached sorted view from inside the cron path.
    pairs = sorted(_name_map.items(), key=lambda kv: -len(kv[1] or ""))
    for symbol, name in pairs:
        if name and name in text:
            return symbol
    return None


def find_symbols_by_names_in_text(text: str, *, limit: int) -> list[str]:
    """Multi-match sibling of `find_symbol_by_name_in_text` for the
    discussion subsystem (PR #221).

    Scans `text` for TW listed company names and returns up to
    `limit` matched symbols, deduped, longest-name-first so prefix
    collisions resolve correctly (台積電→2330 chosen over 台積→
    some-other-symbol).

    A name embedded inside a longer one already-matched name is
    skipped: "台積電 vs 中華電" matches both 2330 and 2412, but
    after 中華電→2412 hits we don't also re-match 中華→???.

    Used by `extract_focus_symbols` to pick up name-only mentions
    ("討論台積電 / 鴻海 短線走勢") that the digit / cashtag regexes
    miss. Returns empty list when text is empty or name_map hasn't
    populated yet — caller falls through to the regex matches.
    """
    if not text or not _name_map or limit <= 0:
        return []
    pairs = sorted(_name_map.items(), key=lambda kv: -len(kv[1] or ""))
    found: list[str] = []
    found_set: set[str] = set()
    # Mask out characters covered by already-matched names so a
    # shorter name that's a substring of an earlier match doesn't
    # double-count THE SAME span. But a separate occurrence of the
    # shorter name elsewhere in the text DOES still match — the
    # mask only blanks the consumed positions.
    masked = text
    for symbol, name in pairs:
        if not name or symbol in found_set:
            continue
        if name not in masked:
            continue
        found.append(symbol)
        found_set.add(symbol)
        # Replace ALL occurrences of this name in the mask, so the
        # shorter-name pass sees only what's left.
        masked = masked.replace(name, " " * len(name))
        if len(found) >= limit:
            break
    return found
