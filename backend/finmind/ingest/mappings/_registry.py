"""MAPPINGS dict + the `find_mapping` + `transform_row` lookup helpers.

The registry maps every dataset_code that has an ingest mapping to its
DatasetMapping descriptor. Adding a new dataset is one entry here +
(optionally) a custom row_transform / batch_transform in the sibling
`_row_transforms` / `_batch_transforms` modules.

Datasets without a mapping fall through with a `MappingNotFoundError`
that the runner records as `skipped` — Phase 1 schema work continues
independently of which datasets the runner actually knows how to ingest.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from ._batch_transforms import (
    _batch_block_trade,
    _batch_block_trade_full,
    _batch_cb_daily,
    _batch_futures_dealer_volume,
    _batch_futures_oi_largetraders,
    _batch_govt_bank_flow,
    _batch_option_dealer_volume,
    _batch_option_inst_afterhours,
    _batch_option_inst_regular,
    _batch_option_oi_largetraders,
    _batch_stock_tick,
    _pivot_balance_sheet,
    _pivot_cash_flow,
    _pivot_income_statement,
    _pivot_total_institutional,
)
from ._row_transforms import (
    _LOAN_COLLATERAL_FIELDS,
    _row_broker_daily_report,
    _row_broker_master,
    _row_business_indicator,
    _row_buyback,
    _row_cb_info,
    _row_cb_inst_daily,
    _row_crypto_asset_info,
    _row_crypto_funding_rate,
    _row_crypto_ohlcv,
    _row_crypto_open_interest,
    _row_day_trade,
    _row_day_trade_fee,
    _row_delisting,
    _row_disposition,
    _row_dividend,
    _row_dividend_result,
    _row_futures_daily,
    _row_futures_inst,
    _row_futures_settlement,
    _row_futures_spread,
    _row_holdings_aggregates,
    _row_industry_chain,
    _row_institutional,
    _row_loan_collateral,
    _row_margin,
    _row_margin_maintenance,
    _row_market_value,
    _row_market_value_weight,
    _row_news,
    _row_ohlcv,
    _row_option_daily,
    _row_option_settlement,
    _row_par_value_change,
    _row_per,
    _row_price_adj,
    _row_price_limit,
    _row_revenue,
    _row_securities_lending,
    _row_shareholding,
    _row_short_sale_balance,
    _row_short_sale_suspension,
    _row_split,
    _row_stock_info,
    _row_stock_info_with_warrant,
    _row_stock_minute,
    _row_suspended,
    _row_total_margin,
    _row_total_return_index,
    _row_trading_calendar,
)
from ._types import (
    CompareSpec,
    DatasetMapping,
    MappingNotFoundError,
    _to_date,
    _to_decimal,
    _to_int,
    _to_str,
)




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
        compare_spec=CompareSpec(
            key_cols=("date", "stock_id"),
            value_cols=(
                ("open", "rel", 0.005),
                ("max", "rel", 0.005),
                ("min", "rel", 0.005),
                ("close", "rel", 0.005),
                # Volume can differ by odd-lot inclusion between TWSE
                # STOCK_DAY and FinMind — allow 1% relative slack.
                ("Trading_Volume", "rel", 0.01),
            ),
        ),
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
        compare_spec=CompareSpec(
            key_cols=("date", "stock_id"),
            value_cols=(
                # Balances are exact share counts — no rounding slack.
                ("MarginPurchaseTodayBalance", "abs", 0.0),
                ("ShortSaleTodayBalance", "abs", 0.0),
            ),
        ),
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
        compare_spec=CompareSpec(
            key_cols=("date", "stock_id"),
            value_cols=(
                ("PER", "rel", 0.01),
                ("PBR", "rel", 0.01),
                ("dividend_yield", "rel", 0.01),
            ),
        ),
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
    "TaiwanStockCapitalReductionReferencePrice": DatasetMapping(
        dataset_code="TaiwanStockCapitalReductionReferencePrice",
        local_table="tw_capital_reduction",
        column_map={
            "date": "ex_date",
            "stock_id": "symbol",
            "ClosingPriceonTheLastTradingDay": "before_price",
            "PostReductionReferencePrice": "after_price",
        },
        pk_columns=("symbol", "ex_date"),
        extra={"source": "finmind"},
        row_transform=lambda r: {
            "symbol": _to_str(r.get("symbol")),
            "ex_date": _to_date(r.get("ex_date")),
            "before_price": _to_decimal(r.get("before_price")),
            "after_price": _to_decimal(r.get("after_price")),
            # FinMind doesn't ship a reduction_pct column directly;
            # derive it from before/after prices when both present
            # ((before - after) / before, capped to 4 decimal places).
            "reduction_pct": (
                (Decimal(str(r.get("before_price"))) - Decimal(str(r.get("after_price"))))
                / Decimal(str(r.get("before_price")))
                if (r.get("before_price") not in (None, "", 0, 0.0)
                    and r.get("after_price") not in (None, ""))
                else None
            ),
            "source": r.get("source", "finmind"),
        },
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
        # FinMind returns 400 "size is too large, end_date parameter
        # need be none" for any multi-day request. Force per-day fan-out
        # so direct-CLI backfills don't need to chunk the range manually.
        single_day=True,
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
    "TaiwanStockConvertibleBondDailyOverview": DatasetMapping(
        dataset_code="TaiwanStockConvertibleBondDailyOverview",
        local_table="tw_cb_daily_overview",
        column_map={
            "cb_id": "cb_id",
            "date": "ts",
            "OutstandingAmount": "outstanding_amount",
        },
        pk_columns=("cb_id", "ts"),
        extra={"source": "finmind"},
        row_transform=lambda r: {
            "cb_id": _to_str(r.get("cb_id")),
            "ts": _to_date(r.get("ts")),
            "outstanding_amount": _to_decimal(r.get("outstanding_amount")),
            "source": r.get("source", "finmind"),
        },
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
    "TaiwanStockBlockTrade": DatasetMapping(
        dataset_code="TaiwanStockBlockTrade",
        local_table="tw_block_trade",
        column_map={},
        pk_columns=("market", "symbol", "ts", "seq"),
        batch_transform=_batch_block_trade_full,
        # FinMind silently truncates a multi-day request to just the
        # start_date when no `data_id` is supplied (verified 2026-05-06:
        # 2025-12-01..2025-12-31 returned only 2025-12-01's 17 rows;
        # data_id=2330 over the same range returned 156 rows across all
        # 21 trading days). Force per-day fan-out so we get every day.
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
    "TaiwanOptionInstitutionalInvestors": DatasetMapping(
        dataset_code="TaiwanOptionInstitutionalInvestors",
        local_table="tw_option_inst_daily",
        column_map={},  # batch_transform owns the pivot
        pk_columns=("contract", "ts", "session", "call_put"),
        batch_transform=_batch_option_inst_regular,
    ),
    "TaiwanOptionInstitutionalInvestorsAfterHours": DatasetMapping(
        dataset_code="TaiwanOptionInstitutionalInvestorsAfterHours",
        local_table="tw_option_inst_daily",
        column_map={},
        pk_columns=("contract", "ts", "session", "call_put"),
        batch_transform=_batch_option_inst_afterhours,
    ),
    # Same shape as TaiwanStockTradingDailyReport, but indexed by a
    # warrant code instead of an equity stock_id. The runner picks the
    # warrant universe via `dispatcher._WARRANT_UNIVERSE_DATASETS`, so
    # a `--warrant-universe-from-tw-stock-info` invocation reaches it
    # without bleeding the warrant fan-out into equity per-symbol
    # datasets.
    "TaiwanStockWarrantTradingDailyReport": DatasetMapping(
        dataset_code="TaiwanStockWarrantTradingDailyReport",
        local_table="tw_broker_daily_report",
        column_map={
            "stock_id": "symbol",
            "date": "ts",
            "securities_trader_id": "broker_id",
            "price": "price",
            "buy": "buy_volume",
            "sell": "sell_volume",
        },
        pk_columns=("market", "symbol", "ts", "broker_id", "price"),
        extra={"market": "TWSE", "source": "finmind"},
        row_transform=_row_broker_daily_report,
        single_day=True,
    ),
    "TaiwanStockMarginShortSaleSuspension": DatasetMapping(
        dataset_code="TaiwanStockMarginShortSaleSuspension",
        local_table="tw_short_sale_suspension",
        column_map={
            "stock_id": "symbol",
            "date": "suspended_at",
            "end_date": "resumed_at",
            "reason": "reason",
        },
        pk_columns=("symbol", "suspended_at"),
        extra={"source": "finmind"},
        row_transform=_row_short_sale_suspension,
    ),
    "TaiwanStockLoanCollateralBalance": DatasetMapping(
        dataset_code="TaiwanStockLoanCollateralBalance",
        local_table="tw_loan_collateral",
        column_map={
            "date": "ts",
            "stock_id": "symbol",
            "market": "market",
            **{fm: lc for fm, lc in _LOAN_COLLATERAL_FIELDS},
        },
        pk_columns=("market", "symbol", "ts"),
        extra={"source": "finmind"},
        row_transform=_row_loan_collateral,
    ),
    "TaiwanStockDayTradingBorrowingFeeRate": DatasetMapping(
        dataset_code="TaiwanStockDayTradingBorrowingFeeRate",
        local_table="tw_day_trade_fee",
        column_map={
            "date": "ts",
            "stock_id": "symbol",
            "InvestorBorrowingFeeRate": "fee_rate",
        },
        pk_columns=("market", "symbol", "ts"),
        extra={"market": "TWSE", "source": "finmind"},
        row_transform=_row_day_trade_fee,
    ),
    "TaiwanStockConvertibleBondInstitutionalInvestors": DatasetMapping(
        dataset_code="TaiwanStockConvertibleBondInstitutionalInvestors",
        local_table="tw_cb_inst_daily",
        column_map={
            "cb_id": "cb_id",
            "date": "ts",
            "Foreign_Investor_Buy": "foreign_buy",
            "Foreign_Investor_Sell": "foreign_sell",
            "Investment_Trust_Buy": "sitc_buy",
            "Investment_Trust_Sell": "sitc_sell",
            "Dealer_self_Buy": "dealer_buy",
            "Dealer_self_Sell": "dealer_sell",
        },
        pk_columns=("cb_id", "ts"),
        extra={"source": "finmind"},
        row_transform=_row_cb_inst_daily,
    ),
    "TaiwanFuturesFinalSettlementPrice": DatasetMapping(
        dataset_code="TaiwanFuturesFinalSettlementPrice",
        local_table="tw_futures_settlement",
        column_map={
            "futures_id": "contract",
            "date": "settlement_date",
            "settlement_price": "final_settlement_price",
        },
        pk_columns=("contract", "settlement_date"),
        extra={"source": "finmind"},
        row_transform=_row_futures_settlement,
    ),
    "TaiwanFuturesSpreadTrading": DatasetMapping(
        dataset_code="TaiwanFuturesSpreadTrading",
        local_table="tw_futures_spread",
        column_map={
            "date": "ts",
            "contract_date": "spread_pair",
            "open": "open",
            "close": "close",
            "spread_to_spread_volume": "volume",
        },
        pk_columns=("ts", "spread_pair"),
        extra={"source": "finmind"},
        row_transform=_row_futures_spread,
    ),
    "TaiwanStockConvertibleBondDaily": DatasetMapping(
        dataset_code="TaiwanStockConvertibleBondDaily",
        local_table="tw_cb_daily",
        column_map={},  # batch_transform owns column resolution
        pk_columns=("cb_id", "ts"),
        batch_transform=_batch_cb_daily,
    ),
    "TaiwanFuturesDealerTradingVolumeDaily": DatasetMapping(
        dataset_code="TaiwanFuturesDealerTradingVolumeDaily",
        local_table="tw_futures_dealer_volume",
        column_map={},
        pk_columns=("ts", "dealer_id", "contract"),
        batch_transform=_batch_futures_dealer_volume,
    ),
    "TaiwanOptionFinalSettlementPrice": DatasetMapping(
        dataset_code="TaiwanOptionFinalSettlementPrice",
        local_table="tw_option_settlement",
        column_map={
            "option_id": "contract",
            "contract_month": "contract_month",
            "date": "settlement_date",
            "settlement_price": "final_settlement_price",
        },
        pk_columns=("contract", "contract_month"),
        extra={"source": "finmind"},
        row_transform=_row_option_settlement,
    ),
    "TaiwanOptionDealerTradingVolumeDaily": DatasetMapping(
        dataset_code="TaiwanOptionDealerTradingVolumeDaily",
        local_table="tw_option_dealer_volume",
        column_map={},
        pk_columns=("ts", "dealer_id", "contract"),
        batch_transform=_batch_option_dealer_volume,
    ),
    "TaiwanFutOptDailyInfo": DatasetMapping(
        dataset_code="TaiwanFutOptDailyInfo",
        local_table="tw_futopt_master",
        # FinMind ships master data — one row per futures/option contract
        # code: {code, type, name}. Migration 0017 replaced the placeholder
        # `tw_futopt_daily_info` table with `tw_futopt_master`.
        column_map={
            "code": "code",
            "type": "type",
            "name": "name_zh",
        },
        pk_columns=("code",),
        extra={"source": "finmind"},
    ),
    "TaiwanFuturesOpenInterestLargeTraders": DatasetMapping(
        dataset_code="TaiwanFuturesOpenInterestLargeTraders",
        local_table="tw_futures_oi_largetraders",
        column_map={},
        pk_columns=("contract", "ts", "rank"),
        batch_transform=_batch_futures_oi_largetraders,
    ),
    "TaiwanOptionOpenInterestLargeTraders": DatasetMapping(
        dataset_code="TaiwanOptionOpenInterestLargeTraders",
        local_table="tw_option_oi_largetraders",
        column_map={},
        pk_columns=("contract", "ts", "call_put", "rank"),
        batch_transform=_batch_option_oi_largetraders,
    ),
    "TaiwanDailyShortSaleBalances": DatasetMapping(
        dataset_code="TaiwanDailyShortSaleBalances",
        local_table="tw_short_sale_balance_daily",
        column_map={
            "date": "ts",
            "stock_id": "symbol",
            "MarginShortSalesPreviousDayBalance":  "margin_prev_balance",
            "MarginShortSalesShortSales":          "margin_short_sales",
            "MarginShortSalesShortCovering":       "margin_short_covering",
            "MarginShortSalesStockRedemption":     "margin_stock_redemption",
            "MarginShortSalesCurrentDayBalance":   "margin_balance",
            "MarginShortSalesQuota":               "margin_quota",
            "SBLShortSalesPreviousDayBalance":     "sbl_prev_balance",
            "SBLShortSalesShortSales":             "sbl_short_sales",
            "SBLShortSalesShortCovering":          "sbl_short_covering",
            "SBLShortSalesReturns":                "sbl_returns",
            "SBLShortSalesAdjustments":            "sbl_adjustments",
            "SBLShortSalesCurrentDayBalance":      "sbl_balance",
            "SBLShortSalesQuota":                  "sbl_quota",
        },
        pk_columns=("market", "symbol", "ts"),
        extra={"market": "TWSE", "source": "finmind"},
        row_transform=_row_short_sale_balance,
    ),
    # ── Macro indicators (PR adding 0021) ─────────────────────────
    "TaiwanExchangeRate": DatasetMapping(
        dataset_code="TaiwanExchangeRate",
        local_table="tw_exchange_rate",
        column_map={
            "date": "ts",
            "currency": "currency",
            "cash_buy": "cash_buy",
            "cash_sell": "cash_sell",
            "spot_buy": "spot_buy",
            "spot_sell": "spot_sell",
        },
        pk_columns=("currency", "ts"),
        extra={"source": "finmind"},
        row_transform=lambda r: {
            "currency":  _to_str(r.get("currency")),
            "ts":        _to_date(r.get("ts")),
            "cash_buy":  _to_decimal(r.get("cash_buy")),
            "cash_sell": _to_decimal(r.get("cash_sell")),
            "spot_buy":  _to_decimal(r.get("spot_buy")),
            "spot_sell": _to_decimal(r.get("spot_sell")),
            "source":    r.get("source", "finmind"),
        },
    ),
    "InterestRate": DatasetMapping(
        dataset_code="InterestRate",
        local_table="macro_interest_rate",
        column_map={
            "country":           "country",
            "date":              "ts",
            "full_country_name": "full_country_name",
            "interest_rate":     "interest_rate",
        },
        pk_columns=("country", "ts"),
        extra={"source": "finmind"},
        row_transform=lambda r: {
            "country":           _to_str(r.get("country")),
            "ts":                _to_date(r.get("ts")),
            "full_country_name": _to_str(r.get("full_country_name")),
            "interest_rate":     _to_decimal(r.get("interest_rate")),
            "source":            r.get("source", "finmind"),
        },
    ),
    "GovernmentBondsYield": DatasetMapping(
        dataset_code="GovernmentBondsYield",
        local_table="us_bond_yield",
        column_map={
            "date":  "ts",
            "name":  "tenor",
            "value": "yield_pct",
        },
        pk_columns=("tenor", "ts"),
        extra={"source": "finmind"},
        row_transform=lambda r: {
            "tenor":     _to_str(r.get("tenor")),
            "ts":        _to_date(r.get("ts")),
            "yield_pct": _to_decimal(r.get("yield_pct")),
            "source":    r.get("source", "finmind"),
        },
    ),
    "CnnFearGreedIndex": DatasetMapping(
        dataset_code="CnnFearGreedIndex",
        local_table="us_fear_greed",
        column_map={
            "date":               "ts",
            "fear_greed":         "value",
            "fear_greed_emotion": "emotion",
        },
        pk_columns=("ts",),
        extra={"source": "finmind"},
        row_transform=lambda r: {
            "ts":      _to_date(r.get("ts")),
            "value":   _to_int(r.get("value")),
            "emotion": _to_str(r.get("emotion")),
            "source":  r.get("source", "finmind"),
        },
    ),
    "CrudeOilPrices": DatasetMapping(
        dataset_code="CrudeOilPrices",
        local_table="commodity_price",
        column_map={
            "date":  "ts",
            "name":  "commodity",
            "price": "price",
        },
        pk_columns=("commodity", "ts"),
        extra={"source": "finmind"},
        row_transform=lambda r: {
            "commodity": _to_str(r.get("commodity")),
            "ts":        _to_date(r.get("ts")),
            "price":     _to_decimal(r.get("price")),
            "source":    r.get("source", "finmind"),
        },
        # FinMind silently truncates multi-day requests on this dataset
        # to the start_date alone (verified 2026-05-06: a 2020-01-01..
        # 2024-12-31 range returned 0 rows; the same start with
        # end_date omitted returned 2 rows for that one day). Force
        # per-day fan-out so the runner gets every trading day.
        single_day=True,
    ),
    # ── Crypto (Binance) ────────────────────────────────────────
    # Both write crypto_ohlcv, discriminated by the `interval` column
    # the selfcrawl handler stamps ('1d' vs '1h'). `market`/`source`
    # are injected via extra; symbol is the Binance pair (BTCUSDT).
    "CryptoPrice": DatasetMapping(
        dataset_code="CryptoPrice",
        local_table="crypto_ohlcv",
        column_map={
            "symbol": "symbol",
            "interval": "interval",
            "ts": "ts",
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "volume": "volume",
            "quote_volume": "quote_volume",
            "trades": "trades",
        },
        pk_columns=("market", "symbol", "interval", "ts"),
        extra={"market": "BINANCE", "source": "binance"},
        row_transform=_row_crypto_ohlcv,
    ),
    "CryptoPriceHourly": DatasetMapping(
        dataset_code="CryptoPriceHourly",
        local_table="crypto_ohlcv",
        column_map={
            "symbol": "symbol",
            "interval": "interval",
            "ts": "ts",
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "volume": "volume",
            "quote_volume": "quote_volume",
            "trades": "trades",
        },
        pk_columns=("market", "symbol", "interval", "ts"),
        extra={"market": "BINANCE", "source": "binance"},
        row_transform=_row_crypto_ohlcv,
    ),
    "CryptoFundingRate": DatasetMapping(
        dataset_code="CryptoFundingRate",
        local_table="crypto_funding_rate",
        column_map={
            "symbol": "symbol",
            "funding_time": "funding_time",
            "funding_rate": "funding_rate",
            "mark_price": "mark_price",
        },
        pk_columns=("market", "symbol", "funding_time"),
        extra={"market": "BINANCE", "source": "binance"},
        row_transform=_row_crypto_funding_rate,
    ),
    "CryptoOpenInterest": DatasetMapping(
        dataset_code="CryptoOpenInterest",
        local_table="crypto_open_interest",
        column_map={
            "symbol": "symbol",
            "ts": "ts",
            "open_interest": "open_interest",
            "open_interest_value": "open_interest_value",
        },
        pk_columns=("market", "symbol", "ts"),
        extra={"market": "BINANCE", "source": "binance"},
        row_transform=_row_crypto_open_interest,
    ),
    # Market-wide (per_symbol=False) — one CoinGecko call per run stamps a
    # dated snapshot of the whole top-N into crypto_asset_info.
    "CryptoInfo": DatasetMapping(
        dataset_code="CryptoInfo",
        local_table="crypto_asset_info",
        column_map={
            "snapshot_date": "snapshot_date",
            "coingecko_id": "coingecko_id",
            "symbol": "symbol",
            "name": "name",
            "market_cap_rank": "market_cap_rank",
            "market_cap": "market_cap",
            "circulating_supply": "circulating_supply",
            "total_supply": "total_supply",
            "ath": "ath",
        },
        pk_columns=("snapshot_date", "coingecko_id"),
        extra={"source": "coingecko"},
        row_transform=_row_crypto_asset_info,
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
