import type { Market } from "./market";

export interface Holding {
  id: string;
  symbol: string;
  market: Market;
  quantity: number;
  avg_cost: number;
  cost_currency: string;
  current_price?: number;
  current_value?: number;
  unrealized_pnl?: number;
  unrealized_pnl_pct?: number;
  weight_pct?: number;
}

export interface Portfolio {
  id: string;
  name: string;
  currency: string;
  total_value?: number;
  total_pnl?: number;
  total_pnl_pct?: number;
  holdings: Holding[];
  created_at: string;
}

export interface Transaction {
  id: string;
  symbol: string;
  market: Market;
  tx_type: "buy" | "sell" | "dividend";
  quantity: number;
  price: number;
  fx_rate: number;
  tx_date: string;
  notes?: string;
}
