/**
 * Screener filter bar: per-market strategy preset grid + the filter
 * input panel (apply / reset / result count). Extracted verbatim from
 * `pages/ScreenerPage.tsx` (PR-8 巨石頁拆分) — all filter state stays
 * in the page; this component is purely presentational.
 */
import { useTranslation } from "react-i18next";
import type { Market } from "@/types/market";
import type { ETFMode, Filters, Strategy } from "@/components/screener/_shared";
import { TW_STRATEGIES, US_STRATEGIES } from "@/components/screener/_shared";

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

const US_SECTORS = [
  "", "Technology", "Healthcare", "Financials", "Consumer Cyclical",
  "Communication Services", "Industrials", "Consumer Defensive",
  "Energy", "Utilities", "Real Estate", "Basic Materials",
];

export function FilterBar({
  filters,
  setFilter,
  applyStrategy,
  applyFilters,
  resetFilters,
  isFetching,
  resultsCount,
}: {
  filters: Filters;
  setFilter: <K extends keyof Filters>(key: K, value: Filters[K]) => void;
  applyStrategy: (s: Strategy) => void;
  applyFilters: () => void;
  resetFilters: () => void;
  isFetching: boolean;
  resultsCount: number;
}) {
  const { t } = useTranslation();
  return (
    <>
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
            {t("screener.results_count", { count: resultsCount })}
          </span>
        </div>
      </div>
    </>
  );
}
