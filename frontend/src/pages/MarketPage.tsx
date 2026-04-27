import { useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import api from "@/lib/api";
import type { Market, ScreenerResult } from "@/types/market";

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
    change_pct: 0,
    volume: item.volume,
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
  volume: number;
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

function ChangeCell({ value }: { value: number | null | undefined }) {
  if (value === null || value === undefined) return <span className="text-muted-foreground">—</span>;
  const pos = value >= 0;
  return (
    <span className={pos ? "text-green-400" : "text-red-400"}>
      {pos ? "+" : ""}{value.toFixed(2)}%
    </span>
  );
}

function DataSourceBadge({ source }: { source: string | undefined }) {
  const { t } = useTranslation();
  // Only flag the rows the user actually needs to know about. polygon /
  // yfinance is the steady-state path on free + paid tiers; chip would
  // just be noise.
  if (source === "stooq") {
    return (
      <span
        title={t("market.source_badge.stooq_tooltip")}
        className="ml-1.5 text-[10px] uppercase font-medium px-1 py-px rounded bg-amber-500/10 text-amber-400 border border-amber-500/30"
      >
        {t("market.source_badge.stooq_label")}
      </span>
    );
  }
  if (source === "unavailable") {
    return (
      <span
        title={t("market.source_badge.unavailable_tooltip")}
        className="ml-1.5 text-[10px] uppercase font-medium px-1 py-px rounded bg-red-500/10 text-red-400 border border-red-500/30"
      >
        {t("market.source_badge.unavailable_label")}
      </span>
    );
  }
  return null;
}

function IndexCard({ idx }: { idx: MarketIndex }) {
  const pos = idx.change_pct >= 0;
  return (
    <div className="bg-card border border-border rounded-lg p-4">
      <div className="text-xs text-muted-foreground">{idx.name}</div>
      <div className="text-xl font-bold text-foreground mt-1">
        {idx.price.toLocaleString(undefined, { maximumFractionDigits: 2 })}
      </div>
      <div className={`text-sm font-medium ${pos ? "text-green-400" : "text-red-400"}`}>
        {pos ? "+" : ""}{idx.change_pct.toFixed(2)}%
      </div>
    </div>
  );
}

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

  return (
    <div className="p-4 sm:p-6 space-y-5 sm:space-y-6">
      {/* header */}
      <div>
        <h1 className="text-xl sm:text-2xl font-bold text-foreground">
          {mkt === "US" ? t("market.us_title")
            : mkt === "CRYPTO" ? t("market.crypto_title")
            : t("market.tw_title")}
        </h1>
        <p className="text-xs sm:text-sm text-muted-foreground mt-1">
          {mkt === "US" ? t("market.us_subtitle")
            : mkt === "CRYPTO" ? t("market.crypto_subtitle")
            : t("market.tw_subtitle")}
        </p>
      </div>

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
              />
            );
          })}
        </div>
      )}

      {/* degraded-source banner — only when at least one row is fully blocked */}
      {mkt === "US" && rows.some((r) => r.data_source === "unavailable") && (
        <div className="bg-amber-500/5 border border-amber-500/30 text-amber-300 rounded-lg px-3 py-2 text-xs sm:text-sm">
          {t("market.degraded_banner")}
        </div>
      )}

      {/* search + table */}
      <div className="bg-card border border-border rounded-lg overflow-hidden">
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
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-xs text-muted-foreground">
                  <th className="text-left px-3 sm:px-4 py-2.5 font-medium">{t("market.table.symbol")}</th>
                  <th className="text-left px-3 sm:px-4 py-2.5 font-medium">{t("market.table.name")}</th>
                  <th className="text-right px-3 sm:px-4 py-2.5 font-medium">{t("market.table.price")}</th>
                  <th
                    className="text-right px-3 sm:px-4 py-2.5 font-medium cursor-pointer select-none hover:text-foreground"
                    onClick={() => toggleSort("change_pct")}
                  >
                    {t("market.table.change")}<SortIndicator k="change_pct" sortKey={sortKey} sortDir={sortDir} />
                  </th>
                  <th
                    className="hidden sm:table-cell text-right px-3 sm:px-4 py-2.5 font-medium cursor-pointer select-none hover:text-foreground"
                    onClick={() => toggleSort("volume")}
                  >
                    {t("market.table.volume")}<SortIndicator k="volume" sortKey={sortKey} sortDir={sortDir} />
                  </th>
                  <th
                    className="hidden md:table-cell text-right px-3 sm:px-4 py-2.5 font-medium cursor-pointer select-none hover:text-foreground"
                    onClick={() => toggleSort("market_cap")}
                  >
                    {t("market.table.market_cap")}<SortIndicator k="market_cap" sortKey={sortKey} sortDir={sortDir} />
                  </th>
                  <th className="hidden lg:table-cell text-right px-3 sm:px-4 py-2.5 font-medium">{t("market.table.pe")}</th>
                  <th className="hidden lg:table-cell text-left px-3 sm:px-4 py-2.5 font-medium">{t("market.table.sector")}</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((row) => (
                  <tr
                    key={row.symbol}
                    onClick={() => navigate(`/stock/${mkt}/${row.symbol}`)}
                    className="border-b border-border/40 hover:bg-accent/5 cursor-pointer transition-colors"
                  >
                    <td className="px-3 sm:px-4 py-2.5 font-medium text-primary whitespace-nowrap">
                      {row.symbol}
                      <DataSourceBadge source={row.data_source} />
                    </td>
                    <td className="px-3 sm:px-4 py-2.5 text-muted-foreground max-w-[120px] sm:max-w-[180px] truncate">{row.name}</td>
                    <td className="px-3 sm:px-4 py-2.5 text-right text-foreground tabular-nums">
                      {row.price.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                    </td>
                    <td className="px-3 sm:px-4 py-2.5 text-right tabular-nums">
                      <ChangeCell value={row.change_pct} />
                    </td>
                    <td className="hidden sm:table-cell px-3 sm:px-4 py-2.5 text-right text-muted-foreground tabular-nums">
                      {row.volume >= 1e6
                        ? `${(row.volume / 1e6).toFixed(1)}M`
                        : row.volume >= 1e3
                        ? `${(row.volume / 1e3).toFixed(0)}K`
                        : row.volume.toLocaleString()}
                    </td>
                    <td className="hidden md:table-cell px-3 sm:px-4 py-2.5 text-right text-muted-foreground tabular-nums">
                      {row.market_cap
                        ? row.market_cap >= 1e12
                          ? `$${(row.market_cap / 1e12).toFixed(2)}T`
                          : `$${(row.market_cap / 1e9).toFixed(1)}B`
                        : "—"}
                    </td>
                    <td className="hidden lg:table-cell px-3 sm:px-4 py-2.5 text-right text-muted-foreground tabular-nums">
                      {row.pe_ratio ? row.pe_ratio.toFixed(1) : "—"}
                    </td>
                    <td className="hidden lg:table-cell px-3 sm:px-4 py-2.5 text-muted-foreground max-w-[120px] truncate">
                      {row.sector ?? "—"}
                    </td>
                  </tr>
                ))}
                {filtered.length === 0 && (
                  <tr>
                    <td colSpan={8} className="px-4 py-8 text-center text-muted-foreground text-sm">
                      {t("common.no_results")}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
