import { useRef, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import { useTranslation } from "react-i18next";
import api from "@/lib/api";
import { formatPct } from "@/lib/formatters";
import type { Market, ScreenerResult } from "@/types/market";
import { DataSourceBadge } from "@/components/DataSourceBadge";
import { PageHeader } from "@/components/ui/PageHeader";

// ── types ──────────────────────────────────────────────────────────

interface MarketIndex {
  symbol: string;
  name: string;
  price: number;
  change_pct: number;
}

// ── API helpers ────────────────────────────────────────────────────

async function fetchUSScreener(limit = 50): Promise<ScreenerResult[]> {
  const res = await api.get<ScreenerResult[]>(`/us/screener?limit=${limit}`);
  return res.data;
}

async function fetchTWScreener(): Promise<ScreenerResult[]> {
  const res = await api.get<TWScreenerItem[]>("/tw/screener?limit=200");
  return res.data.map((item) => ({
    symbol: item.symbol,
    market: "TW" as const,
    name: item.name_zh,
    price: item.price ?? 0,
    change_pct: item.change_pct ?? 0,
    volume: item.volume,
    data_source: item.data_source,
  }));
}

async function fetchCryptoScreener(): Promise<ScreenerResult[]> {
  const res = await api.get<ScreenerResult[]>("/crypto/screener?limit=20");
  return res.data;
}

interface TWIndexResponse {
  index: string;
  value: number | null;
  change: number | null;
  time: string | null;
}

interface TWScreenerItem {
  symbol: string;
  market: string;
  exchange: string;
  name_zh: string;
  price: number | null;
  change_pct?: number | null;
  volume: number;
  data_source?: string;
}

async function fetchTWIndex(twIndexLabel: string): Promise<MarketIndex | null> {
  try {
    const res = await api.get<TWIndexResponse>("/tw/indices");
    const d = res.data;
    if (!d.value) return null;
    return {
      symbol: "TAIEX",
      name: twIndexLabel,
      price: d.value,
      change_pct: d.change ? (d.change / (d.value - d.change)) * 100 : 0,
    };
  } catch {
    return null;
  }
}

// ── sub-components ─────────────────────────────────────────────────

function ChangeCell({ value, unavailable }: { value: number | null | undefined; unavailable?: boolean }) {
  if (unavailable || value === null || value === undefined) {
    return <span className="text-muted-foreground">—</span>;
  }
  const pos = value >= 0;
  return (
    <span className={pos ? "text-up" : "text-down"}>
      {formatPct(value)}
    </span>
  );
}


function IndexCard({ idx, unavailable }: { idx: MarketIndex; unavailable?: boolean }) {
  const pos = idx.change_pct >= 0;
  return (
    <div className="bg-surface-1 shadow-highlight border border-border rounded-lg p-4">
      <div className="text-label uppercase text-muted-foreground truncate">{idx.name}</div>
      <div className="text-stat font-semibold tabular-nums text-foreground mt-1">
        {unavailable
          ? <span className="text-muted-foreground">—</span>
          : idx.price.toLocaleString(undefined, { maximumFractionDigits: 2 })}
      </div>
      <div className={`text-data font-medium ${unavailable ? "text-muted-foreground" : pos ? "text-up" : "text-down"}`}>
        {unavailable ? "—" : formatPct(idx.change_pct)}
      </div>
    </div>
  );
}

// Shared grid template for the header row and virtualized data rows —
// the two must stay identical or columns drift out of alignment.
const MARKET_GRID_COLS =
  "grid grid-cols-[85px_1fr_90px_80px] sm:grid-cols-[95px_1fr_100px_90px_90px] md:grid-cols-[95px_1fr_100px_90px_90px_110px] lg:grid-cols-[95px_1fr_100px_90px_90px_110px_70px_1fr]";

// ── sort indicator ─────────────────────────────────────────────────

type SortKey = "change_pct" | "volume" | "market_cap";
type SortDir = "asc" | "desc";

function SortIndicator({ k, sortKey, sortDir }: { k: SortKey; sortKey: SortKey; sortDir: SortDir }) {
  if (sortKey !== k) return <span className="text-muted-foreground/40 ml-1">↕</span>;
  return <span className="text-primary ml-1">{sortDir === "desc" ? "↓" : "↑"}</span>;
}

// ── main page ──────────────────────────────────────────────────────

export default function MarketPage() {
  const { t } = useTranslation();
  const { market = "US" } = useParams<{ market: string }>();
  const mkt = market.toUpperCase() as Market;
  const navigate = useNavigate();
  const [search, setSearch] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("change_pct");
  const [sortDir, setSortDir] = useState<SortDir>("desc");

  const { data: rows = [], isLoading } = useQuery({
    queryKey: ["screener", mkt],
    queryFn:
      mkt === "US" ? () => fetchUSScreener(100)
      : mkt === "CRYPTO" ? fetchCryptoScreener
      : fetchTWScreener,
    staleTime: mkt === "CRYPTO" ? 30_000 : 60_000,
    gcTime: 600_000, // screener tier — keep the list warm while hopping between market tabs
  });

  const twIndexLabel = t("market.tw_index");
  const { data: twIndex } = useQuery({
    queryKey: ["tw-index"],
    queryFn: () => fetchTWIndex(twIndexLabel),
    enabled: mkt === "TW",
    staleTime: 60_000,
  });

  function toggleSort(key: SortKey) {
    if (sortKey === key) setSortDir((d) => (d === "desc" ? "asc" : "desc"));
    else { setSortKey(key); setSortDir("desc"); }
  }

  const filtered = rows
    .filter((r) => {
      const q = search.toLowerCase();
      return r.symbol.toLowerCase().includes(q) || (r.name ?? "").toLowerCase().includes(q);
    })
    .sort((a, b) => {
      const av = (a[sortKey] ?? 0) as number;
      const bv = (b[sortKey] ?? 0) as number;
      return sortDir === "desc" ? bv - av : av - bv;
    });

  // Virtual scroll (PR-9) — TW screener returns up to 200 rows; only the
  // visible window is mounted. Same pattern as ScreenerPage: header row
  // lives outside the scroll element so it stays pinned.
  const parentRef = useRef<HTMLDivElement>(null);
  const rowVirtualizer = useVirtualizer({
    count: filtered.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 44,
    overscan: 10,
  });

  return (
    <div className="p-gutter sm:p-page space-y-stack sm:space-y-section">
      <PageHeader
        title={
          mkt === "US" ? t("market.us_title")
            : mkt === "CRYPTO" ? t("market.crypto_title")
            : t("market.tw_title")
        }
        description={
          mkt === "US" ? t("market.us_subtitle")
            : mkt === "CRYPTO" ? t("market.crypto_subtitle")
            : t("market.tw_subtitle")
        }
      />

      {/* index cards */}
      {mkt === "TW" && twIndex && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
          <IndexCard idx={twIndex} />
        </div>
      )}
      {mkt === "US" && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
          {[
            { symbol: "SPY", name: t("market.indices.sp500") },
            { symbol: "QQQ", name: t("market.indices.nasdaq100") },
            { symbol: "DIA", name: t("market.indices.dow") },
            { symbol: "IWM", name: t("market.indices.russell2000") },
          ].map(({ symbol, name }) => {
            const row = rows.find((r) => r.symbol === symbol);
            if (!row) return null;
            return (
              <IndexCard
                key={symbol}
                idx={{ symbol, name, price: row.price, change_pct: row.change_pct }}
                unavailable={row.data_source === "unavailable"}
              />
            );
          })}
        </div>
      )}

      {/* degraded-source banner — fires for any market where at least one
          row is fully blocked. */}
      {rows.some((r) => r.data_source === "unavailable") && (
        <div className="bg-warning/5 border border-warning/30 text-warning rounded-lg px-3 py-2 text-xs sm:text-sm">
          {t("market.degraded_banner")}
        </div>
      )}

      {/* search + table */}
      <div className="bg-card shadow-highlight border border-border rounded-lg overflow-hidden">
        <div className="px-4 py-3 border-b border-border">
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={mkt === "US" ? t("market.us_search_placeholder") : t("market.tw_search_placeholder")}
            className="w-full max-w-sm bg-background border border-border rounded-md px-3 py-1.5 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:border-primary/50"
          />
        </div>

        {isLoading ? (
          <div className="p-8 text-center text-muted-foreground text-sm animate-pulse">{t("common.loading")}</div>
        ) : (
          <div className="text-sm">
            {/* header row — outside the scroll element so it stays pinned.
                Column count grows with the viewport (ScreenerPage pattern):
                <sm symbol/name/price/change; sm adds volume; md adds market
                cap; lg adds P/E + sector. Hidden cells are removed from the
                DOM via `hidden sm:block` so grid auto-flow stays aligned. */}
            <div className={`${MARKET_GRID_COLS} text-xs text-muted-foreground border-b border-border px-3 sm:px-4 py-2.5 gap-x-3`}>
              <span className="font-medium text-left">{t("market.table.symbol")}</span>
              <span className="font-medium text-left">{t("market.table.name")}</span>
              <span className="font-medium text-right">{t("market.table.price")}</span>
              <span
                className="font-medium text-right cursor-pointer select-none hover:text-foreground"
                onClick={() => toggleSort("change_pct")}
              >
                {t("market.table.change")}<SortIndicator k="change_pct" sortKey={sortKey} sortDir={sortDir} />
              </span>
              <span
                className="hidden sm:block font-medium text-right cursor-pointer select-none hover:text-foreground"
                onClick={() => toggleSort("volume")}
              >
                {t("market.table.volume")}<SortIndicator k="volume" sortKey={sortKey} sortDir={sortDir} />
              </span>
              <span
                className="hidden md:block font-medium text-right cursor-pointer select-none hover:text-foreground"
                onClick={() => toggleSort("market_cap")}
              >
                {t("market.table.market_cap")}<SortIndicator k="market_cap" sortKey={sortKey} sortDir={sortDir} />
              </span>
              <span className="hidden lg:block font-medium text-right">{t("market.table.pe")}</span>
              <span className="hidden lg:block font-medium text-left pl-4">{t("market.table.sector")}</span>
            </div>

            {filtered.length === 0 ? (
              <div className="px-4 py-8 text-center text-muted-foreground text-sm">
                {t("common.no_results")}
              </div>
            ) : (
              <div ref={parentRef} className="overflow-y-auto max-h-[65vh]" data-testid="market-virtual-scroll">
                <div style={{ height: rowVirtualizer.getTotalSize(), position: "relative" }}>
                  {rowVirtualizer.getVirtualItems().map((virtualRow) => {
                    const row = filtered[virtualRow.index];
                    const unavailable = row.data_source === "unavailable";
                    return (
                      <div
                        key={row.symbol}
                        data-index={virtualRow.index}
                        ref={rowVirtualizer.measureElement}
                        onClick={() => navigate(`/stock/${mkt}/${row.symbol}`)}
                        className={`${MARKET_GRID_COLS} absolute w-full px-3 sm:px-4 gap-x-3 items-center border-b border-border/40 hover:bg-accent/5 cursor-pointer transition-colors`}
                        style={{ top: virtualRow.start, height: 44 }}
                      >
                        <span className="font-medium text-primary whitespace-nowrap truncate">
                          {row.symbol}
                          <DataSourceBadge source={row.data_source} />
                        </span>
                        <span className="text-muted-foreground truncate">{row.name}</span>
                        <span className="text-right text-foreground tabular-nums">
                          {unavailable
                            ? <span className="text-muted-foreground">—</span>
                            : row.price.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                        </span>
                        <span className="text-right tabular-nums">
                          <ChangeCell value={row.change_pct} unavailable={unavailable} />
                        </span>
                        <span className="hidden sm:block text-right text-muted-foreground tabular-nums">
                          {unavailable
                            ? "—"
                            : row.volume >= 1e6
                            ? `${(row.volume / 1e6).toFixed(1)}M`
                            : row.volume >= 1e3
                            ? `${(row.volume / 1e3).toFixed(0)}K`
                            : row.volume.toLocaleString()}
                        </span>
                        <span className="hidden md:block text-right text-muted-foreground tabular-nums">
                          {row.market_cap
                            ? row.market_cap >= 1e12
                              ? `$${(row.market_cap / 1e12).toFixed(2)}T`
                              : `$${(row.market_cap / 1e9).toFixed(1)}B`
                            : "—"}
                        </span>
                        <span className="hidden lg:block text-right text-muted-foreground tabular-nums">
                          {row.pe_ratio ? row.pe_ratio.toFixed(1) : "—"}
                        </span>
                        <span className="hidden lg:block text-muted-foreground truncate pl-4">
                          {row.sector ?? "—"}
                        </span>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
