/**
 * Screener results table (virtualized). Extracted verbatim from
 * `pages/ScreenerPage.tsx` (PR-8 巨石頁拆分) — the virtual-scroll ref +
 * virtualizer move with the table markup they drive; row data and the
 * applied filters stay in the page.
 */
import { useRef } from "react";
import { useNavigate } from "react-router-dom";
import { useVirtualizer } from "@tanstack/react-virtual";
import { useTranslation } from "react-i18next";
import type { ScreenerResult } from "@/types/market";
import type { Filters } from "@/components/screener/_shared";

export function ResultsTable({
  rows,
  applied,
  isLoading,
}: {
  rows: ScreenerResult[];
  applied: Filters;
  isLoading: boolean;
}) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const parentRef = useRef<HTMLDivElement>(null);

  // Virtual scroll
  const rowVirtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 44,
    overscan: 10,
  });

  const items = rowVirtualizer.getVirtualItems();

  return (
    /* Virtual table — column count grows with viewport instead of
        forcing horizontal scroll on phones. <sm renders just symbol /
        price / change%; sm adds name + volume; md+ shows the full
        eight-column grid. Hidden cells are removed from the DOM via
        `hidden md:block` so the grid auto-flow doesn't reflow them
        into a second row. */
    <div className="bg-card border border-border rounded-lg overflow-x-auto overflow-y-hidden flex flex-col flex-1 min-h-0">
      <div className="grid grid-cols-[1fr_90px_75px] sm:grid-cols-[90px_1fr_90px_80px_90px] md:grid-cols-[100px_1fr_100px_90px_100px_100px_80px_1fr] text-xs text-muted-foreground border-b border-border px-4 py-2.5 shrink-0">
        <span className="font-medium">{t("market.table.symbol")}</span>
        <span className="font-medium hidden sm:block">{t("market.table.name")}</span>
        <span className="font-medium text-right">{t("market.table.price")}</span>
        <span className="font-medium text-right">{t("market.table.change")}</span>
        <span className="font-medium text-right hidden sm:block">{t("market.table.volume")}</span>
        <span className="font-medium text-right hidden md:block">
          {applied.market === "TW" ? t("market.table.pb") : t("market.table.market_cap")}
        </span>
        <span className="font-medium text-right hidden md:block">{t("market.table.pe")}</span>
        <span className="font-medium pl-4 text-right hidden md:block">
          {applied.market === "TW" ? t("market.table.dividend_yield") : t("market.table.sector")}
        </span>
      </div>

      {isLoading ? (
        <div className="flex-1 flex items-center justify-center text-muted-foreground text-sm animate-pulse">
          {t("common.loading")}
        </div>
      ) : (
        <div ref={parentRef} className="overflow-y-auto flex-1">
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
                  className="grid grid-cols-[1fr_90px_75px] sm:grid-cols-[90px_1fr_90px_80px_90px] md:grid-cols-[100px_1fr_100px_90px_100px_100px_80px_1fr] absolute w-full px-4 items-center border-b border-border/30 hover:bg-accent/5 cursor-pointer transition-colors"
                  style={{ top: virtualRow.start, height: 44 }}
                >
                  <span className="font-medium text-primary text-sm truncate">{row.symbol}</span>
                  <span className="text-muted-foreground text-sm truncate hidden sm:block">{row.name}</span>
                  <span className="text-right text-sm text-foreground">
                    {unavailable
                      ? <span className="text-muted-foreground">—</span>
                      : row.price.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                  </span>
                  <span className={`text-right text-sm ${unavailable ? "text-muted-foreground" : pos ? "text-up" : "text-down"}`}>
                    {unavailable ? "—" : `${pos ? "+" : ""}${row.change_pct.toFixed(2)}%`}
                  </span>
                  <span className="text-right text-sm text-muted-foreground hidden sm:block">
                    {unavailable
                      ? "—"
                      : row.volume >= 1e6 ? `${(row.volume / 1e6).toFixed(1)}M` : row.volume >= 1e3 ? `${(row.volume / 1e3).toFixed(0)}K` : row.volume}
                  </span>
                  <span className="text-right text-sm text-muted-foreground hidden md:block">
                    {isTW
                      ? (row.pb_ratio != null ? row.pb_ratio.toFixed(2) : "—")
                      : (row.market_cap
                          ? row.market_cap >= 1e12
                            ? `$${(row.market_cap / 1e12).toFixed(2)}T`
                            : `$${(row.market_cap / 1e9).toFixed(1)}B`
                          : "—")}
                  </span>
                  <span className="text-right text-sm text-muted-foreground hidden md:block">
                    {row.pe_ratio ? row.pe_ratio.toFixed(1) : "—"}
                  </span>
                  <span className="text-sm text-muted-foreground pl-4 truncate text-right hidden md:block">
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
  );
}
