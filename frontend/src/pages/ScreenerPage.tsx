import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import { useRef } from "react";
import { useTranslation } from "react-i18next";
import api from "@/lib/api";
import type { ScreenerResult, Market } from "@/types/market";

// ── filters ────────────────────────────────────────────────────────

interface Filters {
  market: Market;
  minMarketCap: string;
  minPE: string;
  maxPE: string;
  minPB: string;
  maxPB: string;
  minDivYield: string;
  minVolume: string;
  sector: string;
  minChangePct: string;
  maxChangePct: string;
  search: string;
}

const DEFAULT_FILTERS: Filters = {
  market: "US",
  minMarketCap: "",
  minPE: "",
  maxPE: "",
  minPB: "",
  maxPB: "",
  minDivYield: "",
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
  change: number | null;
  change_pct: number | null;
  volume: number;
  pe_ratio: number | null;
  pb_ratio: number | null;
  dividend_yield: number | null;
}

async function fetchTWScreener(params: {
  min_pe?: number;
  max_pe?: number;
  min_pb?: number;
  max_pb?: number;
  min_dividend_yield?: number;
  min_volume?: number;
}): Promise<ScreenerResult[]> {
  const q = new URLSearchParams({ limit: "500" });
  if (params.min_pe) q.set("min_pe", String(params.min_pe));
  if (params.max_pe) q.set("max_pe", String(params.max_pe));
  if (params.min_pb) q.set("min_pb", String(params.min_pb));
  if (params.max_pb) q.set("max_pb", String(params.max_pb));
  if (params.min_dividend_yield) q.set("min_dividend_yield", String(params.min_dividend_yield));
  if (params.min_volume) q.set("min_volume", String(params.min_volume));
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
  const { t } = useTranslation();
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
    queryKey: [
      "screener-page", applied.market,
      applied.minMarketCap, applied.minPE, applied.maxPE,
      applied.minPB, applied.maxPB, applied.minDivYield,
      applied.minVolume, applied.sector,
    ],
    queryFn: () => {
      if (applied.market === "TW") {
        return fetchTWScreener({
          min_pe: applied.minPE ? Number(applied.minPE) : undefined,
          max_pe: applied.maxPE ? Number(applied.maxPE) : undefined,
          min_pb: applied.minPB ? Number(applied.minPB) : undefined,
          max_pb: applied.maxPB ? Number(applied.maxPB) : undefined,
          min_dividend_yield: applied.minDivYield ? Number(applied.minDivYield) : undefined,
          min_volume: applied.minVolume ? Number(applied.minVolume) : undefined,
        });
      }
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
        <h1 className="text-2xl font-bold text-foreground">{t("screener.title")}</h1>
        <p className="text-sm text-muted-foreground mt-1">
          {t("screener.subtitle")}
        </p>
      </div>

      {/* filter panel */}
      <div className="bg-card border border-border rounded-lg p-4">
        <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-8 gap-3">
          {/* market toggle */}
          <div className="flex flex-col gap-1">
            <label className="text-xs text-muted-foreground">{t("alerts.market")}</label>
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

          {filters.market === "US" ? (
            <FilterInput
              label={t("screener.min_market_cap")}
              value={filters.minMarketCap}
              onChange={(v) => setFilter("minMarketCap", v)}
              placeholder="10"
            />
          ) : (
            <FilterInput
              label={t("screener.min_pe")}
              value={filters.minPE}
              onChange={(v) => setFilter("minPE", v)}
              placeholder="5"
            />
          )}
          <FilterInput
            label={t("screener.max_pe")}
            value={filters.maxPE}
            onChange={(v) => setFilter("maxPE", v)}
            placeholder="30"
          />
          {filters.market === "TW" && (
            <>
              <FilterInput
                label={t("screener.max_pb")}
                value={filters.maxPB}
                onChange={(v) => setFilter("maxPB", v)}
                placeholder="3"
              />
              <FilterInput
                label={t("screener.min_dividend_yield")}
                value={filters.minDivYield}
                onChange={(v) => setFilter("minDivYield", v)}
                placeholder="3"
              />
            </>
          )}
          <FilterInput
            label={t("market.table.volume")}
            value={filters.minVolume}
            onChange={(v) => setFilter("minVolume", v)}
            placeholder="1000000"
          />
          <FilterInput
            label={`${t("market.table.change")} ≥`}
            value={filters.minChangePct}
            onChange={(v) => setFilter("minChangePct", v)}
            placeholder="-5"
          />
          <FilterInput
            label={`${t("market.table.change")} ≤`}
            value={filters.maxChangePct}
            onChange={(v) => setFilter("maxChangePct", v)}
            placeholder="10"
          />

          {filters.market === "US" && (
            <div className="flex flex-col gap-1">
              <label className="text-xs text-muted-foreground">{t("market.table.sector")}</label>
              <select
                value={filters.sector}
                onChange={(e) => setFilter("sector", e.target.value)}
                className="bg-background border border-border rounded px-2.5 py-1.5 text-sm text-foreground focus:outline-none focus:border-primary/50"
              >
                {US_SECTORS.map((s) => (
                  <option key={s} value={s}>{s || t("screener.all_sectors")}</option>
                ))}
              </select>
            </div>
          )}

          <FilterInput
            label={t("common.search")}
            value={filters.search}
            onChange={(v) => setFilter("search", v)}
            placeholder=""
          />
        </div>

        <div className="flex gap-2 mt-3">
          <button
            onClick={applyFilters}
            className="px-4 py-1.5 bg-primary text-primary-foreground rounded text-sm font-medium hover:bg-primary/90 transition-colors"
          >
            {isFetching ? t("common.loading") : t("screener.apply")}
          </button>
          <button
            onClick={resetFilters}
            className="px-4 py-1.5 border border-border text-muted-foreground rounded text-sm hover:text-foreground transition-colors"
          >
            {t("screener.reset")}
          </button>
          <span className="text-xs text-muted-foreground self-center ml-2">
            {t("screener.results_count", { count: rows.length })}
          </span>
        </div>
      </div>

      {/* virtual table */}
      <div className="bg-card border border-border rounded-lg overflow-hidden flex flex-col flex-1 min-h-0">
        {/* fixed header */}
        <div className="grid grid-cols-[100px_1fr_100px_90px_100px_100px_80px_1fr] text-xs text-muted-foreground border-b border-border px-4 py-2.5 shrink-0">
          <span className="font-medium">{t("market.table.symbol")}</span>
          <span className="font-medium">{t("market.table.name")}</span>
          <span className="font-medium text-right">{t("market.table.price")}</span>
          <span className="font-medium text-right">{t("market.table.change")}</span>
          <span className="font-medium text-right">{t("market.table.volume")}</span>
          <span className="font-medium text-right">
            {applied.market === "TW" ? t("market.table.pb") : t("market.table.market_cap")}
          </span>
          <span className="font-medium text-right">{t("market.table.pe")}</span>
          <span className="font-medium pl-4 text-right">
            {applied.market === "TW" ? t("market.table.dividend_yield") : t("market.table.sector")}
          </span>
        </div>

        {isLoading ? (
          <div className="flex-1 flex items-center justify-center text-muted-foreground text-sm animate-pulse">
            {t("common.loading")}
          </div>
        ) : (
          <div ref={parentRef} className="overflow-auto flex-1">
            <div style={{ height: rowVirtualizer.getTotalSize(), position: "relative" }}>
              {items.map((virtualRow) => {
                const row = rows[virtualRow.index];
                const pos = row.change_pct >= 0;
                const isTW = applied.market === "TW";
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
                      {isTW
                        ? (row.pb_ratio != null ? row.pb_ratio.toFixed(2) : "—")
                        : (row.market_cap
                            ? row.market_cap >= 1e12
                              ? `$${(row.market_cap / 1e12).toFixed(2)}T`
                              : `$${(row.market_cap / 1e9).toFixed(1)}B`
                            : "—")}
                    </span>
                    <span className="text-right text-sm text-muted-foreground">
                      {row.pe_ratio ? row.pe_ratio.toFixed(1) : "—"}
                    </span>
                    <span className="text-sm text-muted-foreground pl-4 truncate text-right">
                      {isTW
                        ? (row.dividend_yield != null ? `${row.dividend_yield.toFixed(2)}%` : "—")
                        : (row.sector ?? "—")}
                    </span>
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
