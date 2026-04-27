import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import { useRef } from "react";
import { useTranslation } from "react-i18next";
import api from "@/lib/api";
import type { ScreenerResult, Market } from "@/types/market";

// ── filters ────────────────────────────────────────────────────────

type ETFMode = "all" | "exclude" | "only";

interface Filters {
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

const DEFAULT_FILTERS: Filters = {
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

interface Strategy {
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

const US_STRATEGIES: Strategy[] = [
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

const TW_STRATEGIES: Strategy[] = [
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

async function fetchUSScreener(params: {
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

async function fetchTWScreener(params: {
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

  function setFilter<K extends keyof Filters>(key: K, value: Filters[K]) {
    setFilters((prev) => ({ ...prev, [key]: value, strategy: "" }));
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
      applied.minVolume, applied.sector, applied.etfMode,
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
          etfMode: applied.etfMode,
        });
      }
      return fetchUSScreener({
        min_market_cap: applied.minMarketCap ? Number(applied.minMarketCap) * 1e9 : undefined,
        min_pe: applied.minPE ? Number(applied.minPE) : undefined,
        max_pe: applied.maxPE ? Number(applied.maxPE) : undefined,
        min_pb: applied.minPB ? Number(applied.minPB) : undefined,
        max_pb: applied.maxPB ? Number(applied.maxPB) : undefined,
        min_dividend_yield: applied.minDivYield ? Number(applied.minDivYield) : undefined,
        min_volume: applied.minVolume ? Number(applied.minVolume) : undefined,
        sector: applied.sector || undefined,
      });
    },
    staleTime: 120_000,
  });

  function applyStrategy(s: Strategy) {
    const next = { ...s.apply(filters), strategy: s.id };
    setFilters(next);
    setApplied(next);
  }

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
    <div className="p-4 sm:p-6 flex flex-col gap-5 sm:gap-6 h-screen">
      <div>
        <h1 className="text-xl sm:text-2xl font-bold text-foreground">{t("screener.title")}</h1>
        <p className="text-xs sm:text-sm text-muted-foreground mt-1">
          {t("screener.subtitle")}
        </p>
      </div>

      {/* strategy presets — per market */}
      <div className="space-y-2">
        <div className="flex items-baseline justify-between">
          <h3 className="text-sm font-semibold text-foreground">{t("screener.strategies.title")}</h3>
          <p className="text-xs text-muted-foreground">{t("screener.strategies.subtitle")}</p>
        </div>
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-2">
          {(filters.market === "TW" ? TW_STRATEGIES : US_STRATEGIES).map((s) => {
            const active = filters.strategy === s.id;
            return (
              <button
                key={s.id}
                onClick={() => applyStrategy(s)}
                className={`text-left p-3 rounded-lg border transition-colors ${
                  active
                    ? "bg-primary/10 border-primary/60"
                    : "bg-card border-border hover:border-primary/40"
                }`}
              >
                <div className={`text-sm font-medium ${active ? "text-primary" : "text-foreground"}`}>
                  {t(s.nameKey)}
                </div>
                <div className="text-[11px] text-muted-foreground mt-1 leading-snug line-clamp-2">
                  {t(s.descKey)}
                </div>
              </button>
            );
          })}
        </div>
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

          {filters.market === "US" && (
            <FilterInput
              label={t("screener.min_market_cap")}
              value={filters.minMarketCap}
              onChange={(v) => setFilter("minMarketCap", v)}
              placeholder="10"
            />
          )}
          <FilterInput
            label={t("screener.max_pe")}
            value={filters.maxPE}
            onChange={(v) => setFilter("maxPE", v)}
            placeholder="30"
          />
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
          {filters.market === "TW" && (
            <div className="flex flex-col gap-1">
              <label className="text-xs text-muted-foreground">ETF</label>
              <select
                value={filters.etfMode}
                onChange={(e) => setFilter("etfMode", e.target.value as ETFMode)}
                className="bg-background border border-border rounded px-2.5 py-1.5 text-sm text-foreground focus:outline-none focus:border-primary/50"
              >
                <option value="all">{t("screener.etf_all")}</option>
                <option value="exclude">{t("screener.etf_exclude")}</option>
                <option value="only">{t("screener.etf_only")}</option>
              </select>
            </div>
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
      <div className="bg-card border border-border rounded-lg overflow-x-auto overflow-y-hidden flex flex-col flex-1 min-h-0">
        {/* fixed header */}
        <div className="grid grid-cols-[100px_1fr_100px_90px_100px_100px_80px_1fr] min-w-[680px] text-xs text-muted-foreground border-b border-border px-4 py-2.5 shrink-0">
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
          <div ref={parentRef} className="overflow-y-auto flex-1 min-w-[680px]">
            <div style={{ height: rowVirtualizer.getTotalSize(), position: "relative" }}>
              {items.map((virtualRow) => {
                const row = rows[virtualRow.index];
                const pos = row.change_pct >= 0;
                const isTW = applied.market === "TW";
                const unavailable = row.data_source === "unavailable";
                return (
                  <div
                    key={virtualRow.key}
                    data-index={virtualRow.index}
                    ref={rowVirtualizer.measureElement}
                    onClick={() => navigate(`/stock/${applied.market}/${row.symbol}`)}
                    className="grid grid-cols-[100px_1fr_100px_90px_100px_100px_80px_1fr] min-w-[680px] absolute w-full px-4 items-center border-b border-border/30 hover:bg-accent/5 cursor-pointer transition-colors"
                    style={{ top: virtualRow.start, height: 44 }}
                  >
                    <span className="font-medium text-primary text-sm">{row.symbol}</span>
                    <span className="text-muted-foreground text-sm truncate">{row.name}</span>
                    <span className="text-right text-sm text-foreground">
                      {unavailable
                        ? <span className="text-muted-foreground">—</span>
                        : row.price.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                    </span>
                    <span className={`text-right text-sm ${unavailable ? "text-muted-foreground" : pos ? "text-green-400" : "text-red-400"}`}>
                      {unavailable ? "—" : `${pos ? "+" : ""}${row.change_pct.toFixed(2)}%`}
                    </span>
                    <span className="text-right text-sm text-muted-foreground">
                      {unavailable
                        ? "—"
                        : row.volume >= 1e6 ? `${(row.volume / 1e6).toFixed(1)}M` : row.volume >= 1e3 ? `${(row.volume / 1e3).toFixed(0)}K` : row.volume}
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
