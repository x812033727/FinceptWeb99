"""FinMind row → local table mappings.

FinMind's column names are dataset-specific and don't match our
local schema (e.g. `Trading_Volume` → `volume`, `MarginPurchaseBuy`
→ `margin_purchase`). Each registered dataset has a mapping here;
the runner uses the mapping to:

  1. Project FinMind's response dict onto our local-table columns.
  2. Coerce types (date strings → date, "" → None, etc.).
  3. Inject static fields (e.g. `market='TWSE'`).

Adding a new dataset is one entry here + (optionally) a custom
`row_transform` callback for non-trivial unit conversions.

Datasets without a mapping fall through with a `MappingNotFoundError`
that the runner records as `skipped` in `backfill_progress` — Phase 1
schema work continues independently of which datasets the runner
actually knows how to ingest."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True)
class DatasetMapping:
    """One entry per FinMind dataset the ingest runner can route."""

    dataset_code: str
    local_table: str
    # FinMind column name → local column name. Columns absent here
    # are dropped silently (we don't store every FinMind field —
    # see ``raw`` JSONB columns in the fundamental tables for the
    # full-payload alternative).
    column_map: dict[str, str]
    # PK columns of the local table — used to build the ON CONFLICT
    # clause for idempotent UPSERT.
    pk_columns: tuple[str, ...]
    # Static fields injected into every row (e.g. `market='TWSE'` for
    # datasets that don't carry the market axis in their response).
    extra: dict[str, Any] = field(default_factory=dict)
    # Optional per-row callback that runs AFTER `column_map` and
    # `extra` apply — for unit conversions, derived columns, etc.
    row_transform: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    # Optional whole-chunk transform — for wide-format datasets (e.g.
    # quarterly statements that return one FinMind row per line item)
    # that need to be pivoted into one local row per period. Mutually
    # exclusive with row_transform; runner prefers batch_transform when
    # both are set. Receives the raw FinMind payload (list[dict]) and
    # returns local-table rows (list[dict]) directly — column_map and
    # row_transform DO NOT apply on the batch path because the pivot
    # logic owns column resolution itself.
    batch_transform: (
        Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None
    ) = None
    # FinMind has a class of intraday-grain datasets (KBar, PriceTick,
    # BlockTradingDailyReport, GovernmentBankBuySell) that reject any
    # multi-day query with HTTP 400 *"the dataset … size is too large,
    # we only send one day data, so end_date parameter need be none"*.
    # When this flag is set, the runner iterates the requested
    # [range_start, range_end] day-by-day and concatenates the per-day
    # responses, omitting the `end_date` query param on each call.
    # The chunk in `backfill_progress` still spans the full range —
    # the day-level fan-out is internal to one chunk's fetch step.
    single_day: bool = False


class MappingNotFoundError(LookupError):
    """The runner was asked to ingest a dataset with no entry in
    MAPPINGS — distinguishes 'we haven't built this yet' from a
    transient FinMind failure so progress.py records 'skipped'
    rather than 'failed'."""


# ── Type coercion helpers ────────────────────────────────────────


def _to_date(v: Any) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, date):
        return v
    if isinstance(v, datetime):
        return v.date()
    return date.fromisoformat(str(v)[:10])


def _to_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _to_decimal(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except (TypeError, ValueError, InvalidOperation):
        return None


def _to_str(v: Any) -> str | None:
    if v is None or v == "":
        return None
    return str(v)


# ── Mappings ─────────────────────────────────────────────────────
#
# Phase 2 ships mappings for the headline 5 datasets — adding more is
# an append-only operation. The pattern is mechanical: copy an entry,
# swap dataset_code / local_table / column_map.


def _row_ohlcv(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "market": row.get("market", "TWSE"),
        "symbol": _to_str(row.get("symbol")),
        "ts": _to_date(row.get("ts")),
        "open": _to_decimal(row.get("open")),
        "high": _to_decimal(row.get("high")),
        "low": _to_decimal(row.get("low")),
        "close": _to_decimal(row.get("close")),
        "volume": _to_int(row.get("volume")),
        "source": row.get("source", "finmind"),
    }


def _row_margin(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "market": row.get("market", "TWSE"),
        "symbol": _to_str(row.get("symbol")),
        "ts": _to_date(row.get("ts")),
        "margin_purchase": _to_int(row.get("margin_purchase")),
        "margin_sale": _to_int(row.get("margin_sale")),
        "margin_balance": _to_int(row.get("margin_balance")),
        "short_sale": _to_int(row.get("short_sale")),
        "short_cover": _to_int(row.get("short_cover")),
        "short_balance": _to_int(row.get("short_balance")),
        "source": row.get("source", "finmind"),
    }


def _row_institutional(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "market": row.get("market", "TWSE"),
        "symbol": _to_str(row.get("symbol")),
        "ts": _to_date(row.get("ts")),
        "foreign_buy": _to_int(row.get("foreign_buy")),
        "foreign_sell": _to_int(row.get("foreign_sell")),
        "sitc_buy": _to_int(row.get("sitc_buy")),
        "sitc_sell": _to_int(row.get("sitc_sell")),
        "dealer_buy": _to_int(row.get("dealer_buy")),
        "dealer_sell": _to_int(row.get("dealer_sell")),
        "source": row.get("source", "finmind"),
    }


def _row_revenue(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": _to_str(row.get("symbol")),
        "ts": _to_date(row.get("ts")),
        "revenue": _to_decimal(row.get("revenue")),
        "revenue_yoy": _to_decimal(row.get("revenue_yoy")),
        "revenue_mom": _to_decimal(row.get("revenue_mom")),
        "source": row.get("source", "finmind"),
    }


def _row_total_margin(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "market": row.get("market", "TWSE"),
        "ts": _to_date(row.get("ts")),
        "margin_balance": _to_int(row.get("margin_balance")),
        "margin_purchase": _to_int(row.get("margin_purchase")),
        "margin_sale": _to_int(row.get("margin_sale")),
        "short_balance": _to_int(row.get("short_balance")),
        "short_sale": _to_int(row.get("short_sale")),
        "short_cover": _to_int(row.get("short_cover")),
        "source": row.get("source", "finmind"),
    }


def _row_per(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "market": row.get("market", "TWSE"),
        "symbol": _to_str(row.get("symbol")),
        "ts": _to_date(row.get("ts")),
        "per": _to_decimal(row.get("per")),
        "pbr": _to_decimal(row.get("pbr")),
        "dividend_yield": _to_decimal(row.get("dividend_yield")),
        "source": row.get("source", "finmind"),
    }


def _row_shareholding(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "market": row.get("market", "TWSE"),
        "symbol": _to_str(row.get("symbol")),
        "ts": _to_date(row.get("ts")),
        "foreign_holding_pct": _to_decimal(row.get("foreign_holding_pct")),
        "foreign_holding_shares": _to_int(row.get("foreign_holding_shares")),
        "available_shares": _to_int(row.get("available_shares")),
        "source": row.get("source", "finmind"),
    }


def _row_market_value(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "market": row.get("market", "TWSE"),
        "symbol": _to_str(row.get("symbol")),
        "ts": _to_date(row.get("ts")),
        "market_cap": _to_decimal(row.get("market_cap")),
        "issued_shares": _to_int(row.get("issued_shares")),
        "source": row.get("source", "finmind"),
    }


def _row_dividend(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": _to_str(row.get("symbol")),
        "announce_date": _to_date(row.get("announce_date")),
        "cash_dividend": _to_decimal(row.get("cash_dividend")),
        "stock_dividend": _to_decimal(row.get("stock_dividend")),
        "ex_dividend_date": _to_date(row.get("ex_dividend_date")),
        "ex_rights_date": _to_date(row.get("ex_rights_date")),
        "source": row.get("source", "finmind"),
    }


def _row_futures_daily(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "contract": _to_str(row.get("contract")),
        "ts": _to_date(row.get("ts")),
        "open": _to_decimal(row.get("open")),
        "high": _to_decimal(row.get("high")),
        "low": _to_decimal(row.get("low")),
        "close": _to_decimal(row.get("close")),
        "volume": _to_int(row.get("volume")),
        "open_interest": _to_int(row.get("open_interest")),
        "settlement_price": _to_decimal(row.get("settlement_price")),
        "source": row.get("source", "finmind"),
    }


def _row_option_daily(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "contract": _to_str(row.get("contract")),
        "strike": _to_decimal(row.get("strike")),
        "call_put": _to_str(row.get("call_put")),
        "ts": _to_date(row.get("ts")),
        "open": _to_decimal(row.get("open")),
        "high": _to_decimal(row.get("high")),
        "low": _to_decimal(row.get("low")),
        "close": _to_decimal(row.get("close")),
        "volume": _to_int(row.get("volume")),
        "open_interest": _to_int(row.get("open_interest")),
        "source": row.get("source", "finmind"),
    }


def _row_stock_info(row: dict[str, Any]) -> dict[str, Any]:
    # FinMind's `industry_category` carries values like "半導體業";
    # `type` carries the market code (twse / tpex). Normalize to our
    # internal market labels.
    market_raw = (row.get("market") or "").lower()
    market = "TWSE" if "twse" in market_raw or market_raw == "twse" else (
        "OTC" if market_raw in ("otc", "tpex") else market_raw.upper() or "TWSE"
    )
    return {
        "market": market,
        "symbol": _to_str(row.get("symbol")),
        "name_zh": _to_str(row.get("name_zh")),
        "industry_category": _to_str(row.get("industry_category")),
        "listed_at": _to_date(row.get("listed_at")),
        "is_warrant": False,  # FinMind's TaiwanStockInfo is equities only
        "source": row.get("source", "finmind"),
    }


def _row_buyback(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": _to_str(row.get("symbol")),
        "announced_at": _to_date(row.get("announced_at")),
        "started_at": _to_date(row.get("started_at")),
        "ended_at": _to_date(row.get("ended_at")),
        "plan_shares": _to_int(row.get("plan_shares")),
        "actual_shares": _to_int(row.get("actual_shares")),
        "avg_price": _to_decimal(row.get("avg_price")),
        "source": row.get("source", "finmind"),
    }


def _row_news(row: dict[str, Any]) -> dict[str, Any]:
    """News articles need a sha256 dedup key — sha256(title || link).
    Mirrors the existing news_articles ingest pattern in the main app
    so backfilled and live-RSS rows don't double-count by accident."""
    import hashlib

    title = (row.get("title") or "")
    link = (row.get("link") or "")
    sha = hashlib.sha256((title + "||" + link).encode("utf-8")).hexdigest()

    # FinMind's TaiwanStockNews returns `date` as a YYYY-MM-DD string,
    # but our schema is DateTime — coerce to UTC-midnight so naive
    # downstream consumers don't trip over date-vs-datetime types.
    pub = row.get("published_at")
    if isinstance(pub, str) and pub:
        try:
            pub = datetime.fromisoformat(pub[:10]).replace(tzinfo=timezone.utc)
        except ValueError:
            pub = None
    elif isinstance(pub, date) and not isinstance(pub, datetime):
        pub = datetime.combine(pub, datetime.min.time(), tzinfo=timezone.utc)

    return {
        "sha256": sha,
        "market": row.get("market", "TW"),
        "symbol": _to_str(row.get("symbol")),
        "title": title[:512] if title else "(untitled)",
        "link": link or None,
        "summary": _to_str(row.get("summary")),
        "published_at": pub,
        "sentiment_score": None,  # Scored later by the sentiment cron
        "sentiment_label": None,
        "source": row.get("source", "finmind"),
    }


def _row_price_adj(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "market": row.get("market", "TWSE"),
        "symbol": _to_str(row.get("symbol")),
        "ts": _to_date(row.get("ts")),
        "adj_close": _to_decimal(row.get("adj_close")),
        "adj_factor": _to_decimal(row.get("adj_factor")),
        "source": row.get("source", "finmind"),
    }


def _row_dividend_result(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": _to_str(row.get("symbol")),
        "ex_date": _to_date(row.get("ex_date")),
        "before_price": _to_decimal(row.get("before_price")),
        "after_price": _to_decimal(row.get("after_price")),
        "cash_dividend": _to_decimal(row.get("cash_dividend")),
        "stock_dividend": _to_decimal(row.get("stock_dividend")),
        "source": row.get("source", "finmind"),
    }


def _row_split(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": _to_str(row.get("symbol")),
        "ex_date": _to_date(row.get("ex_date")),
        "before_price": _to_decimal(row.get("before_price")),
        "after_price": _to_decimal(row.get("after_price")),
        "split_ratio": _to_decimal(row.get("split_ratio")),
        "source": row.get("source", "finmind"),
    }


def _row_broker_master(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "broker_id": _to_str(row.get("broker_id")),
        "name_zh": _to_str(row.get("name_zh")),
        "branch_name": _to_str(row.get("branch_name")),
        "address": _to_str(row.get("address")),
        "phone": _to_str(row.get("phone")),
        "source": row.get("source", "finmind"),
    }


def _row_disposition(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": _to_str(row.get("symbol")),
        "started_at": _to_date(row.get("started_at")),
        "ended_at": _to_date(row.get("ended_at")),
        "reason": _to_str(row.get("reason")),
        "source": row.get("source", "finmind"),
    }


def _row_holdings_aggregates(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": _to_str(row.get("symbol")),
        "ts": _to_date(row.get("ts")),
        "bracket": _to_str(row.get("bracket")),
        "holders": _to_int(row.get("holders")),
        "shares": _to_int(row.get("shares")),
        "pct": _to_decimal(row.get("pct")),
        "source": row.get("source", "finmind"),
    }


def _row_margin_maintenance(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "market": row.get("market", "TWSE"),
        "ts": _to_date(row.get("ts")),
        "maintenance_pct": _to_decimal(row.get("maintenance_pct")),
        "source": row.get("source", "finmind"),
    }


def _row_cb_info(row: dict[str, Any]) -> dict[str, Any]:
    cb_id = _to_str(row.get("cb_id"))
    # FinMind doesn't ship the underlying stock ticker explicitly; the
    # convention is the first 4 digits of `cb_id` (e.g. `24553` → `2455`)
    # for the standard Taiwan CB numbering. Fall back to None for any
    # cb_id that doesn't fit so we surface odd cases instead of guessing.
    underlying = cb_id[:4] if cb_id and cb_id[:4].isdigit() else None
    return {
        "cb_id": cb_id,
        "underlying_symbol": underlying,
        "name_zh": _to_str(row.get("name_zh")),
        "issue_date": _to_date(row.get("issue_date")),
        "maturity_date": _to_date(row.get("maturity_date")),
        "conversion_price": None,  # not in TaiwanStockConvertibleBondInfo response
        "par_value": _to_decimal(row.get("par_value")),
        "source": row.get("source", "finmind"),
    }


def _row_industry_chain(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": _to_str(row.get("symbol")),
        "industry": _to_str(row.get("industry")),
        "sub_industry": _to_str(row.get("sub_industry")),
        "source": row.get("source", "finmind"),
    }


def _row_suspended(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": _to_str(row.get("symbol")),
        "suspended_at": _to_date(row.get("suspended_at")),
        "reason": _to_str(row.get("reason")),
        "resumed_at": _to_date(row.get("resumed_at")),
        "source": row.get("source", "finmind"),
    }


def _row_stock_info_with_warrant(row: dict[str, Any]) -> dict[str, Any]:
    # FinMind's `TaiwanStockInfoWithWarrant` shares the same column shape
    # as `TaiwanStockInfo` but includes warrants (call/put leveraged
    # certificates). `industry_category == '全部(不含大盤、指數)'` distinguishes
    # warrants from equities since warrants don't have a real industry.
    market_raw = (row.get("market") or "").lower()
    market = "TWSE" if "twse" in market_raw or market_raw == "twse" else (
        "OTC" if market_raw in ("otc", "tpex") else market_raw.upper() or "TWSE"
    )
    industry = _to_str(row.get("industry_category"))
    is_warrant = bool(industry and "不含大盤" in industry)
    return {
        "market": market,
        "symbol": _to_str(row.get("symbol")),
        "name_zh": _to_str(row.get("name_zh")),
        "industry_category": industry,
        "listed_at": _to_date(row.get("listed_at")),
        "is_warrant": is_warrant,
        "source": row.get("source", "finmind"),
    }


def _row_trading_calendar(row: dict[str, Any]) -> dict[str, Any]:
    # FinMind's `TaiwanStockTradingDate` returns one row per trading day
    # with no payload — the row's existence IS the signal that the date
    # is a trading day. Non-trading days are absent from the response.
    return {
        "market": row.get("market", "TWSE"),
        "ts": _to_date(row.get("ts")),
        "is_trading_day": True,
        "note": None,
        "source": row.get("source", "finmind"),
    }


def _row_total_return_index(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "market": row.get("market", "TWSE"),
        "symbol": _to_str(row.get("symbol")),
        "ts": _to_date(row.get("ts")),
        "value": _to_decimal(row.get("value")),
        "source": row.get("source", "finmind"),
    }


def _row_par_value_change(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": _to_str(row.get("symbol")),
        "ex_date": _to_date(row.get("ex_date")),
        # FinMind's TaiwanStockParValueChange doesn't ship the actual
        # old/new par values — only reference prices around the change.
        # Surface `after_ref_close` as the post-change reference and
        # leave the par columns None; operators needing those need a
        # TPEx capital-change announcement join.
        "old_par": None,
        "new_par": None,
        "reference_price": _to_decimal(row.get("reference_price")),
        "source": row.get("source", "finmind"),
    }


def _row_day_trade(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "market": row.get("market", "TWSE"),
        "symbol": _to_str(row.get("symbol")),
        "ts": _to_date(row.get("ts")),
        "buy_volume": _to_int(row.get("buy_volume")),
        "sell_volume": _to_int(row.get("sell_volume")),
        "buy_amount": _to_decimal(row.get("buy_amount")),
        "sell_amount": _to_decimal(row.get("sell_amount")),
        "source": row.get("source", "finmind"),
    }


def _row_securities_lending(row: dict[str, Any]) -> dict[str, Any]:
    """tw_securities_lending PK is (market, symbol, ts,
    transaction_type) — default transaction_type to '_' so FinMind
    rows without that field still satisfy the NOT NULL PK column."""
    return {
        "market": row.get("market", "TWSE"),
        "symbol": _to_str(row.get("symbol")),
        "ts": _to_date(row.get("ts")),
        "transaction_type": _to_str(row.get("transaction_type")) or "_",
        "volume": _to_int(row.get("volume")),
        "fee_rate": _to_decimal(row.get("fee_rate")),
        "source": row.get("source", "finmind"),
    }


def _row_futures_inst(row: dict[str, Any]) -> dict[str, Any]:
    """Both day-session and night-session FinMind rows route through
    here; the per-mapping `extra` dict injects the appropriate
    `session` value ('day' / 'night')."""
    return {
        "contract": _to_str(row.get("contract")),
        "ts": _to_date(row.get("ts")),
        "session": row.get("session", "day"),
        "foreign_long_open_interest": _to_int(
            row.get("foreign_long_open_interest")
        ),
        "foreign_short_open_interest": _to_int(
            row.get("foreign_short_open_interest")
        ),
        "sitc_long_open_interest": _to_int(
            row.get("sitc_long_open_interest")
        ),
        "sitc_short_open_interest": _to_int(
            row.get("sitc_short_open_interest")
        ),
        "dealer_long_open_interest": _to_int(
            row.get("dealer_long_open_interest")
        ),
        "dealer_short_open_interest": _to_int(
            row.get("dealer_short_open_interest")
        ),
        "source": row.get("source", "finmind"),
    }


def _row_market_value_weight(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "market": row.get("market", "TWSE"),
        "symbol": _to_str(row.get("symbol")),
        "ts": _to_date(row.get("ts")),
        "weight": _to_decimal(row.get("weight")),
        "market_cap": _to_decimal(row.get("market_cap")),
        "source": row.get("source", "finmind"),
    }


def _row_price_limit(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "market": row.get("market", "TWSE"),
        "symbol": _to_str(row.get("symbol")),
        "ts": _to_date(row.get("ts")),
        "upper_limit": _to_decimal(row.get("upper_limit")),
        "lower_limit": _to_decimal(row.get("lower_limit")),
        "source": row.get("source", "finmind"),
    }


def _row_suspended(row: dict[str, Any]) -> dict[str, Any]:
    """Sparse event row. PK is (symbol, suspended_at) — operator can
    re-ingest historical suspensions any number of times without
    duplicating rows."""
    return {
        "symbol": _to_str(row.get("symbol")),
        "suspended_at": _to_date(row.get("suspended_at")),
        "reason": _to_str(row.get("reason")),
        "resumed_at": _to_date(row.get("resumed_at")),
        "source": row.get("source", "finmind"),
    }


def _row_business_indicator(row: dict[str, Any]) -> dict[str, Any]:
    """國發會景氣對策信號 — one row per month with a numeric score
    and a Chinese-label color signal (紅/黃紅/綠/黃藍/藍)."""
    return {
        "ts": _to_date(row.get("ts")),
        "score": _to_int(row.get("score")),
        "signal": _to_str(row.get("signal")),
        "source": row.get("source", "finmind"),
    }


def _row_delisting(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": _to_str(row.get("symbol")),
        "delisted_at": _to_date(row.get("delisted_at")),
        "reason": _to_str(row.get("reason")),
        "source": row.get("source", "finmind"),
    }


# ── Single-day datasets (batch transforms) ───────────────────────
#
# FinMind's intraday-grain endpoints (KBar, PriceTick,
# BlockTradingDailyReport, GovernmentBankBuySell) only accept one date
# per call (`single_day=True` on the mapping). The day-by-day fan-out
# happens in `runner.FinmindClient.fetch`; these transforms handle the
# response shape — most need either a `seq` counter (per-day index) or
# a roll-up aggregation (GovernmentBankBuySell sums to one row/day).


def _row_stock_minute(row: dict[str, Any]) -> dict[str, Any]:
    """KBar row → tw_stock_minute. Combines `date` + `minute` (HH:MM[:SS])
    into a UTC-naive timestamp (TWSE local time; downstream consumers
    that need TZ-aware values can apply Asia/Taipei)."""
    d = row.get("ts")  # already renamed from `date` by column_map
    minute_str = row.get("minute_str")
    ts: datetime | None = None
    if d and minute_str:
        d_obj = _to_date(d)
        if d_obj:
            try:
                hh, mm, *rest = str(minute_str).split(":")
                ss = int(rest[0]) if rest else 0
                ts = datetime(d_obj.year, d_obj.month, d_obj.day,
                              int(hh), int(mm), ss, tzinfo=timezone.utc)
            except (ValueError, IndexError):
                ts = None
    return {
        "market": row.get("market", "TWSE"),
        "symbol": _to_str(row.get("symbol")),
        "ts": ts,
        "open": _to_decimal(row.get("open")),
        "high": _to_decimal(row.get("high")),
        "low": _to_decimal(row.get("low")),
        "close": _to_decimal(row.get("close")),
        "volume": _to_int(row.get("volume")),
        "source": row.get("source", "finmind"),
    }


def _batch_stock_tick(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """PriceTick payload → tw_stock_tick. Builds the timestamp from
    `date` + `Time` (HH:MM:SS.ffffff) and assigns a per-day per-symbol
    sequential `seq` so the (market, symbol, ts, seq) PK is unique even
    when multiple ticks land on the same microsecond.
    """
    out: list[dict[str, Any]] = []
    counters: dict[tuple[str, str], int] = {}
    for r in rows:
        sym = str(r.get("stock_id") or "")
        d = str(r.get("date") or "")[:10]
        time_str = str(r.get("Time") or "")
        ts: datetime | None = None
        if d and time_str:
            try:
                d_obj = date.fromisoformat(d)
                hh_mm_ss = time_str.split(".")[0]
                hh, mm, ss = (int(x) for x in hh_mm_ss.split(":"))
                micro = 0
                if "." in time_str:
                    frac = time_str.split(".", 1)[1][:6].ljust(6, "0")
                    micro = int(frac)
                ts = datetime(d_obj.year, d_obj.month, d_obj.day,
                              hh, mm, ss, micro, tzinfo=timezone.utc)
            except (ValueError, IndexError):
                ts = None
        key = (sym, d)
        counters[key] = counters.get(key, 0) + 1
        out.append({
            "market": "TWSE",
            "symbol": sym or None,
            "ts": ts,
            "seq": counters[key],
            "price": _to_decimal(r.get("deal_price")),
            "volume": _to_int(r.get("volume")),
            "bid": None,
            "ask": None,
            "tick_type": _to_str(r.get("TickType")),
            "source": "finmind",
        })
    return out


def _batch_block_trade(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """BlockTradingDailyReport payload → tw_block_trade. The local
    schema only carries the trade economics (price/volume/amount), not
    the broker counterparty — broker_id/securities_trader columns are
    dropped here (they're reachable via `tw_broker_master` joins if
    needed). Sequential `seq` per (symbol, date) makes the PK unique."""
    out: list[dict[str, Any]] = []
    counters: dict[tuple[str, str], int] = {}
    for r in rows:
        sym = str(r.get("stock_id") or "")
        d = _to_date(r.get("date"))
        key = (sym, str(d) if d else "")
        counters[key] = counters.get(key, 0) + 1
        # FinMind reports `buy` and `sell` separately in matched block
        # trades; they're equal for a paired transaction. Surface
        # `volume` = `buy` and `amount` = price × volume since the
        # endpoint doesn't ship a notional column directly.
        volume = _to_int(r.get("buy"))
        price = _to_decimal(r.get("price"))
        amount = None
        if price is not None and volume is not None:
            from decimal import Decimal
            amount = price * Decimal(volume)
        out.append({
            "market": "TWSE",
            "symbol": sym or None,
            "ts": d,
            "seq": counters[key],
            "price": price,
            "volume": volume,
            "amount": amount,
            "source": "finmind",
        })
    return out


def _batch_govt_bank_flow(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """GovernmentBankBuySell payload → tw_govt_bank_flow. Roll up the
    per-(stock, bank) rows into one (market, ts) total per day —
    summing buy_amount and sell_amount, deriving net_amount. The
    per-stock breakout is intentionally not stored; downstream
    consumers wanting it can re-fetch."""
    from decimal import Decimal

    by_date: dict[date, dict[str, Decimal]] = {}
    for r in rows:
        d = _to_date(r.get("date"))
        if d is None:
            continue
        bucket = by_date.setdefault(d, {"buy": Decimal(0), "sell": Decimal(0)})
        bb = _to_decimal(r.get("buy_amount"))
        ss = _to_decimal(r.get("sell_amount"))
        if bb is not None:
            bucket["buy"] += bb
        if ss is not None:
            bucket["sell"] += ss

    return [
        {
            "market": "TWSE",
            "ts": d,
            "buy_amount": agg["buy"],
            "sell_amount": agg["sell"],
            "net_amount": agg["buy"] - agg["sell"],
            "source": "finmind",
        }
        for d, agg in sorted(by_date.items())
    ]


def _row_broker_daily_report(row: dict[str, Any]) -> dict[str, Any]:
    """TaiwanStockTradingDailyReport row → tw_broker_daily_report.
    Per-symbol (data_id=stock_id) + single-day. Each FinMind row is
    one (broker, price) leg of trading on the symbol that day; PK is
    (market, symbol, ts, broker_id, price) so the same broker can have
    multiple price legs without collisions. The `securities_trader`
    name is dropped — joinable via `tw_broker_master.broker_id`."""
    return {
        "market": row.get("market", "TWSE"),
        "symbol": _to_str(row.get("symbol")),
        "ts": _to_date(row.get("ts")),
        "broker_id": _to_str(row.get("broker_id")),
        "price": _to_decimal(row.get("price")),
        "buy_volume": _to_int(row.get("buy_volume")),
        "sell_volume": _to_int(row.get("sell_volume")),
        "source": row.get("source", "finmind"),
    }


# ── Wide-format pivots (batch_transform) ─────────────────────────
#
# FinMind's quarterly statements + market-wide totals return rows in
# long format — one FinMind row per (entity, period, line item). To
# populate our wide local-table rows (one per entity-period with
# typed canonical columns + raw JSONB) we GROUP BY (entity, period)
# and pivot the `type`/`name` field onto columns. Common helpers live
# here; per-dataset specifics in the four batch transforms below.


def _normalize_period(date_str: str) -> tuple[str, date | None]:
    """FinMind statement dates come in as either '2024-Q1' or
    '2024-03-31'. Returns (period_code, period_end_date) — period_code
    is always YYYYQN for the local schema, and period_end is the
    quarter-end date when we can derive it.
    """
    if not date_str:
        return "", None
    s = str(date_str)
    if "Q" in s:
        # '2024-Q1' or '2024Q1' — period code as-is, derive quarter end.
        period = s.replace("-", "")
        try:
            year = int(period[:4])
            q = int(period[-1])
            month = q * 3
            day = 31 if month in (3, 12) else 30
            return period, date(year, month, day)
        except (ValueError, IndexError):
            return period, None
    # YYYY-MM-DD — derive quarter from month.
    try:
        d = _to_date(s)
        if d is None:
            return "", None
        q = (d.month - 1) // 3 + 1
        return f"{d.year}Q{q}", d
    except Exception:
        return "", None


# FinMind's `type` field uses the English line-item name. Each
# statement has dozens of types; we only promote the headline ones
# to typed columns (the rest live in `raw` JSONB for ad-hoc queries).

_INCOME_CANONICAL = {
    "Revenue": "revenue",
    "OperatingRevenue": "revenue",
    "OperatingIncome": "operating_income",
    "NetIncome": "net_income",
    "NetIncomeAttributableToParent": "net_income",
    "EPS": "eps",
    "BasicEPS": "eps",
}

_BALANCE_CANONICAL = {
    "TotalAssets": "total_assets",
    "TotalLiabilities": "total_liabilities",
    "Equity": "total_equity",
    "TotalEquity": "total_equity",
    "CashAndCashEquivalents": "cash",
    "Cash": "cash",
}

_CASHFLOW_CANONICAL = {
    "CashFlowsFromOperatingActivities": "operating_cash_flow",
    "CashFlowsFromInvestingActivities": "investing_cash_flow",
    "CashFlowsFromFinancingActivities": "financing_cash_flow",
    "FreeCashFlow": "free_cash_flow",
}


def _pivot_statement(
    rows: list[dict[str, Any]],
    canonical: dict[str, str],
) -> list[dict[str, Any]]:
    """Generic pivot for FinMind statement payloads.

    Groups by (stock_id, date) and emits one row per group with:
      - typed canonical columns where the line-item name maps
      - `raw` JSONB containing every line item for ad-hoc queries
        that need fields outside the canonical set
    """
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        symbol = r.get("stock_id") or r.get("symbol")
        date_field = r.get("date")
        if not symbol or not date_field:
            continue
        key = (str(symbol), str(date_field))

        period, period_end = _normalize_period(date_field)
        bucket = grouped.setdefault(
            key,
            {
                "symbol": str(symbol),
                "period": period,
                "period_end": period_end,
                "raw": {},
                "source": "finmind",
            },
        )

        line_type = r.get("type") or ""
        value = r.get("value")
        if line_type:
            bucket["raw"][line_type] = value
            local_col = canonical.get(line_type)
            if local_col is not None and local_col not in bucket:
                bucket[local_col] = _to_decimal(value)

    # Ensure every canonical column is present (None when absent) so
    # the UPSERT statement has uniform keys across rows.
    out = []
    for bucket in grouped.values():
        for col in set(canonical.values()):
            bucket.setdefault(col, None)
        out.append(bucket)
    return out


def _pivot_income_statement(rows):
    return _pivot_statement(rows, _INCOME_CANONICAL)


def _pivot_balance_sheet(rows):
    return _pivot_statement(rows, _BALANCE_CANONICAL)


def _pivot_cash_flow(rows):
    return _pivot_statement(rows, _CASHFLOW_CANONICAL)


# Total institutional investors is a different shape — one row per
# (date, name) where `name` ∈ {外資/投信/自營商}. Pivot to one row per
# date with foreign_net / sitc_net / dealer_net derived from buy-sell.
_INST_NAME_TO_COL = {
    # FinMind's exact strings — observed values vary slightly over time
    # so we match on a substring rather than exact equality.
    "外資": "foreign_net",
    "Foreign": "foreign_net",
    "投信": "sitc_net",
    "Investment_Trust": "sitc_net",
    "自營商": "dealer_net",
    "Dealer": "dealer_net",
}


def _pivot_total_institutional(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[date, dict[str, Any]] = {}
    for r in rows:
        d = _to_date(r.get("date"))
        if d is None:
            continue
        bucket = grouped.setdefault(
            d,
            {
                "market": "TWSE",
                "ts": d,
                "foreign_net": None,
                "sitc_net": None,
                "dealer_net": None,
                "source": "finmind",
            },
        )
        name = str(r.get("name") or "")
        col = None
        for needle, candidate_col in _INST_NAME_TO_COL.items():
            if needle in name:
                col = candidate_col
                break
        if col is None:
            continue
        buy = _to_int(r.get("buy")) or 0
        sell = _to_int(r.get("sell")) or 0
        # Net = buy - sell. Multiple FinMind rows for the same name
        # (e.g. 自營商 split into hedging vs proprietary) accumulate.
        existing = bucket[col] or 0
        bucket[col] = existing + buy - sell
    return list(grouped.values())


MAPPINGS: dict[str, DatasetMapping] = {
    "TaiwanStockPrice": DatasetMapping(
        dataset_code="TaiwanStockPrice",
        local_table="ohlcv_daily",
        column_map={
            "date": "ts",
            "stock_id": "symbol",
            "open": "open",
            "max": "high",
            "min": "low",
            "close": "close",
            "Trading_Volume": "volume",
        },
        pk_columns=("market", "symbol", "ts"),
        extra={"market": "TWSE", "source": "finmind"},
        row_transform=_row_ohlcv,
    ),
    "TaiwanStockMarginPurchaseShortSale": DatasetMapping(
        dataset_code="TaiwanStockMarginPurchaseShortSale",
        local_table="tw_margin_daily",
        column_map={
            "date": "ts",
            "stock_id": "symbol",
            "MarginPurchaseBuy": "margin_purchase",
            "MarginPurchaseSell": "margin_sale",
            "MarginPurchaseTodayBalance": "margin_balance",
            "ShortSaleBuy": "short_cover",
            "ShortSaleSell": "short_sale",
            "ShortSaleTodayBalance": "short_balance",
        },
        pk_columns=("market", "symbol", "ts"),
        extra={"market": "TWSE", "source": "finmind"},
        row_transform=_row_margin,
    ),
    "TaiwanStockInstitutionalInvestorsBuySell": DatasetMapping(
        dataset_code="TaiwanStockInstitutionalInvestorsBuySell",
        local_table="tw_institutional_daily",
        column_map={
            "date": "ts",
            "stock_id": "symbol",
            "Foreign_Investor_Buy": "foreign_buy",
            "Foreign_Investor_Sell": "foreign_sell",
            "Investment_Trust_Buy": "sitc_buy",
            "Investment_Trust_Sell": "sitc_sell",
            "Dealer_Buy": "dealer_buy",
            "Dealer_Sell": "dealer_sell",
        },
        pk_columns=("market", "symbol", "ts"),
        extra={"market": "TWSE", "source": "finmind"},
        row_transform=_row_institutional,
    ),
    "TaiwanStockMonthRevenue": DatasetMapping(
        dataset_code="TaiwanStockMonthRevenue",
        local_table="tw_revenue_monthly",
        column_map={
            "date": "ts",
            "stock_id": "symbol",
            "revenue": "revenue",
            "revenue_year": "revenue_yoy",
            "revenue_month": "revenue_mom",
        },
        pk_columns=("symbol", "ts"),
        extra={"source": "finmind"},
        row_transform=_row_revenue,
    ),
    "TaiwanStockTotalMarginPurchaseShortSale": DatasetMapping(
        dataset_code="TaiwanStockTotalMarginPurchaseShortSale",
        local_table="tw_total_margin_daily",
        column_map={
            "date": "ts",
            "MarginPurchaseTodayBalance": "margin_balance",
            "MarginPurchaseBuy": "margin_purchase",
            "MarginPurchaseSell": "margin_sale",
            "ShortSaleTodayBalance": "short_balance",
            "ShortSaleSell": "short_sale",
            "ShortSaleBuy": "short_cover",
        },
        pk_columns=("market", "ts"),
        extra={"market": "TWSE", "source": "finmind"},
        row_transform=_row_total_margin,
    ),
    # ── PER / 估值 ──────────────────────────────────────────────
    "TaiwanStockPER": DatasetMapping(
        dataset_code="TaiwanStockPER",
        local_table="tw_stock_per_daily",
        column_map={
            "date": "ts",
            "stock_id": "symbol",
            "PER": "per",
            "PBR": "pbr",
            "dividend_yield": "dividend_yield",
        },
        pk_columns=("market", "symbol", "ts"),
        extra={"market": "TWSE", "source": "finmind"},
        row_transform=_row_per,
    ),
    # ── 還原股價 ────────────────────────────────────────────────
    "TaiwanStockPriceAdj": DatasetMapping(
        dataset_code="TaiwanStockPriceAdj",
        local_table="tw_stock_price_adj",
        column_map={
            "date": "ts",
            "stock_id": "symbol",
            # FinMind's TaiwanStockPriceAdj returns the same OHLCV
            # shape as TaiwanStockPrice but with adjusted values; we
            # pull the adjusted close into adj_close.
            "close": "adj_close",
        },
        pk_columns=("market", "symbol", "ts"),
        extra={"market": "TWSE", "source": "finmind"},
        row_transform=_row_price_adj,
    ),
    # ── 外資持股 ────────────────────────────────────────────────
    "TaiwanStockShareholding": DatasetMapping(
        dataset_code="TaiwanStockShareholding",
        local_table="tw_foreign_shareholding",
        column_map={
            "date": "ts",
            "stock_id": "symbol",
            "ForeignInvestmentRemainingRatio": "foreign_holding_pct",
            "ForeignInvestmentRemainShares": "foreign_holding_shares",
            "NumberOfSharesIssued": "available_shares",
        },
        pk_columns=("market", "symbol", "ts"),
        extra={"market": "TWSE", "source": "finmind"},
        row_transform=_row_shareholding,
    ),
    # ── 個股市值 ────────────────────────────────────────────────
    "TaiwanStockMarketValue": DatasetMapping(
        dataset_code="TaiwanStockMarketValue",
        local_table="tw_market_value",
        column_map={
            "date": "ts",
            "stock_id": "symbol",
            "MarketValue": "market_cap",
            "shares_issued": "issued_shares",
        },
        pk_columns=("market", "symbol", "ts"),
        extra={"market": "TWSE", "source": "finmind"},
        row_transform=_row_market_value,
    ),
    # ── 股利公告 ────────────────────────────────────────────────
    "TaiwanStockDividend": DatasetMapping(
        dataset_code="TaiwanStockDividend",
        local_table="tw_dividend",
        column_map={
            # FinMind returns one row per (stock, year) — `date` is
            # the announcement date, not ex-date, so it goes to PK.
            "date": "announce_date",
            "stock_id": "symbol",
            "CashEarningsDistribution": "cash_dividend",
            "StockEarningsDistribution": "stock_dividend",
            "CashExDividendTradingDate": "ex_dividend_date",
            "StockExDividendTradingDate": "ex_rights_date",
        },
        pk_columns=("symbol", "announce_date"),
        extra={"source": "finmind"},
        row_transform=_row_dividend,
    ),
    # ── 庫藏股 ──────────────────────────────────────────────────
    "TaiwanStockBuyBack": DatasetMapping(
        dataset_code="TaiwanStockBuyBack",
        local_table="tw_buyback",
        column_map={
            "date": "announced_at",
            "stock_id": "symbol",
            "BuyBackStartDate": "started_at",
            "BuyBackEndDate": "ended_at",
            "BuyBackPlanQuantity": "plan_shares",
            "BuyBackActualQuantity": "actual_shares",
            "BuyBackAveragePrice": "avg_price",
        },
        pk_columns=("symbol", "announced_at"),
        extra={"source": "finmind"},
        row_transform=_row_buyback,
    ),
    # ── 上市櫃公司基本資料 ───────────────────────────────────────
    "TaiwanStockInfo": DatasetMapping(
        dataset_code="TaiwanStockInfo",
        local_table="tw_stock_info",
        column_map={
            "stock_id": "symbol",
            "stock_name": "name_zh",
            "industry_category": "industry_category",
            "type": "market",
            "date": "listed_at",
        },
        pk_columns=("market", "symbol"),
        extra={"source": "finmind"},
        row_transform=_row_stock_info,
    ),
    # ── 期貨日成交資訊 ──────────────────────────────────────────
    "TaiwanFuturesDaily": DatasetMapping(
        dataset_code="TaiwanFuturesDaily",
        local_table="tw_futures_daily",
        column_map={
            "date": "ts",
            "futures_id": "contract",
            "open": "open",
            "max": "high",
            "min": "low",
            "close": "close",
            "volume": "volume",
            "open_interest": "open_interest",
            "settlement_price": "settlement_price",
        },
        pk_columns=("contract", "ts"),
        extra={"source": "finmind"},
        row_transform=_row_futures_daily,
    ),
    # ── 選擇權日成交資訊 ─────────────────────────────────────────
    "TaiwanOptionDaily": DatasetMapping(
        dataset_code="TaiwanOptionDaily",
        local_table="tw_option_daily",
        column_map={
            "date": "ts",
            "option_id": "contract",
            "strike_price": "strike",
            "call_put": "call_put",
            "open": "open",
            "max": "high",
            "min": "low",
            "close": "close",
            "volume": "volume",
            "open_interest": "open_interest",
        },
        pk_columns=("contract", "strike", "call_put", "ts"),
        extra={"source": "finmind"},
        row_transform=_row_option_daily,
    ),
    # ── 季報三表 (wide-format batch_transform) ─────────────────
    "TaiwanStockFinancialStatements": DatasetMapping(
        dataset_code="TaiwanStockFinancialStatements",
        local_table="tw_income_statement",
        column_map={},  # ignored — batch_transform owns column resolution
        pk_columns=("symbol", "period"),
        batch_transform=_pivot_income_statement,
    ),
    "TaiwanStockBalanceSheet": DatasetMapping(
        dataset_code="TaiwanStockBalanceSheet",
        local_table="tw_balance_sheet",
        column_map={},
        pk_columns=("symbol", "period"),
        batch_transform=_pivot_balance_sheet,
    ),
    "TaiwanStockCashFlowsStatement": DatasetMapping(
        dataset_code="TaiwanStockCashFlowsStatement",
        local_table="tw_cash_flow",
        column_map={},
        pk_columns=("symbol", "period"),
        batch_transform=_pivot_cash_flow,
    ),
    # ── 整體市場三大法人 (long → wide pivot) ────────────────────
    "TaiwanStockTotalInstitutionalInvestors": DatasetMapping(
        dataset_code="TaiwanStockTotalInstitutionalInvestors",
        local_table="tw_total_inst_daily",
        column_map={},
        pk_columns=("market", "ts"),
        batch_transform=_pivot_total_institutional,
    ),
    # ── 除權息結果 ──────────────────────────────────────────────
    "TaiwanStockDividendResult": DatasetMapping(
        dataset_code="TaiwanStockDividendResult",
        local_table="tw_dividend_result",
        column_map={
            "date": "ex_date",
            "stock_id": "symbol",
            "before_price": "before_price",
            "after_price": "after_price",
            "stock_or_cache_dividend_price": "cash_dividend",
            "stock_dividend_price": "stock_dividend",
        },
        pk_columns=("symbol", "ex_date"),
        extra={"source": "finmind"},
        row_transform=_row_dividend_result,
    ),
    # ── 股票分割 ──────────────────────────────────────────────
    "TaiwanStockSplitPrice": DatasetMapping(
        dataset_code="TaiwanStockSplitPrice",
        local_table="tw_split",
        column_map={
            "date": "ex_date",
            "stock_id": "symbol",
            "before_price": "before_price",
            "after_price": "after_price",
            "split_ratio": "split_ratio",
        },
        pk_columns=("symbol", "ex_date"),
        extra={"source": "finmind"},
        row_transform=_row_split,
    ),
    # ── 當沖 ────────────────────────────────────────────────────
    "TaiwanStockDayTrading": DatasetMapping(
        dataset_code="TaiwanStockDayTrading",
        local_table="tw_day_trade_daily",
        column_map={
            "date": "ts",
            "stock_id": "symbol",
            "BuyAfterSale": "buy_volume",
            "SellAfterBuy": "sell_volume",
            "BuyAfterSaleAmount": "buy_amount",
            "SellAfterBuyAmount": "sell_amount",
        },
        pk_columns=("market", "symbol", "ts"),
        extra={"market": "TWSE", "source": "finmind"},
        row_transform=_row_day_trade,
    ),
    # ── 借券 ────────────────────────────────────────────────────
    "TaiwanStockSecuritiesLending": DatasetMapping(
        dataset_code="TaiwanStockSecuritiesLending",
        local_table="tw_securities_lending",
        column_map={
            "date": "ts",
            "stock_id": "symbol",
            "transaction_type": "transaction_type",
            "volume": "volume",
            "fee_rate": "fee_rate",
        },
        pk_columns=("market", "symbol", "ts", "transaction_type"),
        extra={"market": "TWSE", "source": "finmind"},
        row_transform=_row_securities_lending,
    ),
    # ── 期貨三大法人 (day session) ─────────────────────────────
    # The night-session sibling (TaiwanFuturesInstitutionalInvestors-
    # AfterHours) shares the same destination table — `extra.session`
    # discriminates. Adding the night-session mapping is one entry
    # below with extra={"session": "night"}.
    "TaiwanFuturesInstitutionalInvestors": DatasetMapping(
        dataset_code="TaiwanFuturesInstitutionalInvestors",
        local_table="tw_futures_inst_daily",
        column_map={
            "date": "ts",
            "futures_id": "contract",
            "long_open_interest_balance_volume_foreign_investment": "foreign_long_open_interest",
            "short_open_interest_balance_volume_foreign_investment": "foreign_short_open_interest",
            "long_open_interest_balance_volume_investment_trust": "sitc_long_open_interest",
            "short_open_interest_balance_volume_investment_trust": "sitc_short_open_interest",
            "long_open_interest_balance_volume_dealer": "dealer_long_open_interest",
            "short_open_interest_balance_volume_dealer": "dealer_short_open_interest",
        },
        pk_columns=("contract", "ts", "session"),
        extra={"session": "day", "source": "finmind"},
        row_transform=_row_futures_inst,
    ),
    "TaiwanFuturesInstitutionalInvestorsAfterHours": DatasetMapping(
        dataset_code="TaiwanFuturesInstitutionalInvestorsAfterHours",
        local_table="tw_futures_inst_daily",
        column_map={
            "date": "ts",
            "futures_id": "contract",
            "long_open_interest_balance_volume_foreign_investment": "foreign_long_open_interest",
            "short_open_interest_balance_volume_foreign_investment": "foreign_short_open_interest",
            "long_open_interest_balance_volume_investment_trust": "sitc_long_open_interest",
            "short_open_interest_balance_volume_investment_trust": "sitc_short_open_interest",
            "long_open_interest_balance_volume_dealer": "dealer_long_open_interest",
            "short_open_interest_balance_volume_dealer": "dealer_short_open_interest",
        },
        pk_columns=("contract", "ts", "session"),
        extra={"session": "night", "source": "finmind"},
        row_transform=_row_futures_inst,
    ),
    # ── 市值比重 ────────────────────────────────────────────────
    "TaiwanStockMarketValueWeight": DatasetMapping(
        dataset_code="TaiwanStockMarketValueWeight",
        local_table="tw_market_value_weight",
        column_map={
            "date": "ts",
            "stock_id": "symbol",
            "weight_per": "weight",
            "TotalMarketValue": "market_cap",
        },
        pk_columns=("market", "symbol", "ts"),
        extra={"market": "TWSE", "source": "finmind"},
        row_transform=_row_market_value_weight,
    ),
    # ── 漲跌停 ──────────────────────────────────────────────────
    "TaiwanStockPriceLimit": DatasetMapping(
        dataset_code="TaiwanStockPriceLimit",
        local_table="tw_price_limit_daily",
        column_map={
            "date": "ts",
            "stock_id": "symbol",
            "PriceUpLimit": "upper_limit",
            "PriceDownLimit": "lower_limit",
        },
        pk_columns=("market", "symbol", "ts"),
        extra={"market": "TWSE", "source": "finmind"},
        row_transform=_row_price_limit,
    ),
    # ── 暫停交易 ────────────────────────────────────────────────
    "TaiwanStockSuspended": DatasetMapping(
        dataset_code="TaiwanStockSuspended",
        local_table="tw_suspended",
        column_map={
            "stock_id": "symbol",
            "suspend_date": "suspended_at",
            "resume_date": "resumed_at",
            "reason": "reason",
        },
        pk_columns=("symbol", "suspended_at"),
        extra={"source": "finmind"},
        row_transform=_row_suspended,
    ),
    # ── 景氣對策信號 ────────────────────────────────────────────
    "TaiwanBusinessIndicator": DatasetMapping(
        dataset_code="TaiwanBusinessIndicator",
        local_table="tw_business_indicator",
        column_map={
            "date": "ts",
            "score": "score",
            "signal": "signal",
        },
        pk_columns=("ts",),
        extra={"source": "finmind"},
        row_transform=_row_business_indicator,
    ),
    # ── 下市 ────────────────────────────────────────────────────
    "TaiwanStockDelisting": DatasetMapping(
        dataset_code="TaiwanStockDelisting",
        local_table="tw_delisting",
        column_map={
            "stock_id": "symbol",
            "date": "delisted_at",
            "reason": "reason",
        },
        pk_columns=("symbol",),
        extra={"source": "finmind"},
        row_transform=_row_delisting,
    ),
    # ── 新聞 ────────────────────────────────────────────────────
    "TaiwanStockNews": DatasetMapping(
        dataset_code="TaiwanStockNews",
        local_table="tw_news_articles",
        column_map={
            "date": "published_at",
            "stock_id": "symbol",
            "title": "title",
            "link": "link",
            "description": "summary",
        },
        # `tw_news_articles` PK is `id` (autoincrement) with a UNIQUE
        # on `sha256` for dedup. Listing `sha256` as the conflict key
        # makes the UPSERT idempotent across re-ingest of the same
        # article from different sources (FinMind + Google RSS).
        pk_columns=("sha256",),
        extra={"market": "TW", "source": "finmind"},
        row_transform=_row_news,
    ),
    "TaiwanSecuritiesTraderInfo": DatasetMapping(
        dataset_code="TaiwanSecuritiesTraderInfo",
        local_table="tw_broker_master",
        column_map={
            "securities_trader_id": "broker_id",
            "securities_trader": "name_zh",
            "address": "address",
            "phone": "phone",
        },
        pk_columns=("broker_id",),
        extra={"source": "finmind"},
        row_transform=_row_broker_master,
    ),
    "TaiwanStockDispositionSecuritiesPeriod": DatasetMapping(
        dataset_code="TaiwanStockDispositionSecuritiesPeriod",
        local_table="tw_disposition",
        column_map={
            "stock_id": "symbol",
            "period_start": "started_at",
            "period_end": "ended_at",
            "condition": "reason",
        },
        pk_columns=("symbol", "started_at"),
        extra={"source": "finmind"},
        row_transform=_row_disposition,
    ),
    "TaiwanStockHoldingSharesPer": DatasetMapping(
        dataset_code="TaiwanStockHoldingSharesPer",
        local_table="tw_holdings_aggregates",
        column_map={
            "date": "ts",
            "stock_id": "symbol",
            "HoldingSharesLevel": "bracket",
            "people": "holders",
            "unit": "shares",
            "percent": "pct",
        },
        pk_columns=("symbol", "ts", "bracket"),
        extra={"source": "finmind"},
        row_transform=_row_holdings_aggregates,
    ),
    "TaiwanTotalExchangeMarginMaintenance": DatasetMapping(
        dataset_code="TaiwanTotalExchangeMarginMaintenance",
        local_table="tw_margin_maintenance",
        column_map={
            "date": "ts",
            "TotalExchangeMarginMaintenance": "maintenance_pct",
        },
        pk_columns=("market", "ts"),
        extra={"market": "TWSE", "source": "finmind"},
        row_transform=_row_margin_maintenance,
    ),
    "TaiwanStockConvertibleBondInfo": DatasetMapping(
        dataset_code="TaiwanStockConvertibleBondInfo",
        local_table="tw_cb_info",
        column_map={
            "cb_id": "cb_id",
            "cb_name": "name_zh",
            "InitialDateOfConversion": "issue_date",
            "DueDateOfConversion": "maturity_date",
            "IssuanceAmount": "par_value",
        },
        pk_columns=("cb_id",),
        extra={"source": "finmind"},
        row_transform=_row_cb_info,
    ),
    "TaiwanStockIndustryChain": DatasetMapping(
        dataset_code="TaiwanStockIndustryChain",
        local_table="tw_industry_chain",
        column_map={
            "stock_id": "symbol",
            "industry": "industry",
            "sub_industry": "sub_industry",
        },
        # PK is (symbol, industry); when FinMind reports the same stock
        # in multiple sub_industries within one industry the last row
        # wins on UPSERT. That's a known precision loss but acceptable
        # at this granularity — the schema treats one (stock, industry)
        # pair as having one canonical sub-industry.
        pk_columns=("symbol", "industry"),
        extra={"source": "finmind"},
        row_transform=_row_industry_chain,
    ),
    "TaiwanStockDayTradingSuspension": DatasetMapping(
        dataset_code="TaiwanStockDayTradingSuspension",
        local_table="tw_suspended",
        column_map={
            "stock_id": "symbol",
            "date": "suspended_at",
            "end_date": "resumed_at",
            "reason": "reason",
        },
        pk_columns=("symbol", "suspended_at"),
        extra={"source": "finmind"},
        row_transform=_row_suspended,
    ),
    "TaiwanStockInfoWithWarrant": DatasetMapping(
        dataset_code="TaiwanStockInfoWithWarrant",
        local_table="tw_stock_info",
        column_map={
            "stock_id": "symbol",
            "stock_name": "name_zh",
            "industry_category": "industry_category",
            "type": "market",
            "date": "listed_at",
        },
        pk_columns=("market", "symbol"),
        extra={"source": "finmind"},
        # Shared destination with TaiwanStockInfo. Re-ingesting an
        # equity row already seeded by TaiwanStockInfo overwrites
        # is_warrant=False with the recomputed value (still False for
        # equities, True for warrants); the row_transform decides.
        row_transform=_row_stock_info_with_warrant,
    ),
    "TaiwanStockTradingDate": DatasetMapping(
        dataset_code="TaiwanStockTradingDate",
        local_table="tw_trading_calendar",
        column_map={
            "date": "ts",
        },
        pk_columns=("market", "ts"),
        extra={"market": "TWSE", "source": "finmind"},
        row_transform=_row_trading_calendar,
    ),
    "TaiwanStockTotalReturnIndex": DatasetMapping(
        dataset_code="TaiwanStockTotalReturnIndex",
        local_table="tw_total_return_index",
        column_map={
            "stock_id": "symbol",
            "date": "ts",
            "price": "value",
        },
        pk_columns=("market", "symbol", "ts"),
        extra={"market": "TWSE", "source": "finmind"},
        # FinMind requires `data_id` for this dataset — TWSE indices like
        # 'TAIEX', 'OTC', 'FRMSA'. The market-wide flow (data_id="") gets
        # HTTP 400. Operators must invoke via `backfill --dataset
        # TaiwanStockTotalReturnIndex --symbols-file <indices.txt>` or
        # set per_symbol=True in the catalog with an indices universe.
        row_transform=_row_total_return_index,
    ),
    "TaiwanStockParValueChange": DatasetMapping(
        dataset_code="TaiwanStockParValueChange",
        local_table="tw_par_value_change",
        column_map={
            "stock_id": "symbol",
            "date": "ex_date",
            "after_ref_close": "reference_price",
        },
        pk_columns=("symbol", "ex_date"),
        extra={"source": "finmind"},
        row_transform=_row_par_value_change,
    ),
    "TaiwanStockKBar": DatasetMapping(
        dataset_code="TaiwanStockKBar",
        local_table="tw_stock_minute",
        column_map={
            "stock_id": "symbol",
            "date": "ts",
            "minute": "minute_str",
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "volume": "volume",
        },
        pk_columns=("market", "symbol", "ts"),
        extra={"market": "TWSE", "source": "finmind"},
        row_transform=_row_stock_minute,
        single_day=True,
    ),
    "TaiwanStockPriceTick": DatasetMapping(
        dataset_code="TaiwanStockPriceTick",
        local_table="tw_stock_tick",
        # batch_transform owns column resolution; column_map kept empty
        # so the runner doesn't double-rename.
        column_map={},
        pk_columns=("market", "symbol", "ts", "seq"),
        batch_transform=_batch_stock_tick,
        single_day=True,
    ),
    "TaiwanStockBlockTradingDailyReport": DatasetMapping(
        dataset_code="TaiwanStockBlockTradingDailyReport",
        local_table="tw_block_trade",
        column_map={},
        pk_columns=("market", "symbol", "ts", "seq"),
        batch_transform=_batch_block_trade,
        single_day=True,
    ),
    "TaiwanStockGovernmentBankBuySell": DatasetMapping(
        dataset_code="TaiwanStockGovernmentBankBuySell",
        local_table="tw_govt_bank_flow",
        column_map={},
        pk_columns=("market", "ts"),
        batch_transform=_batch_govt_bank_flow,
        single_day=True,
    ),
    "TaiwanStockTradingDailyReport": DatasetMapping(
        dataset_code="TaiwanStockTradingDailyReport",
        local_table="tw_broker_daily_report",
        column_map={
            "stock_id": "symbol",
            "date": "ts",
            "securities_trader_id": "broker_id",
            "price": "price",
            "buy": "buy_volume",
            "sell": "sell_volume",
        },
        # PK includes `price` because the same broker can have multiple
        # price legs (buy/sell at different fills) for the same stock
        # on the same day.
        pk_columns=("market", "symbol", "ts", "broker_id", "price"),
        extra={"market": "TWSE", "source": "finmind"},
        row_transform=_row_broker_daily_report,
        single_day=True,
    ),
}


def find_mapping(dataset_code: str) -> DatasetMapping:
    """Resolve dataset_code → DatasetMapping. Raises
    MappingNotFoundError when the runner doesn't know how to
    transform this dataset's payload yet."""
    m = MAPPINGS.get(dataset_code)
    if m is None:
        raise MappingNotFoundError(
            f"no ingest mapping for {dataset_code} — append one to "
            f"finmind/ingest/mappings.py to enable backfill"
        )
    return m


def transform_row(
    finmind_row: dict[str, Any], mapping: DatasetMapping
) -> dict[str, Any]:
    """FinMind row dict → local-table row dict.

    Three steps:
      1. Rename columns per `column_map` (drop unknown columns).
      2. Inject `extra` static fields.
      3. Run `row_transform` if present (type coercion + derived cols).
    """
    renamed: dict[str, Any] = {}
    for fm_col, local_col in mapping.column_map.items():
        if fm_col in finmind_row:
            renamed[local_col] = finmind_row[fm_col]
    renamed.update(mapping.extra)
    if mapping.row_transform is not None:
        renamed = mapping.row_transform(renamed)
    return renamed
