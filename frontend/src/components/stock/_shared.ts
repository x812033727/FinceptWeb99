/**
 * Shared types, API fetchers, and formatter aliases used by every
 * panel in the StockDetailPage extraction.
 *
 * Keeping these in one module (rather than colocating with each
 * panel) means a panel addition just imports — no risk of the new
 * panel re-declaring a slightly different `RevenueRow` shape than
 * what the backend returns.
 */
import api from "@/lib/api";
import {
  formatCompact,
  formatNumber,
  formatPct as formatPct_,
} from "@/lib/formatters";
import type { OHLCVBar, Market } from "@/types/market";

// ── period / interval ─────────────────────────────────────────────

export type Period = "1d" | "5d" | "1mo" | "3mo" | "1y" | "5y";
export type Interval = "1m" | "5m" | "15m" | "1h" | "1d" | "1wk";

export const PERIOD_INTERVAL: Record<Period, Interval> = {
  "1d": "5m", "5d": "15m", "1mo": "1h", "3mo": "1d", "1y": "1d", "5y": "1wk",
};

// Crypto period → Kraken interval + bar limit. Kraken caps at 720 bars/req.
export const CRYPTO_PERIOD: Record<Period, { interval: string; limit: number }> = {
  "1d": { interval: "5m", limit: 288 },
  "5d": { interval: "15m", limit: 480 },
  "1mo": { interval: "1h", limit: 720 },
  "3mo": { interval: "4h", limit: 540 },
  "1y": { interval: "1d", limit: 365 },
  "5y": { interval: "1w", limit: 260 },
};

// ── tab unions ────────────────────────────────────────────────────

export type USTab = "chart" | "financials" | "options" | "news" | "ai_report";
export type TWTab = "chart" | "health" | "valuation" | "holdings" | "dividends" | "institutional" | "margin" | "revenue" | "news" | "ai_report";
export type CryptoTab = "chart" | "news";

// ── data shapes ───────────────────────────────────────────────────

export interface OptionRow {
  ticker: string;
  strike_price: number;
  expiration_date: string;
  contract_type: "call" | "put";
  bid: number | null;
  ask: number | null;
  last_price: number | null;
  volume: number | null;
  open_interest: number | null;
  implied_volatility: number | null;
  delta: number | null;
  gamma: number | null;
  theta: number | null;
  vega: number | null;
  data_source: string;
}

export interface OptionExpiryAnalytics {
  expiration_date: string;
  days_to_expiry: number;
  contract_count: number;
  call_open_interest: number;
  put_open_interest: number;
  put_call_open_interest_ratio: number | null;
  call_volume: number;
  put_volume: number;
  put_call_volume_ratio: number | null;
  atm_iv: number | null;
  atm_call_iv: number | null;
  atm_put_iv: number | null;
  atm_call_strike: number | null;
  atm_put_strike: number | null;
  expected_move: number | null;
  expected_move_pct: number | null;
  put_90_iv: number | null;
  put_90_strike: number | null;
  call_110_iv: number | null;
  call_110_strike: number | null;
  wing_skew_iv_points: number | null;
  max_pain: number | null;
  max_pain_distance_pct: number | null;
  max_pain_total_payout: number | null;
}

export interface OptionsAnalysisResponse {
  symbol: string;
  spot: number | null;
  spot_source: string | null;
  as_of: string;
  contracts: OptionRow[];
  expiries: OptionExpiryAnalytics[];
  quality: {
    status: "good" | "degraded" | "unavailable";
    flags: string[];
    sources: string[];
    rows_received: number;
    rows_usable: number;
    iv_coverage_pct: number;
    open_interest_coverage_pct: number;
  };
  methodology: Record<string, string>;
}

export interface InstitutionalRow {
  date: string;
  fini_buy: number;
  fini_sell: number;
  sitc_buy: number;
  sitc_sell: number;
  dealer_buy: number;
  dealer_sell: number;
}

export interface MarginRow {
  date: string;
  margin_purchase: number;
  margin_balance: number;
  short_sale: number;
  short_balance: number;
}

export interface RevenueRow {
  date: string;
  revenue: number;
  revenue_mom: number | null;
  revenue_yoy: number | null;
}

export type Light = "green" | "yellow" | "red" | "gray";

export interface HealthPeriod {
  date: string;
  revenue: number | null;
  net_income: number | null;
  eps: number | null;
  gross_margin: number | null;
  operating_margin: number | null;
  net_margin: number | null;
  debt_ratio: number | null;
  current_ratio: number | null;
  operating_cf: number | null;
  free_cf: number | null;
  total_equity: number | null;
  total_assets: number | null;
  total_liabilities: number | null;
  current_assets: number | null;
  current_liabilities: number | null;
  cash: number | null;
  capex: number | null;
  revenue_yoy: number | null;
  net_income_yoy: number | null;
  eps_yoy: number | null;
  cash_conversion: number | null;
  free_cf_margin: number | null;
}

export interface HealthResponse {
  symbol: string;
  market: "TW";
  periods: HealthPeriod[];
  summary: {
    latest_roe: number | null;
    latest_debt_ratio: number | null;
    latest_gross_margin: number | null;
    latest_net_margin: number | null;
    revenue_yoy: number | null;
    cf_positive_streak_4q: number;
    latest_roa: number | null;
    ttm_revenue: number | null;
    ttm_net_income: number | null;
    ttm_operating_cf: number | null;
    ttm_free_cf: number | null;
    ttm_net_margin: number | null;
    cash_conversion_ttm: number | null;
    asset_turnover: number | null;
    equity_multiplier: number | null;
    dupont_roe: number | null;
  };
  lights: {
    profitability: Light;
    safety: Light;
    growth: Light;
    cash_flow: Light;
  };
  signals: Array<{
    code: string;
    direction: "positive" | "risk" | "neutral";
    value: number;
    unit: string;
  }>;
  quality: {
    status: "good" | "degraded" | "unavailable";
    flags: string[];
    sources: string[];
    statement_periods: { income: number; balance_sheet: number; cash_flow: number };
    latest_core_coverage_pct: number;
  };
  methodology: Record<string, string>;
}

export interface ValuationBandResponse {
  symbol: string;
  metric: "pe" | "pb";
  series: { date: string; value: number | null }[];
  stats: {
    mean: number | null;
    std: number | null;
    min: number | null;
    max: number | null;
    p10: number | null;
    p25: number | null;
    p50: number | null;
    p75: number | null;
    p90: number | null;
    current: number | null;
    current_z: number | null;
  };
}

export interface DividendRow {
  date: string;
  ex_date: string | null;
  cash_dividend: number;
  stock_dividend: number;
}

export interface ETFHolding {
  symbol: string;
  name_zh: string;
  weight: number;
}

export interface ETFHoldingsResponse {
  as_of: string | null;
  holdings: ETFHolding[];
}

export interface TWSecurityMaster {
  symbol: string;
  effective_from: string;
  effective_to: string | null;
  instrument_type: string;
  asset_class: string;
  is_etf: boolean;
  is_bond_etf: boolean;
  is_leveraged: boolean;
  is_inverse: boolean;
  board_lot_size: number;
  odd_lot_size: number;
  sell_tax_bps: number;
  tax_rule_code: string;
  source: string;
  classification_source_url: string | null;
  tax_source_url: string | null;
  confidence: string;
  is_manual_override: boolean;
  fallback: boolean;
}

// ── helpers ──────────────────────────────────────────────────────

export const isTWETF = (symbol: string) => /^00\d{2,4}[A-Z]?$/.test(symbol);

// Thin aliases over `@/lib/formatters` — keep call-site shape stable
// across panels so PR #156's `+993%` regression cannot recur.
export const fmt = (n: number | null | undefined, d = 2) => formatNumber(n, d);
export const fmtPct = (n: number | null | undefined, alreadyPct = true) =>
  formatPct_(n, { alreadyPct });
export const fmtPct1 = (n: number | null | undefined) =>
  formatPct_(n, { signed: false });
export const fmtK = (n: number) => formatCompact(n);

// ── API fetchers ─────────────────────────────────────────────────

// TW data is daily-only — no intraday endpoint exists. The
// StockDetailPage period selector hides `1d` / `5d` for TW so this
// map only needs to cover the month-based ranges. Defensive fallback
// to 3 months if a future caller passes an unmapped period.
const TW_PERIOD_MONTHS: Record<Period, number> = {
  "1d": 1, "5d": 1, "1mo": 1, "3mo": 3, "1y": 12, "5y": 60,
};

export const fetchHistory = (mkt: Market, sym: string, period: Period) =>
  api.get<OHLCVBar[]>(
    mkt === "US"
      ? `/us/history/${sym}?period=${period}&interval=${PERIOD_INTERVAL[period]}`
      : mkt === "CRYPTO"
        ? `/crypto/history/${sym}?interval=${CRYPTO_PERIOD[period].interval}&limit=${CRYPTO_PERIOD[period].limit}`
        : `/tw/history/${sym}?months=${TW_PERIOD_MONTHS[period]}`
  ).then((r) => r.data);

export const fetchQuote = (mkt: Market, sym: string) =>
  api.get<Record<string, unknown>>(
    mkt === "US" ? `/us/quote/${sym}?verify=true`
      : mkt === "CRYPTO" ? `/crypto/quote/${sym}`
      : `/tw/quote/${sym}?verify=true`
  ).then((r) => r.data);

export const fetchFundamentals = (mkt: Market, sym: string) =>
  api.get<Record<string, unknown>>(
    mkt === "US" ? `/us/fundamentals/${sym}` : `/tw/fundamentals/${sym}`
  ).then((r) => r.data);

export const fetchFinancials = (sym: string) =>
  api.get<{ symbol: string; source: string; data: unknown }>(`/us/financials/${sym}`)
    .then((r) => r.data);

export const fetchOptions = (sym: string, expiry?: string) =>
  api.get<OptionRow[]>(`/us/options/${sym}${expiry ? `?expiration_date=${expiry}` : ""}`)
    .then((r) => r.data);

export const fetchOptionsAnalysis = (sym: string) =>
  api.get<OptionsAnalysisResponse>(`/us/options-analysis/${sym}?max_expiries=8`)
    .then((r) => r.data);

export const fetchInstitutional = (sym: string) =>
  api.get<InstitutionalRow[]>(`/tw/institutional/${sym}?days=60`).then((r) => r.data);

export const fetchMargin = (sym: string) =>
  api.get<MarginRow[]>(`/tw/margin/${sym}?days=60`).then((r) => r.data);

export const fetchRevenue = (sym: string) =>
  api.get<RevenueRow[]>(`/tw/revenue/${sym}?months=24`).then((r) => r.data);

export const fetchHealth = (sym: string) =>
  api.get<HealthResponse>(`/tw/health/${sym}?periods=8`).then((r) => r.data);

export const fetchValuationBand = (sym: string, metric: "pe" | "pb") =>
  api.get<ValuationBandResponse>(`/tw/valuation-band/${sym}?metric=${metric}&years=5`)
    .then((r) => r.data);

export const fetchDividends = (sym: string) =>
  api.get<DividendRow[]>(`/tw/dividends/${sym}`).then((r) => r.data);

export const fetchETFHoldings = (sym: string) =>
  api.get<ETFHoldingsResponse>(`/tw/etf/${sym}/holdings`).then((r) => r.data);

export const fetchTWSecurityMaster = (sym: string) =>
  api.get<TWSecurityMaster>(`/tw/security-master/${sym}`).then((r) => r.data);

export const fetchEarnings = (sym: string) =>
  api.get<{ earnings_date: string | null; eps_estimate: number | null; revenue_estimate: number | null }>(
    `/us/earnings/${sym}`
  ).then((r) => r.data);

// ── A2 分時 (intraday) ────────────────────────────────────────────

export type IntradayInterval = "1m" | "5m" | "15m";

/** Chart timeframe: 分時 (1m/5m/15m via the /intraday endpoint) or
 *  日/週/月 (daily history, 週/月 aggregated client-side). Lives here —
 *  not in TimeframeSelector.tsx — so component files stay
 *  components-only for react-refresh. */
export type Timeframe = IntradayInterval | "1d" | "1wk" | "1mo";

export const INTRADAY_TIMEFRAMES: readonly IntradayInterval[] = ["1m", "5m", "15m"];

export const isIntradayTimeframe = (tf: Timeframe): tf is IntradayInterval =>
  (INTRADAY_TIMEFRAMES as readonly string[]).includes(tf);

export interface IntradayResponse {
  symbol: string;
  market: string;
  interval: IntradayInterval;
  /** Snapshot retention window — the furthest back `bars` can reach.
   *  Surfaced so the chart can label the ~30-day limitation. */
  coverage_days: number;
  bars: OHLCVBar[]; // bar.time is Unix ms (bucket start)
}

export const fetchIntraday = (mkt: Market, sym: string, interval: IntradayInterval) =>
  api.get<IntradayResponse>(
    `/${mkt.toLowerCase()}/intraday/${sym}?interval=${interval}`
  ).then((r) => r.data);
