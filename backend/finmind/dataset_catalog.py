"""Mapping from FinMind dataset → local table + Phase A/B sources.

The pure-data registry at `data.tw.finmind_datasets` knows what
datasets *exist* (name, category, per_symbol, sponsor_tier). This
module adds the FinMind-clone-specific fields:

  - `local_table`: the destination table in the FinMind clone DB
    (empty string until Phase 1 migration creates it)
  - `primary_source`: where to fetch from during Phase A (always
    'finmind' while the subscription is active)
  - `fallback_source`: the Phase B source to switch to when the
    subscription expires
  - `ingest_freq`: how often the daily/monthly cron should hit
    the upstream

The init script seeds `dataset_sources` from this catalog. As Phase 1
migrations land destination tables, this module gets updated to fill
in the `local_table` strings — re-running `init_db` UPSERTs the
changes.

Invariants:
  - Every entry's `dataset_code` must exist in
    `data.tw.finmind_datasets.all_datasets()` (validated at import).
  - Tables marked `realtime` shouldn't be persisted (transient quote
    data) — their `local_table` stays "" by design.
"""
from __future__ import annotations

from dataclasses import dataclass

from data.tw.finmind_datasets import REGISTRY as FINMIND_REGISTRY
from data.tw.finmind_datasets import find_dataset


@dataclass(frozen=True)
class CatalogEntry:
    dataset_code: str           # = DatasetSpec.name
    local_table: str            # "" if Phase 1 hasn't built the table yet
    primary_source: str         # 'finmind' default
    fallback_source: str | None # 'twse' | 'tpex' | 'taifex' | 'mops' | 'tdcc' | None
    ingest_freq: str            # 'daily' | 'monthly' | 'quarterly' | 'realtime' | 'on_demand'
    # FinMind only serves one day per call for these datasets (KBar,
    # PriceTick, BlockTradingDailyReport, GovernmentBankBuySell,
    # {Trading,Warrant}TradingDailyReport — the FinMind validator
    # rejects multi-day queries with "size is too large, end_date
    # parameter need be none"). When True, the scheduler emits one
    # DueChunk per day in the suggested range instead of one chunk for
    # the whole window — gives per-day retry granularity in
    # `backfill_progress`. The runner has its own mapping-level
    # `single_day` flag as a backstop for callers that bypass the
    # scheduler (CLI backfill with --days N, direct ingest_chunk).
    single_day: bool = False


# ── Technical (價量) ──────────────────────────────────────────────
TECHNICAL: tuple[CatalogEntry, ...] = (
    CatalogEntry("TaiwanStockInfo",                          "tw_stock_info",        "finmind", "twse",   "monthly"),
    CatalogEntry("TaiwanStockInfoWithWarrant",               "tw_stock_info",        "finmind", "twse",   "monthly"),
    CatalogEntry("TaiwanStockInfoWithWarrantSummary",        "",                     "finmind", "twse",   "monthly"),
    CatalogEntry("TaiwanStockTradingDate",                   "tw_trading_calendar",  "finmind", "twse",   "monthly"),
    CatalogEntry("TaiwanStockPrice",                         "ohlcv_daily",          "finmind", "twse",   "daily"),
    CatalogEntry("TaiwanStockPriceAdj",                      "tw_stock_price_adj",   "finmind", None,     "daily"),
    CatalogEntry("TaiwanStockPriceTick",                     "tw_stock_tick",        "finmind", None,     "daily", single_day=True),
    CatalogEntry("TaiwanStockPER",                           "tw_stock_per_daily",   "finmind", "twse",   "daily"),
    CatalogEntry("TaiwanStockStatisticsOfOrderBookAndTrade", "", "finmind", None,     "daily"),
    CatalogEntry("TaiwanVariousIndicators5Seconds",          "", "finmind", None,     "realtime"),
    CatalogEntry("TaiwanStockDayTrading",                    "tw_day_trade_daily",       "finmind", "twse",   "daily"),
    CatalogEntry("TaiwanStockTotalReturnIndex",              "tw_total_return_index",    "finmind", "twse",   "daily"),
    CatalogEntry("TaiwanStock10Year",                        "", "finmind", None,     "on_demand"),
    CatalogEntry("TaiwanStockKBar",                          "tw_stock_minute",      "finmind", None,     "daily", single_day=True),
    CatalogEntry("TaiwanStockWeekPrice",                     "", "finmind", None,     "on_demand"),
    CatalogEntry("TaiwanStockMonthPrice",                    "", "finmind", None,     "on_demand"),
    CatalogEntry("TaiwanStockEvery5SecondsIndex",            "", "finmind", None,     "realtime"),
    CatalogEntry("TaiwanStockSuspended",                     "tw_suspended",          "finmind", "twse",   "daily"),
    CatalogEntry("TaiwanStockDayTradingSuspension",          "tw_suspended",          "finmind", "twse",   "daily"),
    CatalogEntry("TaiwanStockPriceLimit",                    "tw_price_limit_daily",  "finmind", "twse",   "daily"),
)


# ── Chip (籌碼) ───────────────────────────────────────────────────
CHIP: tuple[CatalogEntry, ...] = (
    CatalogEntry("TaiwanStockMarginPurchaseShortSale",          "tw_margin_daily",            "finmind", "twse", "daily"),
    CatalogEntry("TaiwanStockTotalMarginPurchaseShortSale",     "tw_total_margin_daily",      "finmind", "twse", "daily"),
    CatalogEntry("TaiwanStockInstitutionalInvestorsBuySell",    "tw_institutional_daily",     "finmind", "twse", "daily"),
    CatalogEntry("TaiwanStockTotalInstitutionalInvestors",      "tw_total_inst_daily",        "finmind", "twse", "daily"),
    CatalogEntry("TaiwanStockShareholding",                     "tw_foreign_shareholding",    "finmind", "twse", "daily"),
    CatalogEntry("TaiwanStockHoldingSharesPer",                 "tw_holdings_aggregates",     "finmind", "tdcc", "weekly"),
    CatalogEntry("TaiwanStockSecuritiesLending",                "tw_securities_lending",      "finmind", "twse", "daily"),
    CatalogEntry("TaiwanStockMarginShortSaleSuspension",        "tw_short_sale_suspension",   "finmind", "twse", "daily"),
    CatalogEntry("TaiwanDailyShortSaleBalances",                "tw_short_sale_balance_daily","finmind", "twse", "daily"),
    CatalogEntry("TaiwanSecuritiesTraderInfo",                  "tw_broker_master",           "finmind", "twse", "monthly"),
    CatalogEntry("TaiwanStockTradingDailyReport",               "tw_broker_daily_report",     "finmind", None,   "daily", single_day=True),
    CatalogEntry("TaiwanStockWarrantTradingDailyReport",        "tw_broker_daily_report",     "finmind", None,   "daily", single_day=True),
    CatalogEntry("TaiwanStockGovernmentBankBuySell",            "tw_govt_bank_flow",          "finmind", None,   "daily", single_day=True),
    CatalogEntry("TaiwanTotalExchangeMarginMaintenance",        "tw_margin_maintenance",      "finmind", "twse", "daily"),
    CatalogEntry("TaiwanStockTradingDailyReportSecIdAgg",       "",                           "finmind", None,   "daily"),
    CatalogEntry("TaiwanStockBlockTradingDailyReport",          "tw_block_trade",             "finmind", "twse", "daily", single_day=True),
    CatalogEntry("TaiwanStockBlockTrade",                       "tw_block_trade",             "finmind", "twse", "daily"),
    CatalogEntry("TaiwanStockLoanCollateralBalance",            "tw_loan_collateral",         "finmind", "twse", "daily"),
    CatalogEntry("TaiwanStockDispositionSecuritiesPeriod",      "tw_disposition",             "finmind", "twse", "daily"),
    CatalogEntry("TaiwanStockDayTradingBorrowingFeeRate",       "tw_day_trade_fee",           "finmind", "twse", "daily"),
)


# ── Fundamental (基本面) ──────────────────────────────────────────
FUNDAMENTAL: tuple[CatalogEntry, ...] = (
    CatalogEntry("TaiwanStockCashFlowsStatement",                 "tw_cash_flow",         "finmind", "mops", "quarterly"),
    CatalogEntry("TaiwanStockFinancialStatements",                "tw_income_statement",  "finmind", "mops", "quarterly"),
    CatalogEntry("TaiwanStockBalanceSheet",                       "tw_balance_sheet",     "finmind", "mops", "quarterly"),
    CatalogEntry("TaiwanStockDividend",                           "tw_dividend",          "finmind", "twse", "on_demand"),
    CatalogEntry("TaiwanStockDividendResult",                     "tw_dividend_result",   "finmind", "twse", "on_demand"),
    CatalogEntry("TaiwanStockMonthRevenue",                       "tw_revenue_monthly",   "finmind", "mops", "monthly"),
    CatalogEntry("TaiwanStockCapitalReductionReferencePrice",     "tw_capital_reduction", "finmind", "twse", "on_demand"),
    CatalogEntry("TaiwanStockMarketValue",                        "tw_market_value",      "finmind", None,   "daily"),
    CatalogEntry("TaiwanStockDelisting",                          "tw_delisting",         "finmind", "twse", "monthly"),
    CatalogEntry("TaiwanStockMarketValueWeight",                  "tw_market_value_weight", "finmind", "twse", "daily"),
    CatalogEntry("TaiwanStockSplitPrice",                         "tw_split",             "finmind", "twse", "on_demand"),
    CatalogEntry("TaiwanStockParValueChange",                     "tw_par_value_change",  "finmind", "twse", "on_demand"),
)


# ── Derivative (期權) ─────────────────────────────────────────────
DERIVATIVE: tuple[CatalogEntry, ...] = (
    CatalogEntry("TaiwanFutOptDailyInfo",                              "tw_futopt_daily_info",            "finmind", "taifex", "daily"),
    CatalogEntry("TaiwanFuturesDaily",                                 "tw_futures_daily",                "finmind", "taifex", "daily"),
    CatalogEntry("TaiwanOptionDaily",                                  "tw_option_daily",                 "finmind", "taifex", "daily"),
    CatalogEntry("TaiwanFuturesTick",                                  "",                                "finmind", None,     "daily"),
    CatalogEntry("TaiwanOptionTIck",                                   "",                                "finmind", None,     "daily"),
    CatalogEntry("TaiwanFuturesInstitutionalInvestors",                "tw_futures_inst_daily",           "finmind", "taifex", "daily"),
    CatalogEntry("TaiwanOptionInstitutionalInvestors",                 "tw_option_inst_daily",            "finmind", "taifex", "daily"),
    CatalogEntry("TaiwanFuturesInstitutionalInvestorsAfterHours",      "tw_futures_inst_daily",           "finmind", "taifex", "daily"),
    CatalogEntry("TaiwanOptionInstitutionalInvestorsAfterHours",       "tw_option_inst_daily",            "finmind", "taifex", "daily"),
    CatalogEntry("TaiwanFuturesDealerTradingVolumeDaily",              "tw_futures_dealer_volume",        "finmind", "taifex", "daily"),
    CatalogEntry("TaiwanOptionDealerTradingVolumeDaily",               "tw_option_dealer_volume",         "finmind", "taifex", "daily"),
    CatalogEntry("TaiwanFuturesOpenInterestLargeTraders",              "tw_futures_oi_largetraders",      "finmind", "taifex", "daily"),
    CatalogEntry("TaiwanOptionOpenInterestLargeTraders",               "tw_option_oi_largetraders",       "finmind", "taifex", "daily"),
    CatalogEntry("TaiwanFuturesSpreadTrading",                         "tw_futures_spread",               "finmind", "taifex", "daily"),
    CatalogEntry("TaiwanFuturesFinalSettlementPrice",                  "tw_futures_settlement",           "finmind", "taifex", "monthly"),
    CatalogEntry("TaiwanOptionFinalSettlementPrice",                   "tw_option_settlement",            "finmind", "taifex", "monthly"),
)


# ── Realtime (即時) — NOT persisted ──────────────────────────────
# These return transient snapshot data; we keep registry rows so the
# admin UI can list them, but `local_table` stays "" forever.
REALTIME: tuple[CatalogEntry, ...] = (
    CatalogEntry("taiwan_stock_tick_snapshot", "", "finmind", None, "realtime"),
    CatalogEntry("TaiwanFutOptTickInfo",       "", "finmind", None, "realtime"),
    CatalogEntry("taiwan_futures_snapshot",    "", "finmind", None, "realtime"),
    CatalogEntry("taiwan_options_snapshot",    "", "finmind", None, "realtime"),
)


# ── Convertible Bond (可轉債) ─────────────────────────────────────
CONVERTIBLE_BOND: tuple[CatalogEntry, ...] = (
    CatalogEntry("TaiwanStockConvertibleBondInfo",                     "tw_cb_info",          "finmind", "tpex", "monthly"),
    CatalogEntry("TaiwanStockConvertibleBondDaily",                    "tw_cb_daily",         "finmind", "tpex", "daily"),
    CatalogEntry("TaiwanStockConvertibleBondInstitutionalInvestors",   "tw_cb_inst_daily",    "finmind", "tpex", "daily"),
    CatalogEntry("TaiwanStockConvertibleBondDailyOverview",            "tw_cb_daily_overview","finmind", "tpex", "daily"),
)


# ── Other ────────────────────────────────────────────────────────
OTHER: tuple[CatalogEntry, ...] = (
    CatalogEntry("TaiwanStockNews",         "tw_news_articles",       "finmind", None, "daily"),
    CatalogEntry("TaiwanBusinessIndicator", "tw_business_indicator",  "finmind", None, "monthly"),
    CatalogEntry("TaiwanStockIndustryChain","tw_industry_chain", "finmind", None,   "monthly"),
    CatalogEntry("TaiwanStockBuyBack",      "tw_buyback",             "finmind", "twse", "daily"),
)


CATALOG: dict[str, tuple[CatalogEntry, ...]] = {
    "technical":        TECHNICAL,
    "chip":             CHIP,
    "fundamental":      FUNDAMENTAL,
    "derivative":       DERIVATIVE,
    "realtime":         REALTIME,
    "convertible_bond": CONVERTIBLE_BOND,
    "other":            OTHER,
}


def all_entries() -> tuple[tuple[str, CatalogEntry], ...]:
    """Flat iterable of (category, entry) pairs."""
    return tuple(
        (cat, entry)
        for cat, entries in CATALOG.items()
        for entry in entries
    )


def _validate_against_finmind_registry() -> None:
    """At import time, ensure every catalog entry corresponds to a real
    FinMind dataset name and no FinMind dataset is missing from the
    catalog. Prevents drift between the upstream-truth registry and our
    local routing table.
    """
    catalog_codes = {entry.dataset_code for _, entry in all_entries()}
    finmind_codes = {
        spec.name
        for group in FINMIND_REGISTRY.values()
        for spec in group
    }

    unknown = catalog_codes - finmind_codes
    if unknown:
        raise RuntimeError(
            f"finmind.dataset_catalog references unknown FinMind datasets: "
            f"{sorted(unknown)}. Either fix the typo or update "
            f"data/tw/finmind_datasets.py to include them."
        )

    missing = finmind_codes - catalog_codes
    if missing:
        raise RuntimeError(
            f"finmind.dataset_catalog is missing FinMind datasets: "
            f"{sorted(missing)}. Add a CatalogEntry for each so "
            f"`init_db` can seed the routing table."
        )


def lookup_finmind_spec(dataset_code: str):
    """Convenience accessor — returns the underlying FinMind DatasetSpec
    for a catalog entry, or None if the code isn't registered.
    """
    return find_dataset(dataset_code)


_validate_against_finmind_registry()
