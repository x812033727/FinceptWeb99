export type Market = "US" | "TW" | "CRYPTO";

export interface OHLCVBar {
  time: string | number; // "YYYY-MM-DD" for daily; Unix ms for intraday
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface QuoteResponse {
  symbol: string;
  market: Market;
  name: string;
  price: number;
  change: number;
  change_pct: number;
  volume: number;
  market_cap?: number;
  currency: string;
  ts: string;       // ISO8601 UTC
  is_market_open: boolean;
  // "polygon" | "yfinance" | "stooq" | "stale" | "unavailable" — emitted
  // by the US backend so the UI can flag degraded / fully-blocked rows.
  data_source?: string;
}

export interface FundamentalsResponse {
  symbol: string;
  market: Market;
  pe_ratio?: number;
  pb_ratio?: number;
  eps?: number;
  market_cap?: number;
  dividend_yield?: number;
  beta?: number;
  sector?: string;
  industry?: string;
  // TW-specific
  foreign_ownership_pct?: number;
  fetched_at: string;
  // "polygon" | "yfinance" | "unavailable" — set by the US backend.
  data_source?: string;
}

export interface ScreenerResult {
  symbol: string;
  market: Market;
  name: string;
  price: number;
  change_pct: number;
  volume: number;
  market_cap?: number;
  pe_ratio?: number;
  pb_ratio?: number;
  dividend_yield?: number;
  exchange?: string;
  sector?: string;
  // "polygon" | "yfinance" | "stooq" | "unavailable" — set by the US
  // backend to mark which upstream served the row. "unavailable" rows
  // are universe placeholders (price=0) when every provider is blocked.
  data_source?: string;
}
