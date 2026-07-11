/**
 * Shared screener types / strategy presets / API helpers.
 * Extracted verbatim from `pages/ScreenerPage.tsx` (PR-8 巨石頁拆分)
 * so `FilterBar` / `ResultsTable` and the page can share them.
 */
import api from "@/lib/api";
import type { ScreenerResult, Market } from "@/types/market";

// ── filters ────────────────────────────────────────────────────────

export type ETFMode = "all" | "exclude" | "only";

export interface Filters {
  market: Market;
  minMarketCap: string;
  minPE: string;
  maxPE: string;
  minPB: string;
  maxPB: string;
  minDivYield: string;
  minVolume: string;
  etfMode: ETFMode;
  sector: string;
  minChangePct: string;
  maxChangePct: string;
  search: string;
  strategy: string;       // id of an active TW preset, "" for none
}

export const DEFAULT_FILTERS: Filters = {
  market: "US",
  minMarketCap: "",
  minPE: "",
  maxPE: "",
  minPB: "",
  maxPB: "",
  minDivYield: "",
  minVolume: "",
  etfMode: "all",
  sector: "",
  minChangePct: "",
  maxChangePct: "",
  search: "",
  strategy: "",
};

// ── TW strategy presets ────────────────────────────────────────────
//
// Each preset is a partial-Filters delta that the screener applies on
// click. They use only filter fields the TW backend already supports
// (PE / PB / yield / volume / etfMode); strategies that need ROE or EPS
// growth are deferred until the screener exposes those metrics in bulk.

export interface Strategy {
  id: string;
  nameKey: string;
  descKey: string;
  apply: (f: Filters) => Filters;
}

const RESET_TW_FIELDS = (f: Filters): Filters => ({
  ...f,
  minPE: "", maxPE: "", minPB: "", maxPB: "",
  minDivYield: "", minVolume: "",
  etfMode: "all",
});

const RESET_US_FIELDS = (f: Filters): Filters => ({
  ...f,
  minMarketCap: "",
  minPE: "", maxPE: "", minPB: "", maxPB: "",
  minDivYield: "", minVolume: "",
  sector: "",
});

export const US_STRATEGIES: Strategy[] = [
  {
    id: "us_graham_value",
    nameKey: "screener.strategies.us_graham_value",
    descKey: "screener.strategies.us_graham_value_desc",
    apply: (f) => ({ ...RESET_US_FIELDS(f), maxPE: "15", maxPB: "1.5", minVolume: "1000000" }),
  },
  {
    id: "us_mega_cap",
    nameKey: "screener.strategies.us_mega_cap",
    descKey: "screener.strategies.us_mega_cap_desc",
    apply: (f) => ({ ...RESET_US_FIELDS(f), minMarketCap: "200" }),
  },
  {
    id: "us_dividend_stalwart",
    nameKey: "screener.strategies.us_dividend_stalwart",
    descKey: "screener.strategies.us_dividend_stalwart_desc",
    apply: (f) => ({ ...RESET_US_FIELDS(f), minDivYield: "3", minMarketCap: "20" }),
  },
  {
    id: "us_tech_giants",
    nameKey: "screener.strategies.us_tech_giants",
    descKey: "screener.strategies.us_tech_giants_desc",
    apply: (f) => ({ ...RESET_US_FIELDS(f), sector: "Technology", minMarketCap: "100" }),
  },
  {
    id: "us_high_volume",
    nameKey: "screener.strategies.us_high_volume",
    descKey: "screener.strategies.us_high_volume_desc",
    apply: (f) => ({ ...RESET_US_FIELDS(f), minVolume: "20000000" }),
  },
  {
    id: "us_quality_fair_price",
    nameKey: "screener.strategies.us_quality_fair_price",
    descKey: "screener.strategies.us_quality_fair_price_desc",
    apply: (f) => ({ ...RESET_US_FIELDS(f), maxPE: "25", maxPB: "4", minMarketCap: "10" }),
  },
];

export const TW_STRATEGIES: Strategy[] = [
  {
    id: "high_yield",
    nameKey: "screener.strategies.high_yield",
    descKey: "screener.strategies.high_yield_desc",
    apply: (f) => ({ ...RESET_TW_FIELDS(f), minDivYield: "5", maxPE: "15", etfMode: "exclude" }),
  },
  {
    id: "deep_value",
    nameKey: "screener.strategies.deep_value",
    descKey: "screener.strategies.deep_value_desc",
    apply: (f) => ({ ...RESET_TW_FIELDS(f), maxPE: "8", maxPB: "1", etfMode: "exclude" }),
  },
  {
    id: "quality_value",
    nameKey: "screener.strategies.quality_value",
    descKey: "screener.strategies.quality_value_desc",
    apply: (f) => ({ ...RESET_TW_FIELDS(f), maxPE: "15", minDivYield: "2.5", etfMode: "exclude" }),
  },
  {
    id: "yield_etf",
    nameKey: "screener.strategies.yield_etf",
    descKey: "screener.strategies.yield_etf_desc",
    apply: (f) => ({ ...RESET_TW_FIELDS(f), minDivYield: "3", etfMode: "only" }),
  },
  {
    id: "liquid_blue_chip",
    nameKey: "screener.strategies.liquid_blue_chip",
    descKey: "screener.strategies.liquid_blue_chip_desc",
    apply: (f) => ({ ...RESET_TW_FIELDS(f), minVolume: "10000000", etfMode: "exclude" }),
  },
  {
    id: "neglected_value",
    nameKey: "screener.strategies.neglected_value",
    descKey: "screener.strategies.neglected_value_desc",
    apply: (f) => ({ ...RESET_TW_FIELDS(f), maxPE: "10", minDivYield: "4", etfMode: "exclude" }),
  },
];

// ── API helpers ────────────────────────────────────────────────────

export async function fetchUSScreener(params: {
  min_market_cap?: number;
  min_pe?: number;
  max_pe?: number;
  min_pb?: number;
  max_pb?: number;
  min_dividend_yield?: number;
  min_volume?: number;
  sector?: string;
}): Promise<ScreenerResult[]> {
  const q = new URLSearchParams({ limit: "500" });
  if (params.min_market_cap) q.set("min_market_cap", String(params.min_market_cap));
  if (params.min_pe) q.set("min_pe", String(params.min_pe));
  if (params.max_pe) q.set("max_pe", String(params.max_pe));
  if (params.min_pb) q.set("min_pb", String(params.min_pb));
  if (params.max_pb) q.set("max_pb", String(params.max_pb));
  if (params.min_dividend_yield) q.set("min_dividend_yield", String(params.min_dividend_yield));
  if (params.min_volume) q.set("min_volume", String(params.min_volume));
  if (params.sector) q.set("sector", params.sector);
  const res = await api.get<ScreenerResult[]>(`/us/screener?${q}`);
  return res.data;
}

interface TWScreenerItem {
  symbol: string;
  market: string;
  exchange: string;
  name_zh: string;
  price: number | null;
  change: number | null;
  change_pct: number | null;
  volume: number;
  pe_ratio: number | null;
  pb_ratio: number | null;
  dividend_yield: number | null;
}

export async function fetchTWScreener(params: {
  min_pe?: number;
  max_pe?: number;
  min_pb?: number;
  max_pb?: number;
  min_dividend_yield?: number;
  min_volume?: number;
  etfMode?: ETFMode;
}): Promise<ScreenerResult[]> {
  const q = new URLSearchParams({ limit: "500" });
  if (params.min_pe) q.set("min_pe", String(params.min_pe));
  if (params.max_pe) q.set("max_pe", String(params.max_pe));
  if (params.min_pb) q.set("min_pb", String(params.min_pb));
  if (params.max_pb) q.set("max_pb", String(params.max_pb));
  if (params.min_dividend_yield) q.set("min_dividend_yield", String(params.min_dividend_yield));
  if (params.min_volume) q.set("min_volume", String(params.min_volume));
  if (params.etfMode === "exclude") q.set("include_etf", "false");
  if (params.etfMode === "only") q.set("etf_only", "true");
  const res = await api.get<TWScreenerItem[]>(`/tw/screener?${q}`);
  return res.data.map((item) => ({
    symbol: item.symbol,
    market: "TW" as const,
    name: item.name_zh,
    price: item.price ?? 0,
    change_pct: item.change_pct ?? 0,
    volume: item.volume,
    pe_ratio: item.pe_ratio ?? undefined,
    pb_ratio: item.pb_ratio ?? undefined,
    dividend_yield: item.dividend_yield ?? undefined,
    exchange: item.exchange,
  }));
}
