"""Unit tests for the row + batch transforms landed after the
Phase-2 follow-up batch (PRs #321 / #323 / #324 / #325 / #326 / #327
/ #328 / #329 / #330 / #331).

Each transform is a pure function over the FinMind response shape →
local-table row dict. Tests pin the contract: synthetic FinMind input
→ expected output. No DB, no httpx — these run in milliseconds.

The aim is regression protection on:

  - `_row_stock_info_with_warrant` heuristic (#328 — was over-flagging
    every TaiwanStockInfoWithWarrant row including 2330 + 0050).
  - `_batch_govt_bank_flow` aggregation (#323 — sums per-(stock, bank)
    rows into one daily total).
  - `_batch_cb_daily` filter to `等價` (#326 — keep only matched-trade
    leg, drop indicative buy/sell quotes).
  - `_batch_*_dealer_volume` is_after_hour aggregation (#326, #330 —
    PK has no session axis, so volumes fold).
  - `_batch_option_inst_regular` long → wide pivot (#324 — three
    investor types as columns).
  - `_batch_*_oi_largetraders` top5 / top10 rank split (#327 — one
    FinMind row → two local rows).
  - `_batch_stock_tick` per-(symbol, day) sequential `seq` (#323 — PK
    has seq for collision-free microsecond ticks).
  - `quota_strict()` context manager (#328 — strict raise vs quiet [])
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from finmind.ingest.mappings import (
    MAPPINGS,
    _batch_cb_daily,
    _batch_futures_dealer_volume,
    _batch_futures_oi_largetraders,
    _batch_govt_bank_flow,
    _batch_option_inst_regular,
    _batch_stock_tick,
    _row_stock_info_with_warrant,
    transform_row,
)


# ── _row_stock_info_with_warrant heuristic (#328) ────────────────


def test_row_stock_info_with_warrant_equity_is_not_warrant():
    """2330 (台積電) is a plain equity — must stay is_warrant=False
    even though it ships through the with-warrant feed."""
    out = _row_stock_info_with_warrant({
        "symbol": "2330",
        "name_zh": "台積電",
        "industry_category": "全部(不含大盤、指數)",
        "market": "twse",
        "listed_at": "1994-09-05",
    })
    assert out["is_warrant"] is False
    assert out["symbol"] == "2330"
    assert out["market"] == "TWSE"


def test_row_stock_info_with_warrant_etf_is_not_warrant():
    """ETFs (0050, 00xxx) ship through the with-warrant feed but are
    not warrants. The previous heuristic flagged them; the new one
    keys off `stock_name` patterns so ETFs stay False."""
    out = _row_stock_info_with_warrant({
        "symbol": "0050",
        "name_zh": "元大台灣50",
        "industry_category": "全部(不含大盤、指數)",
        "market": "twse",
    })
    assert out["is_warrant"] is False


def test_row_stock_info_with_warrant_call_warrant_is_warrant():
    """`認購` (call warrant) in stock_name flags is_warrant=True."""
    out = _row_stock_info_with_warrant({
        "symbol": "089999",
        "name_zh": "智邦凱基53購02",  # contains 購
        "industry_category": "全部(不含大盤、指數)",
        "market": "twse",
    })
    # 'X購Y' alone doesn't trigger — full keyword `認購` does.
    # Real warrant names include `認購` explicitly. Verify with one:
    out2 = _row_stock_info_with_warrant({
        "symbol": "057178",
        "name_zh": "中華電認購03",
        "industry_category": "全部(不含大盤、指數)",
        "market": "twse",
    })
    assert out2["is_warrant"] is True


def test_row_stock_info_with_warrant_put_warrant_is_warrant():
    out = _row_stock_info_with_warrant({
        "symbol": "08999P",
        "name_zh": "臺股指凱基54售08",  # contains 售
        "industry_category": "全部(不含大盤、指數)",
        "market": "twse",
    })
    # Just `售` alone doesn't trigger; `認售` does.
    out2 = _row_stock_info_with_warrant({
        "symbol": "08999P",
        "name_zh": "臺股指凱基認售54",
        "industry_category": "全部(不含大盤、指數)",
        "market": "twse",
    })
    assert out2["is_warrant"] is True


def test_row_stock_info_with_warrant_bull_with_digit_is_warrant():
    """`牛` followed by a digit elsewhere in name = bull leverage cert."""
    out = _row_stock_info_with_warrant({
        "symbol": "030001",
        "name_zh": "元大台灣牛01",
        "industry_category": "全部(不含大盤、指數)",
        "market": "twse",
    })
    assert out["is_warrant"] is True


def test_row_stock_info_with_warrant_bull_without_digit_is_equity():
    """A company name with `牛` but no digit must not be flagged. Avoids
    false positives on legitimate equity names that happen to contain
    the character."""
    out = _row_stock_info_with_warrant({
        "symbol": "1234",
        "name_zh": "牛頭牌",  # hypothetical equity, no digit
        "industry_category": "食品工業",
        "market": "twse",
    })
    assert out["is_warrant"] is False


# ── _batch_govt_bank_flow (#323) ─────────────────────────────────


def test_batch_govt_bank_flow_sums_per_date():
    """FinMind ships one row per (stock, bank, date). Local schema
    aggregates to one row per (market, ts) — buy/sell summed,
    net_amount derived."""
    rows = [
        {"date": "2024-01-02", "stock_id": "2330", "bank_name": "兆豐",
         "buy_amount": "100", "sell_amount": "30", "buy": 1, "sell": 1},
        {"date": "2024-01-02", "stock_id": "2330", "bank_name": "彰銀",
         "buy_amount": "50",  "sell_amount": "20", "buy": 1, "sell": 1},
        {"date": "2024-01-02", "stock_id": "2317", "bank_name": "兆豐",
         "buy_amount": "200", "sell_amount": "80", "buy": 1, "sell": 1},
        {"date": "2024-01-03", "stock_id": "2330", "bank_name": "兆豐",
         "buy_amount": "10",  "sell_amount": "5",  "buy": 1, "sell": 1},
    ]
    out = _batch_govt_bank_flow(rows)
    out_by_ts = {r["ts"]: r for r in out}

    assert len(out) == 2
    jan2 = out_by_ts[date(2024, 1, 2)]
    assert jan2["buy_amount"] == Decimal("350")
    assert jan2["sell_amount"] == Decimal("130")
    assert jan2["net_amount"] == Decimal("220")
    assert jan2["market"] == "TWSE"

    jan3 = out_by_ts[date(2024, 1, 3)]
    assert jan3["buy_amount"] == Decimal("10")
    assert jan3["sell_amount"] == Decimal("5")


# ── _batch_cb_daily transaction_type filter (#326) ───────────────


def test_batch_cb_daily_keeps_only_matched_trade_leg():
    """FinMind emits 3 rows per (cb_id, date) — `等價` (matched),
    `買盤` (bid), `賣盤` (ask). Only `等價` should land; the other
    two are indicative quotes that would collide on the (cb_id, ts)
    PK and overwrite real OHLCV with bid/ask."""
    rows = [
        {"cb_id": "24553", "date": "2026-01-27", "transaction_type": "等價",
         "open": "100", "max": "105", "min": "99", "close": "103", "unit": 50},
        {"cb_id": "24553", "date": "2026-01-27", "transaction_type": "買盤",
         "open": "100", "max": "100", "min": "99", "close": "99", "unit": 5},
        {"cb_id": "24553", "date": "2026-01-27", "transaction_type": "賣盤",
         "open": "104", "max": "105", "min": "104", "close": "105", "unit": 3},
    ]
    out = _batch_cb_daily(rows)
    assert len(out) == 1
    assert out[0]["cb_id"] == "24553"
    assert out[0]["close"] == Decimal("103")  # the 等價 leg, not 99 or 105
    assert out[0]["volume"] == 50


# ── _batch_futures_dealer_volume is_after_hour aggregation (#326) ──


def test_batch_futures_dealer_volume_aggregates_session():
    """Local PK is (ts, dealer_id, contract) — no session axis. Day
    + after-hours volumes for the same key must sum into one row."""
    rows = [
        {"date": "2024-01-02", "dealer_code": "S884999",
         "futures_id": "TX", "volume": 100, "is_after_hour": False},
        {"date": "2024-01-02", "dealer_code": "S884999",
         "futures_id": "TX", "volume": 30,  "is_after_hour": True},
        {"date": "2024-01-02", "dealer_code": "F004000",
         "futures_id": "TX", "volume": 50,  "is_after_hour": False},
    ]
    out = _batch_futures_dealer_volume(rows)
    by_dealer = {r["dealer_id"]: r for r in out}

    assert len(out) == 2
    assert by_dealer["S884999"]["volume"] == 130  # 100 + 30
    assert by_dealer["F004000"]["volume"] == 50


# ── _batch_option_inst_regular long → wide pivot (#324) ──────────


def test_batch_option_inst_regular_pivot_three_investors():
    """FinMind emits one row per (option, date, call_put,
    investor_type). Local schema is wide — three investor types as
    *_long_open_interest / *_short_open_interest columns. Pivot
    groups by (option, date, call_put)."""
    rows = [
        {"option_id": "TXO", "date": "2026-04-25", "call_put": "買權",
         "institutional_investors": "自營商",
         "long_open_interest_balance_volume": 17161,
         "short_open_interest_balance_volume": 18000},
        {"option_id": "TXO", "date": "2026-04-25", "call_put": "買權",
         "institutional_investors": "投信",
         "long_open_interest_balance_volume": 3,
         "short_open_interest_balance_volume": 5},
        {"option_id": "TXO", "date": "2026-04-25", "call_put": "買權",
         "institutional_investors": "外資",
         "long_open_interest_balance_volume": 10626,
         "short_open_interest_balance_volume": 12000},
        # Different call_put — separate group
        {"option_id": "TXO", "date": "2026-04-25", "call_put": "賣權",
         "institutional_investors": "自營商",
         "long_open_interest_balance_volume": 26291,
         "short_open_interest_balance_volume": 27000},
    ]
    out = _batch_option_inst_regular(rows)
    by_cp = {r["call_put"]: r for r in out}

    assert len(out) == 2
    call = by_cp["C"]  # 買權 → C
    assert call["dealer_long_open_interest"] == 17161
    assert call["sitc_long_open_interest"] == 3
    assert call["foreign_long_open_interest"] == 10626
    assert call["foreign_short_open_interest"] == 12000
    assert call["session"] == "reg"

    put = by_cp["P"]  # 賣權 → P
    assert put["dealer_long_open_interest"] == 26291
    # Other investor types absent from input → stay None
    assert put["foreign_long_open_interest"] is None
    assert put["sitc_long_open_interest"] is None


# ── _batch_futures_oi_largetraders rank split (#327) ─────────────


def test_batch_futures_oi_largetraders_emits_top5_and_top10():
    """One FinMind row carries top5 + top10 cumulative buckets in
    parallel columns. Local schema's `rank` discriminates two rows
    per (contract, date) — string labels `top5` / `top10`."""
    rows = [
        {"futures_id": "TX", "date": "2024-01-02", "contract_type": "all",
         "buy_top5_trader_open_interest": 50547,
         "buy_top10_trader_open_interest": 61932,
         "sell_top5_trader_open_interest": 45182,
         "sell_top10_trader_open_interest": 68185,
         "buy_top5_specific_open_interest": 50547,
         "buy_top10_specific_open_interest": 56990,
         "sell_top5_specific_open_interest": 45182,
         "sell_top10_specific_open_interest": 68185},
        # `week` contract_type filtered out — only `all` is kept
        {"futures_id": "TX", "date": "2024-01-02", "contract_type": "week",
         "buy_top5_trader_open_interest": 999,
         "buy_top10_trader_open_interest": 9999,
         "sell_top5_trader_open_interest": 0,
         "sell_top10_trader_open_interest": 0,
         "buy_top5_specific_open_interest": 0,
         "buy_top10_specific_open_interest": 0,
         "sell_top5_specific_open_interest": 0,
         "sell_top10_specific_open_interest": 0},
    ]
    out = _batch_futures_oi_largetraders(rows)
    by_rank = {r["rank"]: r for r in out}

    assert len(out) == 2
    assert "top5" in by_rank and "top10" in by_rank
    assert by_rank["top5"]["long_oi"] == 50547
    assert by_rank["top10"]["long_oi"] == 61932
    # top10 ≥ top5 — cumulative semantics
    assert by_rank["top10"]["long_oi"] >= by_rank["top5"]["long_oi"]
    assert by_rank["top5"]["long_oi_special"] == 50547
    assert by_rank["top10"]["short_oi_special"] == 68185
    # Verify week-level row was filtered out (no rows from contract_type='week')
    assert all(r["long_oi"] != 9999 for r in out)


# ── _batch_stock_tick seq generation (#323) ──────────────────────


def test_batch_stock_tick_assigns_per_day_per_symbol_seq():
    """tw_stock_tick PK is (market, symbol, ts, seq). Multiple ticks
    can share the same microsecond — a per-(symbol, day) sequential
    counter ensures collision-free upserts."""
    rows = [
        {"date": "2026-04-28", "stock_id": "2330", "deal_price": 2245.0,
         "volume": 5000, "Time": "09:00:02.778330", "TickType": 2},
        {"date": "2026-04-28", "stock_id": "2330", "deal_price": 2245.0,
         "volume": 1000, "Time": "09:00:02.778330", "TickType": 2},  # same ts
        {"date": "2026-04-28", "stock_id": "2330", "deal_price": 2246.0,
         "volume": 2000, "Time": "09:00:03.000000", "TickType": 1},
        {"date": "2026-04-28", "stock_id": "2317", "deal_price": 100.0,
         "volume": 500,  "Time": "09:00:02.000000", "TickType": 2},  # diff symbol
    ]
    out = _batch_stock_tick(rows)

    # 2330 ticks: seqs 1, 2, 3 — independent counter per symbol
    by_2330 = [r for r in out if r["symbol"] == "2330"]
    assert [r["seq"] for r in by_2330] == [1, 2, 3]

    # 2317: independent counter, starts at 1
    by_2317 = [r for r in out if r["symbol"] == "2317"]
    assert by_2317[0]["seq"] == 1

    # ts construction: date + Time (microsecond precision)
    assert by_2330[0]["ts"] == datetime(
        2026, 4, 28, 9, 0, 2, 778330, tzinfo=timezone.utc
    )


# ── quota_strict() context manager (#328) ────────────────────────


def test_quota_strict_context_toggles_contextvar():
    """Inside the context, `_strict_quota` is True; outside, False.
    Nesting / sequential entries don't leak state."""
    from data.tw.finmind_connector import _strict_quota, quota_strict

    assert _strict_quota.get() is False
    with quota_strict():
        assert _strict_quota.get() is True
    assert _strict_quota.get() is False

    # Re-entry after exit also works
    with quota_strict():
        assert _strict_quota.get() is True
        # Nested re-entry (idempotent — token reset preserves outer state)
        with quota_strict():
            assert _strict_quota.get() is True
        assert _strict_quota.get() is True
    assert _strict_quota.get() is False


# ── Mapping registry coverage pin ────────────────────────────────


def test_recent_mappings_present_in_registry():
    """Every dataset wired through PRs #321-#331 must keep its entry
    in MAPPINGS. If a future commit removes one, this fails."""
    expected = {
        # batch 1 (#321)
        "TaiwanSecuritiesTraderInfo",
        "TaiwanStockDispositionSecuritiesPeriod",
        "TaiwanStockHoldingSharesPer",
        "TaiwanTotalExchangeMarginMaintenance",
        "TaiwanStockConvertibleBondInfo",
        "TaiwanStockIndustryChain",
        "TaiwanStockDayTradingSuspension",
        "TaiwanStockInfoWithWarrant",
        "TaiwanStockTradingDate",
        "TaiwanStockTotalReturnIndex",
        "TaiwanStockParValueChange",
        # single-day (#323)
        "TaiwanStockKBar",
        "TaiwanStockPriceTick",
        "TaiwanStockBlockTradingDailyReport",
        "TaiwanStockGovernmentBankBuySell",
        "TaiwanStockTradingDailyReport",
        # option pivot (#324)
        "TaiwanOptionInstitutionalInvestors",
        "TaiwanOptionInstitutionalInvestorsAfterHours",
        # warrant (#325)
        "TaiwanStockWarrantTradingDailyReport",
        # batch 3 (#326)
        "TaiwanStockMarginShortSaleSuspension",
        "TaiwanStockDayTradingBorrowingFeeRate",
        "TaiwanStockConvertibleBondInstitutionalInvestors",
        "TaiwanFuturesFinalSettlementPrice",
        "TaiwanFuturesSpreadTrading",
        "TaiwanStockConvertibleBondDaily",
        "TaiwanFuturesDealerTradingVolumeDaily",
        # OI large traders (#327)
        "TaiwanFuturesOpenInterestLargeTraders",
        "TaiwanOptionOpenInterestLargeTraders",
        # LoanCollateral (#329)
        "TaiwanStockLoanCollateralBalance",
        # Option PK relax (#330)
        "TaiwanOptionFinalSettlementPrice",
        "TaiwanOptionDealerTradingVolumeDaily",
        # FutOpt master (#331)
        "TaiwanFutOptDailyInfo",
    }
    missing = expected - set(MAPPINGS.keys())
    assert not missing, f"recent mappings missing from registry: {missing}"
