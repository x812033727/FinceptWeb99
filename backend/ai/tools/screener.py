"""
run_screener — natural-language stock screening tool (功能 B2).

Wraps the EXISTING cached screener services (`us_market_service.get_screener`
/ `tw_market_service.get_screener`) and applies additional constrained
filters in Python. Strictly read-only:

  * `filters` is a whitelisted key set per market — unknown keys are
    rejected with the allowed list, never interpreted. No SQL string
    ever passes through from the model.
  * The one DB-backed filter (`foreign_net_buy_days_min`, TW 外資買超)
    is a fixed ORM query over `tw_institutional_daily` following the
    same guard conventions as `ai/tools/sql.py`: whitelisted table,
    parameter-bound values, SELECT-only session usage.
  * `limit` is clamped to 50 so a runaway model can't dump the whole
    market into its context window.

Only fields the existing screener services actually return are exposed
as filters. RSI / other derived indicators would need per-symbol price
history (expensive fan-out) and are intentionally NOT exposed.
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

logger = logging.getLogger(__name__)

# ── constrained filter schema ─────────────────────────────────────

# Common to both markets — every key maps onto a field the screener
# services return in each row (or natively accept as a filter param).
_COMMON_FILTERS = frozenset({
    "pe_min", "pe_max",
    "pb_min", "pb_max",
    "dividend_yield_min",
    "price_min", "price_max",
    "change_pct_min", "change_pct_max",
    "volume_min",
})
_US_FILTERS = _COMMON_FILTERS | {"market_cap_min", "sector"}
_TW_FILTERS = _COMMON_FILTERS | {
    "exclude_etf", "etf_only",
    "industry",                    # 產業別 substring, in-memory map lookup
    "foreign_net_buy_days_min",    # 外資買超天數 (last 20 sessions), DB-backed
}

# sort_by whitelist. pe/pb sort ascending (cheap first), the rest
# descending (big first) — matches how the intents are phrased.
_SORT_ASC = frozenset({"pe_ratio", "pb_ratio"})
_SORT_KEYS = frozenset({
    "volume", "change_pct", "price",
    "pe_ratio", "pb_ratio", "dividend_yield", "market_cap",
})

_LIMIT_MAX = 50
_LIMIT_DEFAULT = 20
# Candidate pool pulled from the (cached) screener service before the
# Python-side filters narrow it down.
_POOL_LIMIT = 500
# 外資買超 window: ~20 trading sessions ≈ 30 calendar days + buffer.
_FOREIGN_WINDOW_CAL_DAYS = 35

RUN_SCREENER_DESCRIPTION = (
    "Read-only stock screener over the US (S&P 500) or TW (TWSE/TPEx) "
    "market. Use it to turn natural-language stock-picking intents into "
    "a filtered result table. Examples:\n"
    "  「殖利率>5%、本益比<15 的台股」→ market='TW', "
    "filters={'dividend_yield_min':5,'pe_max':15}\n"
    "  「近月外資買超、本益比低於 20 的電子股」→ market='TW', "
    "filters={'foreign_net_buy_days_min':10,'pe_max':20,'industry':'電子'}\n"
    "  「市值超過 2000 億美元的科技股,依市值排序」→ market='US', "
    "filters={'market_cap_min':200000000000,'sector':'Technology'}, "
    "sort_by='market_cap'\n"
    "filters keys — both markets: pe_min, pe_max, pb_min, pb_max, "
    "dividend_yield_min (%), price_min, price_max, change_pct_min, "
    "change_pct_max, volume_min (shares). "
    "US only: market_cap_min (USD), sector (e.g. 'Technology'). "
    "TW only: exclude_etf (bool), etf_only (bool), industry (產業別 "
    "substring, e.g. '電子'/'半導體'/'金融'), foreign_net_buy_days_min "
    "(外資近 20 個交易日買超天數下限, 1-20). Unknown keys are rejected. "
    "RSI 等衍生指標未支援 — 請勿假造。 "
    "sort_by: volume | change_pct | price | pe_ratio | pb_ratio | "
    "dividend_yield | market_cap (pe/pb 由低到高,其餘由高到低; "
    "default volume). limit: max 50, default 20."
)


def _num(filters: dict[str, Any], key: str) -> float | None:
    """Coerce a filter value to float; raise ValueError on garbage."""
    v = filters.get(key)
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ValueError(f"filter {key!r} must be a number, got {v!r}")


def _passes_range(value: Any, lo: float | None, hi: float | None) -> bool:
    """Range check; rows missing the field fail any active bound."""
    if lo is None and hi is None:
        return True
    if value is None:
        return False
    if lo is not None and value < lo:
        return False
    if hi is not None and value > hi:
        return False
    return True


async def _foreign_net_buy_stats(symbols: list[str]) -> dict[str, dict[str, int]]:
    """外資買賣超 aggregate over the last ~20 sessions per symbol.

    Fixed, parameter-bound ORM query on `tw_institutional_daily`
    (same read-only guard conventions as ai/tools/sql.py — whitelisted
    table, no free-form SQL, SELECT only). Returns
    {symbol: {"net_buy_days": int, "net_shares": int}}.
    """
    from sqlalchemy import case, func, select

    from db.session import AsyncSessionLocal
    from models.tw_chip_metrics import TwInstitutionalDaily as T

    if not symbols:
        return {}
    net = func.coalesce(T.fini_buy, 0) - func.coalesce(T.fini_sell, 0)
    async with AsyncSessionLocal() as session:
        latest = (
            await session.execute(
                select(func.max(T.ts)).where(T.market == "TW")
            )
        ).scalar()
        if latest is None:
            return {}
        cutoff = latest - timedelta(days=_FOREIGN_WINDOW_CAL_DAYS)
        stmt = (
            select(
                T.symbol,
                func.sum(case((net > 0, 1), else_=0)).label("net_buy_days"),
                func.sum(net).label("net_shares"),
            )
            .where(T.market == "TW", T.symbol.in_(symbols), T.ts >= cutoff)
            .group_by(T.symbol)
        )
        rows = (await session.execute(stmt)).all()
    return {
        sym: {"net_buy_days": int(days or 0), "net_shares": int(shares or 0)}
        for sym, days, shares in rows
    }


async def run_screener_query(args: dict[str, Any]) -> dict[str, Any]:
    """Shared core for both the MCP and the OpenAI-compat wrappers."""
    market = str(args.get("market", "")).upper()
    if market not in ("US", "TW"):
        return {"error": f"Unsupported market: {market!r}. Use 'US' or 'TW'."}

    filters = args.get("filters") or {}
    if not isinstance(filters, dict):
        return {"error": "filters must be an object"}
    allowed = _US_FILTERS if market == "US" else _TW_FILTERS
    unknown = set(filters) - allowed
    if unknown:
        return {
            "error": (
                f"Unknown filter(s) for {market}: {sorted(unknown)}. "
                f"Allowed: {sorted(allowed)}"
            )
        }

    try:
        limit = int(args.get("limit") or _LIMIT_DEFAULT)
    except (TypeError, ValueError):
        return {"error": "limit must be an integer"}
    limit = max(1, min(limit, _LIMIT_MAX))

    sort_by = str(args.get("sort_by") or "volume")
    if sort_by not in _SORT_KEYS:
        return {"error": f"sort_by must be one of {sorted(_SORT_KEYS)}"}
    if sort_by == "market_cap" and market == "TW":
        return {"error": "sort_by='market_cap' is US-only (TW rows carry no market cap)"}

    try:
        pe_min = _num(filters, "pe_min")
        pe_max = _num(filters, "pe_max")
        pb_min = _num(filters, "pb_min")
        pb_max = _num(filters, "pb_max")
        dy_min = _num(filters, "dividend_yield_min")
        price_min = _num(filters, "price_min")
        price_max = _num(filters, "price_max")
        chg_min = _num(filters, "change_pct_min")
        chg_max = _num(filters, "change_pct_max")
        vol_min = _num(filters, "volume_min")
        cap_min = _num(filters, "market_cap_min") if market == "US" else None
        fnb_min = (
            _num(filters, "foreign_net_buy_days_min") if market == "TW" else None
        )
    except ValueError as exc:
        return {"error": str(exc)}

    # ── candidate pool from the existing (cached) screener service ──
    try:
        if market == "US":
            from services.us_market_service import get_screener as _svc
            rows = await _svc(
                min_market_cap=cap_min,
                min_pe=pe_min,
                max_pe=pe_max,
                min_pb=pb_min,
                max_pb=pb_max,
                min_dividend_yield=dy_min,
                min_volume=int(vol_min) if vol_min is not None else None,
                sector=str(filters["sector"]) if filters.get("sector") else None,
                limit=_POOL_LIMIT,
            )
        else:
            from services.tw_market_service import get_screener as _svc
            rows = await _svc(
                min_pe=pe_min,
                max_pe=pe_max,
                min_pb=pb_min,
                max_pb=pb_max,
                min_dividend_yield=dy_min,
                min_volume=int(vol_min) if vol_min is not None else None,
                include_etf=not bool(filters.get("exclude_etf")),
                etf_only=bool(filters.get("etf_only")),
                limit=_POOL_LIMIT,
            )
    except Exception as exc:
        logger.warning("run_screener service call failed (%s): %s", market, exc)
        return {"error": str(exc)}

    rows = [r for r in rows if isinstance(r, dict)]

    # ── Python-side filters over fields the service returned ───────
    rows = [
        r for r in rows
        if _passes_range(r.get("price"), price_min, price_max)
        and _passes_range(r.get("change_pct"), chg_min, chg_max)
    ]

    if market == "TW" and filters.get("industry"):
        from services.tw_market_service import get_industry
        needle = str(filters["industry"])
        rows = [r for r in rows if needle in (get_industry(r.get("symbol", "")) or "")]

    if fnb_min is not None:
        try:
            stats = await _foreign_net_buy_stats([r["symbol"] for r in rows])
        except Exception as exc:
            logger.warning("run_screener foreign-net-buy lookup failed: %s", exc)
            return {"error": f"foreign_net_buy filter unavailable: {exc}"}
        kept: list[dict[str, Any]] = []
        for r in rows:
            s = stats.get(r.get("symbol", ""))
            if s and s["net_buy_days"] >= fnb_min:
                kept.append({
                    **r,
                    "foreign_net_buy_days": s["net_buy_days"],
                    "foreign_net_shares": s["net_shares"],
                })
        rows = kept

    # ── sort + cap ──────────────────────────────────────────────────
    reverse = sort_by not in _SORT_ASC
    missing = [r for r in rows if r.get(sort_by) is None]
    present = [r for r in rows if r.get(sort_by) is not None]
    present.sort(key=lambda r: r[sort_by], reverse=reverse)
    rows = (present + missing)[:limit]

    return {
        "market": market,
        "count": len(rows),
        "filters": filters,
        "sort_by": sort_by,
        "limit": limit,
        "rows": rows,
    }


# ── MCP wrapper (Claude Agent SDK) ────────────────────────────────

def make_screener_tools() -> list[SdkMcpTool]:
    """Build the run_screener tool. Public market data — no user scoping."""

    @tool(
        "run_screener",
        RUN_SCREENER_DESCRIPTION,
        {"market": str, "filters": dict, "sort_by": str, "limit": int},
    )
    async def run_screener(args: dict[str, Any]) -> dict:
        payload = await run_screener_query(args)
        return {"content": [{"type": "text", "text": json.dumps(
            payload, ensure_ascii=False, default=str,
        )}]}

    return [run_screener]


# ── OpenAI-compat schema (shared with ai/tools/openai_compat.py) ──

RUN_SCREENER_OPENAI_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "run_screener",
        "description": RUN_SCREENER_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "market": {"type": "string", "enum": ["US", "TW"]},
                "filters": {
                    "type": "object",
                    "properties": {
                        "pe_min": {"type": "number"},
                        "pe_max": {"type": "number"},
                        "pb_min": {"type": "number"},
                        "pb_max": {"type": "number"},
                        "dividend_yield_min": {"type": "number"},
                        "price_min": {"type": "number"},
                        "price_max": {"type": "number"},
                        "change_pct_min": {"type": "number"},
                        "change_pct_max": {"type": "number"},
                        "volume_min": {"type": "number"},
                        "market_cap_min": {
                            "type": "number",
                            "description": "US only, in USD",
                        },
                        "sector": {"type": "string", "description": "US only"},
                        "exclude_etf": {"type": "boolean", "description": "TW only"},
                        "etf_only": {"type": "boolean", "description": "TW only"},
                        "industry": {
                            "type": "string",
                            "description": "TW only — 產業別 substring, e.g. 電子/半導體/金融",
                        },
                        "foreign_net_buy_days_min": {
                            "type": "integer", "minimum": 1, "maximum": 20,
                            "description": "TW only — 外資近 20 個交易日買超天數下限",
                        },
                    },
                    "additionalProperties": False,
                },
                "sort_by": {
                    "type": "string",
                    "enum": sorted(_SORT_KEYS),
                    "default": "volume",
                },
                "limit": {"type": "integer", "default": _LIMIT_DEFAULT,
                          "maximum": _LIMIT_MAX},
            },
            "required": ["market"],
        },
    },
}
