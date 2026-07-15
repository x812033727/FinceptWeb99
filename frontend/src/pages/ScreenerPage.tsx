import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import type { Filters, Strategy } from "@/components/screener/_shared";
import {
  DEFAULT_FILTERS,
  fetchTWScreener,
  fetchUSScreener,
} from "@/components/screener/_shared";
import { FilterBar } from "@/components/screener/FilterBar";
import type { NLScreenerResult } from "@/components/screener/NLQueryBar";
import { NLQueryBar } from "@/components/screener/NLQueryBar";
import { ResultsTable } from "@/components/screener/ResultsTable";
import { FactorRankingPanel } from "@/components/screener/FactorRankingPanel";

// ── main page ──────────────────────────────────────────────────────

export default function ScreenerPage() {
  const { t } = useTranslation();
  const [filters, setFilters] = useState<Filters>(DEFAULT_FILTERS);
  const [applied, setApplied] = useState<Filters>(DEFAULT_FILTERS);
  // 功能 B2 — natural-language screening result. When set, the table
  // shows the AI-screened rows instead of the manual-filter query.
  const [nlResult, setNlResult] = useState<NLScreenerResult | null>(null);
  const [mode, setMode] = useState<"filters" | "factors">("filters");

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
    enabled: mode === "filters",
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

  return (
    <div className="p-gutter sm:p-page flex flex-col gap-stack sm:gap-section h-screen">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-title font-bold text-foreground">{t("screener.title")}</h1>
          <p className="text-xs sm:text-sm text-muted-foreground mt-1">
            {t("screener.subtitle")}
          </p>
        </div>
        <div className="flex rounded-lg border border-border p-1">
          {(["filters", "factors"] as const).map((item) => (
            <button key={item} type="button" onClick={() => setMode(item)} className={`rounded px-3 py-1.5 text-sm ${mode === item ? "bg-primary text-primary-foreground" : "text-muted-foreground"}`}>
              {t(`screener.mode_${item}`)}
            </button>
          ))}
        </div>
      </div>

      {mode === "filters" ? (
        <>
          <NLQueryBar onResult={setNlResult} />

          <FilterBar
            filters={filters}
            setFilter={setFilter}
            applyStrategy={applyStrategy}
            applyFilters={applyFilters}
            resetFilters={resetFilters}
            isFetching={isFetching}
            resultsCount={nlResult ? nlResult.rows.length : rows.length}
          />

          <ResultsTable
            rows={nlResult ? nlResult.rows : rows}
            applied={nlResult ? { ...applied, market: nlResult.market } : applied}
            isLoading={!nlResult && isLoading}
          />
        </>
      ) : <FactorRankingPanel />}
    </div>
  );
}
