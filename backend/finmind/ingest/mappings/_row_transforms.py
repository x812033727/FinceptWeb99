"""Per-row FinMind → local-table transforms.

One `_row_*` function per dataset that ingests row-by-row (most of the
TWSE+TPEX cash-market datasets fit this shape). Each function takes
the column-mapped row dict and returns a typed local-table row dict
ready for ON CONFLICT upsert. Type coercion via `_to_*` helpers from
`._types`.

Wide-format and intraday-fan-out datasets live in `._batch_transforms`
because they need access to multiple FinMind rows at once."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from ._types import (
    _to_date,
    _to_datetime,
    _to_decimal,
    _to_int,
    _to_str,
)




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



def _row_crypto_ohlcv(row: dict[str, Any]) -> dict[str, Any]:
    """Binance kline → crypto_ohlcv row. `ts` is TIMESTAMPTZ (sub-day
    granularity for 1h bars) so it uses `_to_datetime`, not `_to_date`.
    Prices/volumes stay Decimal to preserve sub-cent alt-coin precision;
    `trades` is the bar's trade count."""
    return {
        "market": row.get("market", "BINANCE"),
        "symbol": _to_str(row.get("symbol")),
        "interval": _to_str(row.get("interval")),
        "ts": _to_datetime(row.get("ts")),
        "open": _to_decimal(row.get("open")),
        "high": _to_decimal(row.get("high")),
        "low": _to_decimal(row.get("low")),
        "close": _to_decimal(row.get("close")),
        "volume": _to_decimal(row.get("volume")),
        "quote_volume": _to_decimal(row.get("quote_volume")),
        "trades": _to_int(row.get("trades")),
        "source": row.get("source", "binance"),
    }


def _row_crypto_funding_rate(row: dict[str, Any]) -> dict[str, Any]:
    """Binance perp funding-rate row → crypto_funding_rate. funding_time
    is TIMESTAMPTZ; funding_rate keeps full precision (~1e-4 magnitude)."""
    return {
        "market": row.get("market", "BINANCE"),
        "symbol": _to_str(row.get("symbol")),
        "funding_time": _to_datetime(row.get("funding_time")),
        "funding_rate": _to_decimal(row.get("funding_rate")),
        "mark_price": _to_decimal(row.get("mark_price")),
        "source": row.get("source", "binance"),
    }


def _row_crypto_open_interest(row: dict[str, Any]) -> dict[str, Any]:
    """Binance perp open-interest row → crypto_open_interest."""
    return {
        "market": row.get("market", "BINANCE"),
        "symbol": _to_str(row.get("symbol")),
        "ts": _to_datetime(row.get("ts")),
        "open_interest": _to_decimal(row.get("open_interest")),
        "open_interest_value": _to_decimal(row.get("open_interest_value")),
        "source": row.get("source", "binance"),
    }


def _row_crypto_asset_info(row: dict[str, Any]) -> dict[str, Any]:
    """CoinGecko markets row → crypto_asset_info. snapshot_date is
    stamped by the coingecko self-crawl handler (the chunk's date), so
    each daily run appends one row per coin keyed by (snapshot_date, id)."""
    return {
        "snapshot_date": _to_date(row.get("snapshot_date")),
        "coingecko_id": _to_str(row.get("coingecko_id")),
        "symbol": _to_str(row.get("symbol")),
        "name": _to_str(row.get("name")),
        "market_cap_rank": _to_int(row.get("market_cap_rank")),
        "market_cap": _to_decimal(row.get("market_cap")),
        "circulating_supply": _to_decimal(row.get("circulating_supply")),
        "total_supply": _to_decimal(row.get("total_supply")),
        "ath": _to_decimal(row.get("ath")),
        "source": row.get("source", "coingecko"),
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
    # FinMind's `TaiwanStockInfoWithWarrant` ships ALL listings — equities,
    # ETFs, warrants, and DRs — under industry_category `全部(不含大盤、
    # 指數)`. The previous heuristic flagged every row as `is_warrant=True`
    # which polluted `tw_stock_info` (2330 was being flagged as a warrant)
    # and forced downstream universe filtering to fall back to symbol-
    # length regex. The proper signal is in `stock_name`:
    #   - Warrants embed `認購` (call) / `認售` (put) verbatim
    #   - Bull/bear certificates embed `牛` / `熊`
    #   - Plain `權證` also appears in some issuer-specific names
    # ETFs and DRs (e.g. `元大台商50`, `恒大健-DR`) match none of the
    # above and stay `is_warrant=False`.
    market_raw = (row.get("market") or "").lower()
    market = "TWSE" if "twse" in market_raw or market_raw == "twse" else (
        "OTC" if market_raw in ("otc", "tpex") else market_raw.upper() or "TWSE"
    )
    name = row.get("name_zh") or ""
    is_warrant = any(tag in name for tag in ("認購", "認售", "權證")) or (
        # `牛` / `熊` only when followed by a digit (e.g. `牛01`) to
        # avoid false positives on company names containing those
        # characters legitimately.
        any(t in name for t in ("牛", "熊"))
        and any(c.isdigit() for c in name)
    )
    return {
        "market": market,
        "symbol": _to_str(row.get("symbol")),
        "name_zh": _to_str(name),
        "industry_category": _to_str(row.get("industry_category")),
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



# FinMind `TaiwanStockLoanCollateralBalance` field map: 5 product
# groups × 6–7 lifecycle fields. Defined as a tuple so the row
# transform stays declarative and the matching migration can keep
# the column list in sync.
_LOAN_COLLATERAL_FIELDS: tuple[tuple[str, str], ...] = (
    # (FinMind field name,  local column name)
    # Margin (融資) — 6 fields
    ("MarginPreviousDayBalance",  "margin_previous_day_balance"),
    ("MarginBuy",                 "margin_buy"),
    ("MarginSell",                "margin_sell"),
    ("MarginCashRedemption",      "margin_cash_redemption"),
    ("MarginCurrentDayBalance",   "margin_current_day_balance"),
    ("MarginNextDayQuota",        "margin_next_day_quota"),
    # SecuritiesFirmLoan (券商借券) — 7 fields
    ("SecuritiesFirmLoanPreviousDayBalance", "securities_firm_loan_previous_day_balance"),
    ("SecuritiesFirmLoanBuy",                "securities_firm_loan_buy"),
    ("SecuritiesFirmLoanSell",               "securities_firm_loan_sell"),
    ("SecuritiesFirmLoanCashRedemption",     "securities_firm_loan_cash_redemption"),
    ("SecuritiesFirmLoanReplacement",        "securities_firm_loan_replacement"),
    ("SecuritiesFirmLoanCurrentDayBalance",  "securities_firm_loan_current_day_balance"),
    ("SecuritiesFirmLoanNextDayQuota",       "securities_firm_loan_next_day_quota"),
    # UnrestrictedLoan (一般借券) — 7 fields
    ("UnrestrictedLoanPreviousDayBalance",   "unrestricted_loan_previous_day_balance"),
    ("UnrestrictedLoanBuy",                  "unrestricted_loan_buy"),
    ("UnrestrictedLoanSell",                 "unrestricted_loan_sell"),
    ("UnrestrictedLoanCashRedemption",       "unrestricted_loan_cash_redemption"),
    ("UnrestrictedLoanReplacement",          "unrestricted_loan_replacement"),
    ("UnrestrictedLoanCurrentDayBalance",    "unrestricted_loan_current_day_balance"),
    ("UnrestrictedLoanNextDayQuota",         "unrestricted_loan_next_day_quota"),
    # SecuritiesFinanceSecuredLoan (集保有擔保借券) — 7 fields
    ("SecuritiesFinanceSecuredLoanPreviousDayBalance", "securities_finance_secured_loan_previous_day_balance"),
    ("SecuritiesFinanceSecuredLoanBuy",                "securities_finance_secured_loan_buy"),
    ("SecuritiesFinanceSecuredLoanSell",               "securities_finance_secured_loan_sell"),
    ("SecuritiesFinanceSecuredLoanCashRedemption",     "securities_finance_secured_loan_cash_redemption"),
    ("SecuritiesFinanceSecuredLoanReplacement",        "securities_finance_secured_loan_replacement"),
    ("SecuritiesFinanceSecuredLoanCurrentDayBalance",  "securities_finance_secured_loan_current_day_balance"),
    ("SecuritiesFinanceSecuredLoanNextDayQuota",       "securities_finance_secured_loan_next_day_quota"),
    # SettlementMargin (交割融資) — 7 fields
    ("SettlementMarginPreviousDayBalance",  "settlement_margin_previous_day_balance"),
    ("SettlementMarginBuy",                 "settlement_margin_buy"),
    ("SettlementMarginSell",                "settlement_margin_sell"),
    ("SettlementMarginCashRedemption",      "settlement_margin_cash_redemption"),
    ("SettlementMarginReplacement",         "settlement_margin_replacement"),
    ("SettlementMarginCurrentDayBalance",   "settlement_margin_current_day_balance"),
    ("SettlementMarginNextDayQuota",        "settlement_margin_next_day_quota"),
)



def _row_loan_collateral(row: dict[str, Any]) -> dict[str, Any]:
    """TaiwanStockLoanCollateralBalance → tw_loan_collateral. column_map
    handles the per-field rename; here we coerce numerics to int (the
    schema is BIGINT) and fold FinMind's `市場別` label `集中市場` /
    `店頭市場` into our internal market codes."""
    market_raw = (row.get("market") or "").strip()
    if market_raw in ("集中市場", "TWSE"):
        market = "TWSE"
    elif market_raw in ("店頭市場", "OTC", "TPEX"):
        market = "OTC"
    else:
        market = "TWSE"
    out: dict[str, Any] = {
        "market": market,
        "symbol": _to_str(row.get("symbol")),
        "ts": _to_date(row.get("ts")),
        "source": row.get("source", "finmind"),
    }
    for _, local_col in _LOAN_COLLATERAL_FIELDS:
        out[local_col] = _to_int(row.get(local_col))
    return out



def _row_short_sale_suspension(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": _to_str(row.get("symbol")),
        "suspended_at": _to_date(row.get("suspended_at")),
        "resumed_at": _to_date(row.get("resumed_at")),
        "reason": _to_str(row.get("reason")),
        "source": row.get("source", "finmind"),
    }



def _row_day_trade_fee(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "market": row.get("market", "TWSE"),
        "symbol": _to_str(row.get("symbol")),
        "ts": _to_date(row.get("ts")),
        "fee_rate": _to_decimal(row.get("fee_rate")),
        "source": row.get("source", "finmind"),
    }



def _row_cb_inst_daily(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "cb_id": _to_str(row.get("cb_id")),
        "ts": _to_date(row.get("ts")),
        "foreign_buy": _to_int(row.get("foreign_buy")),
        "foreign_sell": _to_int(row.get("foreign_sell")),
        "sitc_buy": _to_int(row.get("sitc_buy")),
        "sitc_sell": _to_int(row.get("sitc_sell")),
        "dealer_buy": _to_int(row.get("dealer_buy")),
        "dealer_sell": _to_int(row.get("dealer_sell")),
        "source": row.get("source", "finmind"),
    }



def _row_futures_settlement(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "contract": _to_str(row.get("contract")),
        "settlement_date": _to_date(row.get("settlement_date")),
        "final_settlement_price": _to_decimal(row.get("final_settlement_price")),
        "source": row.get("source", "finmind"),
    }



def _row_futures_spread(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "ts": _to_date(row.get("ts")),
        "spread_pair": _to_str(row.get("spread_pair")),
        "open": _to_decimal(row.get("open")),
        "close": _to_decimal(row.get("close")),
        "volume": _to_int(row.get("volume")),
        "source": row.get("source", "finmind"),
    }



def _row_option_settlement(row: dict[str, Any]) -> dict[str, Any]:
    """TaiwanOptionFinalSettlementPrice → tw_option_settlement.
    FinMind ships per-(option, contract_month) settlement, no
    strike/call_put — see migration 0016 for the PK relaxation."""
    return {
        "contract": _to_str(row.get("contract")),
        "contract_month": _to_str(row.get("contract_month")) or "",
        "settlement_date": _to_date(row.get("settlement_date")),
        "final_settlement_price": _to_decimal(row.get("final_settlement_price")),
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



def _row_short_sale_balance(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "market": row.get("market", "TWSE"),
        "symbol": _to_str(row.get("symbol")),
        "ts": _to_date(row.get("ts")),
        "margin_prev_balance":     _to_int(row.get("margin_prev_balance")),
        "margin_short_sales":      _to_int(row.get("margin_short_sales")),
        "margin_short_covering":   _to_int(row.get("margin_short_covering")),
        "margin_stock_redemption": _to_int(row.get("margin_stock_redemption")),
        "margin_balance":          _to_int(row.get("margin_balance")),
        "margin_quota":            _to_int(row.get("margin_quota")),
        "sbl_prev_balance":        _to_int(row.get("sbl_prev_balance")),
        "sbl_short_sales":         _to_int(row.get("sbl_short_sales")),
        "sbl_short_covering":      _to_int(row.get("sbl_short_covering")),
        "sbl_returns":             _to_int(row.get("sbl_returns")),
        "sbl_adjustments":         _to_int(row.get("sbl_adjustments")),
        "sbl_balance":             _to_int(row.get("sbl_balance")),
        "sbl_quota":               _to_int(row.get("sbl_quota")),
        "source": row.get("source", "finmind"),
    }
