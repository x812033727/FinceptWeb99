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
import { ResultsTable } from "@/components/screener/ResultsTable";

// ── main page ──────────────────────────────────────────────────────

export default function ScreenerPage() {
  const { t } = useTranslation();
  const [filters, setFilters] = useState<Filters>(DEFAULT_FILTERS);
  const [applied, setApplied] = useState<Filters>(DEFAULT_FILTERS);

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

  return (
    <div className="p-4 sm:p-6 flex flex-col gap-5 sm:gap-6 h-screen">
      <div>
        <h1 className="text-xl sm:text-2xl font-bold text-foreground">{t("screener.title")}</h1>
        <p className="text-xs sm:text-sm text-muted-foreground mt-1">
          {t("screener.subtitle")}
        </p>
      </div>

      <FilterBar
        filters={filters}
        setFilter={setFilter}
        applyStrategy={applyStrategy}
        applyFilters={applyFilters}
        resetFilters={resetFilters}
        isFetching={isFetching}
        resultsCount={rows.length}
      />

      <ResultsTable rows={rows} applied={applied} isLoading={isLoading} />
    </div>
  );
}
