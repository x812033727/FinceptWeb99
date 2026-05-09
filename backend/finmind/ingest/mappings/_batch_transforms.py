"""Whole-chunk FinMind → local-table transforms.

For datasets that don't fit the row-by-row shape:

  - Wide-format pivots (quarterly statements, market-wide totals) —
    GROUP BY (entity, period) and pivot the `type`/`name` field onto
    canonical columns.
  - Intraday tick / block-trade datasets that need cross-row
    deduplication or aggregation before insert.
  - Option/futures dealer-volume datasets where one local row spans
    multiple investor-type FinMind rows.

Each `_batch_*` / `_pivot_*` function takes the FinMind payload
(list[dict]) and returns local-table rows directly — `column_map` and
`row_transform` from the DatasetMapping do NOT apply on this path
because the pivot logic owns column resolution itself."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from ._types import (
    _to_date,
    _to_decimal,
    _to_int,
    _to_str,
)




def _batch_cb_daily(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """ConvertibleBondDaily emits multiple rows per (cb_id, date) — one
    per `transaction_type` ('等價' / '買盤' / '賣盤'). The local
    `tw_cb_daily` schema is one OHLCV row per (cb_id, date); we keep
    only the regular `等價` (matched-trade) leg so the PK is
    collision-free and the OHLCV reflects actual market prints rather
    than indicative bid/ask quotes from the buy/sell legs."""
    out: list[dict[str, Any]] = []
    for r in rows:
        if str(r.get("transaction_type") or "").strip() != "等價":
            continue
        out.append({
            "cb_id": _to_str(r.get("cb_id")),
            "ts": _to_date(r.get("date")),
            "open": _to_decimal(r.get("open")),
            "high": _to_decimal(r.get("max")),
            "low": _to_decimal(r.get("min")),
            "close": _to_decimal(r.get("close")),
            # FinMind ships unit count (張); local schema's `volume` is
            # the same convention. trading_value would be the notional
            # in TWD but the schema doesn't have a column for it.
            "volume": _to_int(r.get("unit")),
            "source": "finmind",
        })
    return out



def _batch_option_dealer_volume(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Mirror of `_batch_futures_dealer_volume` for options. FinMind
    emits one row per (date, dealer, option, session); the local PK
    `(ts, dealer_id, contract)` has no session axis (see migration
    0016), so we sum the regular + after-hours volumes for each
    (dealer, contract) into a single row. is_after_hour breakout is
    lost — operators wanting it can re-fetch and split client-side."""
    agg: dict[tuple[date | None, str, str], int] = {}
    for r in rows:
        key = (
            _to_date(r.get("date")),
            str(r.get("dealer_code") or ""),
            str(r.get("option_id") or ""),
        )
        v = _to_int(r.get("volume")) or 0
        agg[key] = agg.get(key, 0) + v
    return [
        {
            "ts": ts,
            "dealer_id": dealer or None,
            "contract": contract or None,
            "volume": vol,
            "source": "finmind",
        }
        for (ts, dealer, contract), vol in agg.items()
    ]



def _batch_futures_dealer_volume(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each FinMind row is one (date, dealer, contract, session). Local
    schema PK is (ts, dealer_id, contract) — no session axis — so we
    aggregate day + after-hours volumes for the same (dealer, contract)
    into one local row. is_after_hour split is lost; operators wanting
    the breakdown can re-fetch and split client-side."""
    agg: dict[tuple[date | None, str, str], int] = {}
    for r in rows:
        key = (
            _to_date(r.get("date")),
            str(r.get("dealer_code") or ""),
            str(r.get("futures_id") or ""),
        )
        v = _to_int(r.get("volume")) or 0
        agg[key] = agg.get(key, 0) + v
    return [
        {
            "ts": ts,
            "dealer_id": dealer or None,
            "contract": contract or None,
            "volume": vol,
            "source": "finmind",
        }
        for (ts, dealer, contract), vol in agg.items()
    ]



def _batch_futures_oi_largetraders(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """OpenInterestLargeTraders ships one row per (futures_id, date,
    contract_type). `contract_type` enumerates `'all'` (aggregate
    across all contract months), `'week'` (weekly contract), and
    individual contract codes like `'202401'` / `'202402'`. The local
    PK `(contract, ts, rank)` has no contract_type axis, so we keep
    only the `'all'` aggregate — the most useful single-signal view
    for trader-concentration analytics.

    From each `'all'` row we emit two local rows: rank='top5' and
    rank='top10' (FinMind ships top5 / top10 cumulative buckets in
    the same row; the `rank` column is varchar so the labels survive
    round-trips). `*_specific_*` fields map to `*_oi_special` columns;
    `*_trader_*` fields map to `*_oi`."""
    out: list[dict[str, Any]] = []
    for r in rows:
        if str(r.get("contract_type") or "") != "all":
            continue
        contract = _to_str(r.get("futures_id"))
        ts = _to_date(r.get("date"))
        for prefix in ("top5", "top10"):
            out.append({
                "contract": contract,
                "ts": ts,
                "rank": prefix,
                "long_oi": _to_int(r.get(f"buy_{prefix}_trader_open_interest")),
                "short_oi": _to_int(r.get(f"sell_{prefix}_trader_open_interest")),
                "long_oi_special": _to_int(
                    r.get(f"buy_{prefix}_specific_open_interest")
                ),
                "short_oi_special": _to_int(
                    r.get(f"sell_{prefix}_specific_open_interest")
                ),
                "source": "finmind",
            })
    return out



def _batch_option_oi_largetraders(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Same shape as the futures version, plus `put_call` axis from
    FinMind ('call'/'put') folded into the local `call_put` column
    (varchar(4)) as 'C'/'P'. Local PK is (contract, ts, call_put,
    rank). Filter to `contract_type='all'` for the same reason —
    individual contract months would multiply rows beyond what the
    PK can express without losing data on the (call, week) axis."""
    out: list[dict[str, Any]] = []
    for r in rows:
        if str(r.get("contract_type") or "") != "all":
            continue
        contract = _to_str(r.get("option_id"))
        ts = _to_date(r.get("date"))
        cp_raw = str(r.get("put_call") or "").lower()
        cp = "C" if cp_raw == "call" else "P" if cp_raw == "put" else cp_raw[:4]
        for prefix in ("top5", "top10"):
            out.append({
                "contract": contract,
                "ts": ts,
                "call_put": cp,
                "rank": prefix,
                "long_oi": _to_int(r.get(f"buy_{prefix}_trader_open_interest")),
                "short_oi": _to_int(r.get(f"sell_{prefix}_trader_open_interest")),
                "source": "finmind",
            })
    return out



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



def _batch_block_trade_full(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """TaiwanStockBlockTrade payload → tw_block_trade. Each FinMind
    row is one block-trade execution; multiple per (symbol, date) get
    a sequential `seq` starting at 1 to satisfy the table's PK
    `(market, symbol, ts, seq)`. Differs from `_batch_block_trade` (used
    by TaiwanStockBlockTradingDailyReport): that endpoint splits each
    trade into buy/sell broker-side rows, this one ships the trade
    economics directly with `volume` and `trading_money`."""
    out: list[dict[str, Any]] = []
    counters: dict[tuple[str, str], int] = {}
    for r in rows:
        sym = str(r.get("stock_id") or "")
        d = _to_date(r.get("date"))
        key = (sym, str(d) if d else "")
        counters[key] = counters.get(key, 0) + 1
        out.append({
            "market": "TWSE",
            "symbol": sym or None,
            "ts": d,
            "seq": counters[key],
            "price": _to_decimal(r.get("price")),
            "volume": _to_int(r.get("volume")),
            "amount": _to_decimal(r.get("trading_money")),
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



_OPTION_INVESTOR_MAP = {
    "自營商": "dealer",
    "投信": "sitc",
    "外資": "foreign",
    "外資及陸資": "foreign",  # alt form sometimes seen on TWSE Stock variants
}

_OPTION_CALLPUT_MAP = {
    "買權": "C",
    "賣權": "P",
    "C": "C",
    "P": "P",
}



def _make_batch_option_inst(session_label: str):
    """Returns a batch_transform that pivots FinMind's long-form option
    institutional rows (one per (option, date, call_put, investor_type))
    into local wide-form rows (one per (contract, ts, session, call_put)
    with foreign/sitc/dealer × long/short_open_interest as columns).

    `session_label` distinguishes regular session from after-hours so
    both feeds can target the same `tw_option_inst_daily` table without
    collisions on PK (contract, ts, session, call_put). FinMind doesn't
    ship `deal_volume`/`deal_amount` separately for the local schema —
    only the open-interest balance columns survive the pivot."""

    def _pivot(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        groups: dict[tuple[str, date | None, str], dict[str, Any]] = {}
        for r in rows:
            contract = _to_str(r.get("option_id"))
            ts = _to_date(r.get("date"))
            cp_raw = str(r.get("call_put") or "")
            cp = _OPTION_CALLPUT_MAP.get(cp_raw, cp_raw[:4])
            key = (contract or "", ts, cp)
            if key not in groups:
                groups[key] = {
                    "contract": contract,
                    "ts": ts,
                    "session": session_label,
                    "call_put": cp,
                    "foreign_long_open_interest": None,
                    "foreign_short_open_interest": None,
                    "sitc_long_open_interest": None,
                    "sitc_short_open_interest": None,
                    "dealer_long_open_interest": None,
                    "dealer_short_open_interest": None,
                    "source": "finmind",
                }
            inv_raw = str(r.get("institutional_investors") or "")
            prefix = _OPTION_INVESTOR_MAP.get(inv_raw)
            if prefix is None:
                # Unknown investor type — skip rather than dropping the
                # whole pivot row, so a new FinMind investor label
                # surfaces as missing data rather than corrupted rows.
                continue
            groups[key][f"{prefix}_long_open_interest"] = _to_int(
                r.get("long_open_interest_balance_volume")
            )
            groups[key][f"{prefix}_short_open_interest"] = _to_int(
                r.get("short_open_interest_balance_volume")
            )
        return list(groups.values())

    return _pivot



_batch_option_inst_regular = _make_batch_option_inst("reg")

_batch_option_inst_afterhours = _make_batch_option_inst("AH")



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
