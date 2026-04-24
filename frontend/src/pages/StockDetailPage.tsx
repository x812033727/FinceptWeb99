import { useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import api from "@/lib/api";
import CandlestickChart from "@/components/charts/CandlestickChart";
import { useWebSocket } from "@/hooks/useWebSocket";
import type { OHLCVBar, Market } from "@/types/market";

// ── API helpers ────────────────────────────────────────────────────

type Period = "1d" | "5d" | "1mo" | "3mo" | "1y" | "5y";
type Interval = "1m" | "5m" | "15m" | "1h" | "1d" | "1wk";

const PERIOD_INTERVAL: Record<Period, Interval> = {
  "1d": "5m",
  "5d": "15m",
  "1mo": "1h",
  "3mo": "1d",
  "1y": "1d",
  "5y": "1wk",
};

async function fetchHistory(market: Market, symbol: string, period: Period): Promise<OHLCVBar[]> {
  const interval = PERIOD_INTERVAL[period];
  const endpoint =
    market === "US"
      ? `/us/history/${symbol}?period=${period}&interval=${interval}`
      : `/tw/history/${symbol}?months=${period === "5y" ? 60 : period === "1y" ? 12 : 3}`;
  const res = await api.get<OHLCVBar[]>(endpoint);
  return res.data;
}

async function fetchFundamentals(market: Market, symbol: string) {
  if (market !== "US") return null;
  const res = await api.get<Record<string, unknown>>(`/us/fundamentals/${symbol}`);
  return res.data;
}

async function fetchQuote(market: Market, symbol: string) {
  const res = await api.get<Record<string, unknown>>(
    market === "US" ? `/us/quote/${symbol}` : `/tw/quote/${symbol}`
  );
  return res.data;
}

// ── sub-components ─────────────────────────────────────────────────

function StatRow({ label, value }: { label: string; value: string | number | null | undefined }) {
  return (
    <div className="flex justify-between items-center py-1.5 border-b border-border/50 last:border-0">
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className="text-xs text-foreground font-medium">
        {value === null || value === undefined ? "—" : value}
      </span>
    </div>
  );
}

function PeriodButton({ active, label, onClick }: { active: boolean; label: string; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className={`px-2.5 py-1 text-xs rounded transition-colors ${
        active ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:text-foreground"
      }`}
    >
      {label}
    </button>
  );
}

// ── main page ──────────────────────────────────────────────────────

export default function StockDetailPage() {
  const { market = "US", symbol = "" } = useParams<{ market: string; symbol: string }>();
  const navigate = useNavigate();
  const mkt = market.toUpperCase() as Market;
  const sym = symbol.toUpperCase();

  const [period, setPeriod] = useState<Period>("1y");
  const [livePrice, setLivePrice] = useState<number | null>(null);
  const [liveChange, setLiveChange] = useState<number | null>(null);

  // Static quote for initial load
  const { data: quote } = useQuery({
    queryKey: ["quote", mkt, sym],
    queryFn: () => fetchQuote(mkt, sym),
    staleTime: 15_000,
  });

  // OHLCV bars
  const { data: bars = [], isLoading: barsLoading } = useQuery({
    queryKey: ["history", mkt, sym, period],
    queryFn: () => fetchHistory(mkt, sym, period),
    staleTime: 60_000,
  });

  // Fundamentals
  const { data: fundamentals } = useQuery({
    queryKey: ["fundamentals", mkt, sym],
    queryFn: () => fetchFundamentals(mkt, sym),
    staleTime: 60_000 * 60,
  });

  // Live WebSocket updates
  useWebSocket(`${sym}:${mkt}`, (data: unknown) => {
    const d = data as Record<string, number>;
    if (d.price) setLivePrice(d.price);
    if (d.change_pct !== undefined) setLiveChange(d.change_pct);
  });

  const displayPrice = livePrice ?? (quote?.price as number | undefined);
  const displayChange = liveChange ?? (quote?.change_pct as number | undefined);
  const isPositive = (displayChange ?? 0) >= 0;

  function fmt(n: number | null | undefined, digits = 2) {
    if (n === null || n === undefined) return "—";
    return n.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
  }

  function fmtPct(n: number | null | undefined) {
    if (n === null || n === undefined) return "—";
    return `${n >= 0 ? "+" : ""}${(n * 100).toFixed(2)}%`;
  }

  return (
    <div className="p-6 space-y-6">
      {/* breadcrumb */}
      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        <button onClick={() => navigate(`/market/${mkt}`)} className="hover:text-foreground transition-colors">
          {mkt === "US" ? "US Market" : "台股"}
        </button>
        <span>/</span>
        <span className="text-foreground font-medium">{sym}</span>
      </div>

      {/* header */}
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-bold text-foreground">{sym}</h1>
          {quote?.name && <p className="text-sm text-muted-foreground mt-0.5">{quote.name as string}</p>}
        </div>
        <div className="text-right">
          <div className="text-3xl font-bold text-foreground">
            {displayPrice !== undefined ? fmt(displayPrice) : "—"}
          </div>
          <div className={`text-sm font-medium ${isPositive ? "text-green-400" : "text-red-400"}`}>
            {displayChange !== undefined ? fmtPct(displayChange) : "—"}
          </div>
          {quote?.currency && (
            <div className="text-xs text-muted-foreground mt-0.5">{quote.currency as string}</div>
          )}
        </div>
      </div>

      {/* chart + fundamentals */}
      <div className="grid grid-cols-1 lg:grid-cols-4 gap-6">
        {/* chart (3/4 width) */}
        <div className="lg:col-span-3 bg-card border border-border rounded-lg overflow-hidden">
          {/* period selector */}
          <div className="flex items-center gap-1 px-4 pt-3 pb-2 border-b border-border">
            {(["1d", "5d", "1mo", "3mo", "1y", "5y"] as Period[]).map((p) => (
              <PeriodButton key={p} active={p === period} label={p} onClick={() => setPeriod(p)} />
            ))}
          </div>
          <div className="p-3">
            {barsLoading ? (
              <div className="h-[360px] flex items-center justify-center text-muted-foreground text-sm animate-pulse">
                Loading chart…
              </div>
            ) : bars.length === 0 ? (
              <div className="h-[360px] flex items-center justify-center text-muted-foreground text-sm">
                No data available
              </div>
            ) : (
              <CandlestickChart bars={bars} height={360} />
            )}
          </div>
        </div>

        {/* fundamentals (1/4 width) */}
        <div className="bg-card border border-border rounded-lg p-4">
          <h3 className="text-sm font-semibold text-foreground mb-3">Fundamentals</h3>
          <StatRow label="Market Cap" value={quote?.market_cap ? `$${((quote.market_cap as number) / 1e9).toFixed(2)}B` : "—"} />
          <StatRow label="P/E Ratio" value={fmt(fundamentals?.pe_ratio as number | null)} />
          <StatRow label="P/B Ratio" value={fmt(fundamentals?.pb_ratio as number | null)} />
          <StatRow label="EPS" value={fmt(fundamentals?.eps as number | null)} />
          <StatRow label="Beta" value={fmt(fundamentals?.beta as number | null)} />
          <StatRow label="Dividend Yield" value={fundamentals?.dividend_yield ? fmtPct(fundamentals.dividend_yield as number) : "—"} />
          <StatRow label="52W High" value={fmt(fundamentals?.fifty_two_week_high as number | null)} />
          <StatRow label="52W Low" value={fmt(fundamentals?.fifty_two_week_low as number | null)} />
          <StatRow label="Sector" value={fundamentals?.sector as string | null} />
          {mkt === "TW" && fundamentals?.foreign_ownership_pct !== undefined && (
            <StatRow label="Foreign Own." value={fmtPct((fundamentals.foreign_ownership_pct as number) / 100)} />
          )}
          <StatRow label="Volume" value={quote?.volume ? (quote.volume as number).toLocaleString() : "—"} />
        </div>
      </div>

      {/* description */}
      {fundamentals?.description && (
        <div className="bg-card border border-border rounded-lg p-4">
          <h3 className="text-sm font-semibold text-foreground mb-2">About</h3>
          <p className="text-xs text-muted-foreground leading-relaxed line-clamp-4">
            {fundamentals.description as string}
          </p>
        </div>
      )}
    </div>
  );
}
