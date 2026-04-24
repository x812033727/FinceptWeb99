import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import { useRef } from "react";
import api from "@/lib/api";
import type { ScreenerResult, Market } from "@/types/market";

// ── filters ────────────────────────────────────────────────────────

interface Filters {
  market: Market;
  minMarketCap: string;
  maxPE: string;
  minVolume: string;
  sector: string;
  minChangePct: string;
  maxChangePct: string;
  search: string;
}

const DEFAULT_FILTERS: Filters = {
  market: "US",
  minMarketCap: "",
  maxPE: "",
  minVolume: "",
  sector: "",
  minChangePct: "",
  maxChangePct: "",
  search: "",
};

// ── API helpers ────────────────────────────────────────────────────

async function fetchUSScreener(params: {
  min_market_cap?: number;
  max_pe?: number;
  min_volume?: number;
  sector?: string;
}): Promise<ScreenerResult[]> {
  const q = new URLSearchParams({ limit: "500" });
  if (params.min_market_cap) q.set("min_market_cap", String(params.min_market_cap));
  if (params.max_pe) q.set("max_pe", String(params.max_pe));
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
  volume: number;
}

async function fetchTWScreener(): Promise<ScreenerResult[]> {
  const res = await api.get<TWScreenerItem[]>("/tw/screener?limit=500");
  return res.data.map((item) => ({
    symbol: item.symbol,
    market: "TW" as const,
    name: item.name_zh,
    price: item.price ?? 0,
    change_pct: 0,
    volume: item.volume,
  }));
}

// ── sub-components ─────────────────────────────────────────────────

function FilterInput({
  label, value, onChange, placeholder,
}: {
  label: string; value: string; onChange: (v: string) => void; placeholder?: string;
}) {
  return (
    <div className="flex flex-col gap-1">
      <label className="text-xs text-muted-foreground">{label}</label>
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="bg-background border border-border rounded px-2.5 py-1.5 text-sm text-foreground placeholder:text-muted-foreground/50 focus:outline-none focus:border-primary/50 w-full"
      />
    </div>
  );
}

// ── main page ──────────────────────────────────────────────────────

const US_SECTORS = [
  "", "Technology", "Healthcare", "Financials", "Consumer Cyclical",
  "Communication Services", "Industrials", "Consumer Defensive",
  "Energy", "Utilities", "Real Estate", "Basic Materials",
];

export default function ScreenerPage() {
  const navigate = useNavigate();
  const [filters, setFilters] = useState<Filters>(DEFAULT_FILTERS);
  const [applied, setApplied] = useState<Filters>(DEFAULT_FILTERS);
  const parentRef = useRef<HTMLDivElement>(null);

  function setFilter(key: keyof Filters, value: string) {
    setFilters((prev) => ({ ...prev, [key]: value }));
  }

  function applyFilters() {
    setApplied({ ...filters });
  }

  function resetFilters() {
    setFilters(DEFAULT_FILTERS);
    setApplied(DEFAULT_FILTERS);
  }

  const { data: raw = [], isLoading, isFetching } = useQuery({
    queryKey: ["screener-page", applied.market, applied.minMarketCap, applied.maxPE, applied.minVolume, applied.sector],
    queryFn: () => {
      if (applied.market === "TW") return fetchTWScreener();
      return fetchUSScreener({
        min_market_cap: applied.minMarketCap ? Number(applied.minMarketCap) * 1e9 : undefined,
        max_pe: applied.maxPE ? Number(applied.maxPE) : undefined,
        min_volume: applied.minVolume ? Number(applied.minVolume) : undefined,
        sector: applied.sector || undefined,
      });
    },
    staleTime: 120_000,
  });

  // Client-side filtering for search and change_pct range
  const rows = raw.filter((r) => {
    const q = applied.search.toLowerCase();
    if (q && !r.symbol.toLowerCase().includes(q) && !(r.name ?? "").toLowerCase().includes(q)) return false;
    if (applied.minChangePct && r.change_pct < Number(applied.minChangePct)) return false;
    if (applied.maxChangePct && r.change_pct > Number(applied.maxChangePct)) return false;
    return true;
  });

  // Virtual scroll
  const rowVirtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 44,
    overscan: 10,
  });

  const items = rowVirtualizer.getVirtualItems();

  return (
    <div className="p-6 flex flex-col gap-6 h-screen">
      <div>
        <h1 className="text-2xl font-bold text-foreground">Stock Screener</h1>
        <p className="text-sm text-muted-foreground mt-1">
          Filter and rank stocks across US and Taiwan markets
        </p>
      </div>

      {/* filter panel */}
      <div className="bg-card border border-border rounded-lg p-4">
        <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-8 gap-3">
          {/* market toggle */}
          <div className="flex flex-col gap-1">
            <label className="text-xs text-muted-foreground">Market</label>
            <div className="flex rounded border border-border overflow-hidden">
              {(["US", "TW"] as Market[]).map((m) => (
                <button
                  key={m}
                  onClick={() => setFilter("market", m)}
                  className={`flex-1 py-1.5 text-sm transition-colors ${
                    filters.market === m
                      ? "bg-primary text-primary-foreground"
                      : "text-muted-foreground hover:text-foreground"
                  }`}
                >
                  {m}
                </button>
              ))}
            </div>
          </div>

          <FilterInput
            label="Min Mkt Cap (B)"
            value={filters.minMarketCap}
            onChange={(v) => setFilter("minMarketCap", v)}
            placeholder="e.g. 10"
          />
          <FilterInput
            label="Max P/E"
            value={filters.maxPE}
            onChange={(v) => setFilter("maxPE", v)}
            placeholder="e.g. 30"
          />
          <FilterInput
            label="Min Volume"
            value={filters.minVolume}
            onChange={(v) => setFilter("minVolume", v)}
            placeholder="e.g. 1000000"
          />
          <FilterInput
            label="Min Chg%"
            value={filters.minChangePct}
            onChange={(v) => setFilter("minChangePct", v)}
            placeholder="e.g. -5"
          />
          <FilterInput
            label="Max Chg%"
            value={filters.maxChangePct}
            onChange={(v) => setFilter("maxChangePct", v)}
            placeholder="e.g. 10"
          />

          {/* sector (US only) */}
          <div className="flex flex-col gap-1">
            <label className="text-xs text-muted-foreground">Sector</label>
            <select
              value={filters.sector}
              onChange={(e) => setFilter("sector", e.target.value)}
              disabled={filters.market === "TW"}
              className="bg-background border border-border rounded px-2.5 py-1.5 text-sm text-foreground focus:outline-none focus:border-primary/50 disabled:opacity-40"
            >
              {US_SECTORS.map((s) => (
                <option key={s} value={s}>{s || "All Sectors"}</option>
              ))}
            </select>
          </div>

          <FilterInput
            label="Search"
            value={filters.search}
            onChange={(v) => setFilter("search", v)}
            placeholder="Symbol / name"
          />
        </div>

        <div className="flex gap-2 mt-3">
          <button
            onClick={applyFilters}
            className="px-4 py-1.5 bg-primary text-primary-foreground rounded text-sm font-medium hover:bg-primary/90 transition-colors"
          >
            {isFetching ? "Loading…" : "Apply Filters"}
          </button>
          <button
            onClick={resetFilters}
            className="px-4 py-1.5 border border-border text-muted-foreground rounded text-sm hover:text-foreground transition-colors"
          >
            Reset
          </button>
          <span className="text-xs text-muted-foreground self-center ml-2">
            {rows.length} results
          </span>
        </div>
      </div>

      {/* virtual table */}
      <div className="bg-card border border-border rounded-lg overflow-hidden flex flex-col flex-1 min-h-0">
        {/* fixed header */}
        <div className="grid grid-cols-[100px_1fr_100px_90px_100px_100px_80px_1fr] text-xs text-muted-foreground border-b border-border px-4 py-2.5 shrink-0">
          <span className="font-medium">Symbol</span>
          <span className="font-medium">Name</span>
          <span className="font-medium text-right">Price</span>
          <span className="font-medium text-right">Chg%</span>
          <span className="font-medium text-right">Volume</span>
          <span className="font-medium text-right">Mkt Cap</span>
          <span className="font-medium text-right">P/E</span>
          <span className="font-medium pl-4">Sector</span>
        </div>

        {isLoading ? (
          <div className="flex-1 flex items-center justify-center text-muted-foreground text-sm animate-pulse">
            Loading…
          </div>
        ) : (
          <div ref={parentRef} className="overflow-auto flex-1">
            <div style={{ height: rowVirtualizer.getTotalSize(), position: "relative" }}>
              {items.map((virtualRow) => {
                const row = rows[virtualRow.index];
                const pos = row.change_pct >= 0;
                return (
                  <div
                    key={virtualRow.key}
                    data-index={virtualRow.index}
                    ref={rowVirtualizer.measureElement}
                    onClick={() => navigate(`/stock/${applied.market}/${row.symbol}`)}
                    className="grid grid-cols-[100px_1fr_100px_90px_100px_100px_80px_1fr] absolute w-full px-4 items-center border-b border-border/30 hover:bg-accent/5 cursor-pointer transition-colors"
                    style={{ top: virtualRow.start, height: 44 }}
                  >
                    <span className="font-medium text-primary text-sm">{row.symbol}</span>
                    <span className="text-muted-foreground text-sm truncate">{row.name}</span>
                    <span className="text-right text-sm text-foreground">
                      {row.price.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                    </span>
                    <span className={`text-right text-sm ${pos ? "text-green-400" : "text-red-400"}`}>
                      {pos ? "+" : ""}{row.change_pct.toFixed(2)}%
                    </span>
                    <span className="text-right text-sm text-muted-foreground">
                      {row.volume >= 1e6 ? `${(row.volume / 1e6).toFixed(1)}M` : row.volume >= 1e3 ? `${(row.volume / 1e3).toFixed(0)}K` : row.volume}
                    </span>
                    <span className="text-right text-sm text-muted-foreground">
                      {row.market_cap
                        ? row.market_cap >= 1e12
                          ? `$${(row.market_cap / 1e12).toFixed(2)}T`
                          : `$${(row.market_cap / 1e9).toFixed(1)}B`
                        : "—"}
                    </span>
                    <span className="text-right text-sm text-muted-foreground">
                      {row.pe_ratio ? row.pe_ratio.toFixed(1) : "—"}
                    </span>
                    <span className="text-sm text-muted-foreground pl-4 truncate">{row.sector ?? "—"}</span>
                  </div>
                );
              })}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
