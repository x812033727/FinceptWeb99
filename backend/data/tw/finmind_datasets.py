"""Canonical FinMind dataset registry for the Taiwan Market.

Source of truth: https://finmind.github.io/tutor/TaiwanMarket/DataList/
(snapshot 2026-05).

Every public ``api/v4/data`` dataset name FinMind exposes for TW lives
here, grouped by the same five categories the documentation uses.
This module is *data only* — it doesn't make any HTTP calls. The
runtime connector (:mod:`data.tw.finmind_connector`) owns the actual
fetch + quota plumbing.

Why a registry:
  - A single grep target when wiring a new ingest task or admin tool.
  - Lets future code enumerate "which datasets are sponsor-only?" or
    "which take a per-symbol ``data_id``?" without re-reading the FinMind
    docs.
  - Keeps the connector lean — adding a new dataset means appending a
    spec here, not bloating the connector with another wrapper.

Field semantics:
  - ``name``: the FinMind ``dataset`` query-string value, exactly as
    documented. Typos here = silent 200/empty from the API.
  - ``description``: short Traditional-Chinese label matching the doc.
  - ``per_symbol``: True when ``data_id=<symbol>`` is the canonical
    call shape; False when ``data_id=""`` returns the whole market
    (or there is no per-symbol axis, e.g. macro datasets).
  - ``sponsor_tier``: True when the registered free tier returns
    silent-deny / paywall body — used by ``finmind_paywall`` callers
    to decide whether to fall back to a per-symbol fan-out or surface
    the paywall to the user. Best-effort; FinMind moves datasets
    between tiers occasionally and we update on observation.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    description: str
    per_symbol: bool
    sponsor_tier: bool = False


# ── Technical (價量) ──────────────────────────────────────────────

TECHNICAL: tuple[DatasetSpec, ...] = (
    DatasetSpec("TaiwanStockInfo", "台股總覽", per_symbol=False),
    DatasetSpec("TaiwanStockInfoWithWarrant", "台股總覽(含權證)", per_symbol=False),
    DatasetSpec("TaiwanStockInfoWithWarrantSummary", "台股權證標的對照表", per_symbol=False),
    DatasetSpec("TaiwanStockTradingDate", "台股交易日", per_symbol=False),
    DatasetSpec("TaiwanStockPrice", "台灣股價資料表", per_symbol=True),
    DatasetSpec("TaiwanStockPriceAdj", "台灣還原股價資料表", per_symbol=True),
    DatasetSpec("TaiwanStockPriceTick", "台灣股價歷史逐筆資料表", per_symbol=True, sponsor_tier=True),
    DatasetSpec("TaiwanStockPER", "個股 PER、PBR 資料表", per_symbol=True),
    DatasetSpec("TaiwanStockStatisticsOfOrderBookAndTrade", "每 5 秒委託成交統計", per_symbol=False, sponsor_tier=True),
    DatasetSpec("TaiwanVariousIndicators5Seconds", "台股加權指數 (5 秒)", per_symbol=False, sponsor_tier=True),
    DatasetSpec("TaiwanStockDayTrading", "當日沖銷交易標的及成交量值", per_symbol=False, sponsor_tier=True),
    DatasetSpec("TaiwanStockTotalReturnIndex", "加權、櫃買報酬指數", per_symbol=True, sponsor_tier=True),
    DatasetSpec("TaiwanStock10Year", "台灣個股十年線資料表", per_symbol=True),
    DatasetSpec("TaiwanStockKBar", "台股分 K 資料表", per_symbol=True, sponsor_tier=True),
    DatasetSpec("TaiwanStockWeekPrice", "台股週 K 資料表", per_symbol=True),
    DatasetSpec("TaiwanStockMonthPrice", "台股月 K 資料表", per_symbol=True),
    DatasetSpec("TaiwanStockEvery5SecondsIndex", "每 5 秒指數統計", per_symbol=False, sponsor_tier=True),
    DatasetSpec("TaiwanStockSuspended", "台股暫停交易公告", per_symbol=False, sponsor_tier=True),
    DatasetSpec("TaiwanStockDayTradingSuspension", "暫停先賣後買當沖預告表", per_symbol=False),
    DatasetSpec("TaiwanStockPriceLimit", "每日漲跌停價", per_symbol=False),
)


# ── Chip (籌碼) ───────────────────────────────────────────────────

CHIP: tuple[DatasetSpec, ...] = (
    DatasetSpec("TaiwanStockMarginPurchaseShortSale", "個股融資融劵表", per_symbol=True),
    DatasetSpec("TaiwanStockTotalMarginPurchaseShortSale", "整體市場融資融劵表", per_symbol=False),
    DatasetSpec("TaiwanStockInstitutionalInvestorsBuySell", "個股三大法人買賣表", per_symbol=True),
    DatasetSpec("TaiwanStockTotalInstitutionalInvestors", "整體三大市場法人買賣表", per_symbol=False, sponsor_tier=True),
    DatasetSpec("TaiwanStockShareholding", "外資持股表", per_symbol=False, sponsor_tier=True),
    DatasetSpec("TaiwanStockHoldingSharesPer", "股權持股分級表", per_symbol=True, sponsor_tier=True),
    DatasetSpec("TaiwanStockSecuritiesLending", "借券成交明細", per_symbol=True),
    DatasetSpec("TaiwanStockMarginShortSaleSuspension", "暫停融券賣出表(融券回補日)", per_symbol=False),
    DatasetSpec("TaiwanDailyShortSaleBalances", "信用額度總量管制餘額表", per_symbol=False),
    DatasetSpec("TaiwanSecuritiesTraderInfo", "證券商資訊表", per_symbol=False),
    # FinMind dropped these three from `/api/v4/data` enum sometime before
    # 2026-05; calls return 422. They stay registered (and in
    # `dataset_catalog`) because the registry/catalog invariants enforce
    # bidirectional consistency, and the connector still references the
    # first two. Migration 0020 sets `dataset_sources.enabled=false` for
    # all three so the chain / scheduler never claims them; the
    # init_db UPSERT preserves `enabled`, so the disabled state persists.
    DatasetSpec("TaiwanStockTradingDailyReport", "台股分點資料表 (legacy; FinMind enum-removed)", per_symbol=True, sponsor_tier=True),
    DatasetSpec("TaiwanStockWarrantTradingDailyReport", "台股權證分點資料表 (by 股票代碼 / 券商代碼)", per_symbol=True, sponsor_tier=True),
    DatasetSpec("TaiwanStockGovernmentBankBuySell", "台股八大行庫買賣表", per_symbol=False, sponsor_tier=True),
    DatasetSpec("TaiwanTotalExchangeMarginMaintenance", "台灣大盤融資維持率", per_symbol=False),
    DatasetSpec("TaiwanStockTradingDailyReportSecIdAgg", "當日卷商分點統計表 (legacy; FinMind enum-removed)", per_symbol=False, sponsor_tier=True),
    DatasetSpec("TaiwanStockBlockTradingDailyReport", "鉅額交易買賣日報表", per_symbol=False),
    DatasetSpec("TaiwanStockBlockTrade", "鉅額交易日成交資訊", per_symbol=False),
    DatasetSpec("TaiwanStockLoanCollateralBalance", "借貸款項擔保品餘額表", per_symbol=False),
    DatasetSpec("TaiwanStockDispositionSecuritiesPeriod", "公布處置有價證券表", per_symbol=False, sponsor_tier=True),
    DatasetSpec("TaiwanStockDayTradingBorrowingFeeRate", "現股當日沖銷券差借券費率", per_symbol=True),
)


# ── Fundamental (基本面) ──────────────────────────────────────────

FUNDAMENTAL: tuple[DatasetSpec, ...] = (
    DatasetSpec("TaiwanStockCashFlowsStatement", "現金流量表", per_symbol=True),
    DatasetSpec("TaiwanStockFinancialStatements", "綜合損益表", per_symbol=True),
    DatasetSpec("TaiwanStockBalanceSheet", "資產負債表", per_symbol=True),
    DatasetSpec("TaiwanStockDividend", "股利政策表", per_symbol=True),
    DatasetSpec("TaiwanStockDividendResult", "除權除息結果表", per_symbol=True),
    DatasetSpec("TaiwanStockMonthRevenue", "月營收表", per_symbol=True),
    DatasetSpec("TaiwanStockCapitalReductionReferencePrice", "減資恢復買賣參考價格", per_symbol=True),
    DatasetSpec("TaiwanStockMarketValue", "台灣股價市值表", per_symbol=True),
    DatasetSpec("TaiwanStockDelisting", "台灣股票下市櫃表", per_symbol=False),
    DatasetSpec("TaiwanStockMarketValueWeight", "台股市值比重表", per_symbol=False),
    DatasetSpec("TaiwanStockSplitPrice", "台股分割後參考價", per_symbol=True),
    DatasetSpec("TaiwanStockParValueChange", "台灣股票變更面額恢復買賣參考價格", per_symbol=True),
)


# ── Derivatives (期貨 / 選擇權) ───────────────────────────────────

DERIVATIVE: tuple[DatasetSpec, ...] = (
    DatasetSpec("TaiwanFutOptDailyInfo", "期貨、選擇權日成交資訊總覽", per_symbol=False),
    DatasetSpec("TaiwanFuturesDaily", "期貨日成交資訊", per_symbol=True),
    DatasetSpec("TaiwanOptionDaily", "選擇權日成交資訊", per_symbol=True),
    DatasetSpec("TaiwanFuturesTick", "期貨交易明細表", per_symbol=True, sponsor_tier=True),
    DatasetSpec("TaiwanOptionTick", "選擇權交易明細表", per_symbol=True, sponsor_tier=True),
    DatasetSpec("TaiwanFuturesInstitutionalInvestors", "期貨三大法人買賣", per_symbol=True),
    DatasetSpec("TaiwanOptionInstitutionalInvestors", "選擇權三大法人買賣", per_symbol=True),
    DatasetSpec("TaiwanFuturesInstitutionalInvestorsAfterHours", "期貨夜盤三大法人買賣", per_symbol=True),
    DatasetSpec("TaiwanOptionInstitutionalInvestorsAfterHours", "選擇權夜盤三大法人買賣", per_symbol=True),
    DatasetSpec("TaiwanFuturesDealerTradingVolumeDaily", "期貨各卷商每日交易", per_symbol=False),
    DatasetSpec("TaiwanOptionDealerTradingVolumeDaily", "選擇權各卷商每日交易", per_symbol=False),
    DatasetSpec("TaiwanFuturesOpenInterestLargeTraders", "期貨大額交易人未沖銷部位", per_symbol=True),
    DatasetSpec("TaiwanOptionOpenInterestLargeTraders", "選擇權大額交易人未沖銷部位", per_symbol=True),
    DatasetSpec("TaiwanFuturesSpreadTrading", "期貨價差行情表", per_symbol=False, sponsor_tier=True),
    DatasetSpec("TaiwanFuturesFinalSettlementPrice", "期貨最後結算價", per_symbol=True),
    DatasetSpec("TaiwanOptionFinalSettlementPrice", "選擇權最後結算價", per_symbol=True),
)


# ── Real-time (即時) ──────────────────────────────────────────────

REALTIME: tuple[DatasetSpec, ...] = (
    DatasetSpec("taiwan_stock_tick_snapshot", "台股即時資訊", per_symbol=True, sponsor_tier=True),
    DatasetSpec("TaiwanFutOptTickInfo", "期貨、選擇權即時報價總覽", per_symbol=False, sponsor_tier=True),
    DatasetSpec("taiwan_futures_snapshot", "台股期貨即時資訊", per_symbol=True, sponsor_tier=True),
    DatasetSpec("taiwan_options_snapshot", "台股選擇權即時資訊", per_symbol=True, sponsor_tier=True),
)


# ── Convertible Bond (可轉債) ─────────────────────────────────────

CONVERTIBLE_BOND: tuple[DatasetSpec, ...] = (
    DatasetSpec("TaiwanStockConvertibleBondInfo", "可轉債總覽", per_symbol=False),
    DatasetSpec("TaiwanStockConvertibleBondDaily", "可轉債日成交資訊", per_symbol=True),
    DatasetSpec("TaiwanStockConvertibleBondInstitutionalInvestors", "可轉債三大法人日交易資訊", per_symbol=True),
    DatasetSpec("TaiwanStockConvertibleBondDailyOverview", "可轉債每日總覽資訊", per_symbol=False),
)


# ── Other (其他) ─────────────────────────────────────────────────

OTHER: tuple[DatasetSpec, ...] = (
    DatasetSpec("TaiwanStockNews", "相關新聞", per_symbol=True, sponsor_tier=True),
    DatasetSpec("TaiwanBusinessIndicator", "台灣每月景氣對策信號表", per_symbol=False),
    DatasetSpec("TaiwanStockIndustryChain", "個體公司所屬產業鏈", per_symbol=True),
    # FinMind dropped this from `/api/v4/data` enum sometime before 2026-05;
    # calls return 422. Migration 0020 disables the dataset_sources row
    # so the chain / scheduler never claims it; the spec stays here
    # because the connector still references the name and the registry/
    # catalog invariants enforce bidirectional consistency.
    DatasetSpec("TaiwanStockBuyBack", "庫藏股公告 (legacy; FinMind enum-removed)", per_symbol=False, sponsor_tier=True),
)


REGISTRY: dict[str, tuple[DatasetSpec, ...]] = {
    "technical":        TECHNICAL,
    "chip":             CHIP,
    "fundamental":      FUNDAMENTAL,
    "derivative":       DERIVATIVE,
    "realtime":         REALTIME,
    "convertible_bond": CONVERTIBLE_BOND,
    "other":            OTHER,
}


def all_datasets() -> tuple[DatasetSpec, ...]:
    """Flat tuple of every dataset across all categories."""
    return tuple(spec for group in REGISTRY.values() for spec in group)


def find_dataset(name: str) -> DatasetSpec | None:
    """Resolve a dataset spec by exact name. Returns None when the
    name isn't registered — callers can ``raise`` if that's a hard
    requirement, or fall through to a generic call."""
    for spec in all_datasets():
        if spec.name == name:
            return spec
    return None
