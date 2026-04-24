import { useState } from "react";
import { useParams, Link, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
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

async function fetchTWIndex(): Promise<MarketIndex | null> {
  try {
    const res = await api.get<TWIndexResponse>("/tw/indices");
    const d = res.data;
    if (!d.value) return null;
    return {
      symbol: "TAIEX",
      name: "台灣加權指數",
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

// ── main page ──────────────────────────────────────────────────────

export default function MarketPage() {
  const { market = "US" } = useParams<{ market: string }>();
  const mkt = market.toUpperCase() as Market;
  const navigate = useNavigate();
  const [search, setSearch] = useState("");
  const [sortKey, setSortKey] = useState<"change_pct" | "volume" | "market_cap">("change_pct");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");

  const { data: rows = [], isLoading } = useQuery({
    queryKey: ["screener", mkt],
    queryFn: mkt === "US" ? () => fetchUSScreener(100) : fetchTWScreener,
    staleTime: 60_000,
  });

  const { data: twIndex } = useQuery({
    queryKey: ["tw-index"],
    queryFn: fetchTWIndex,
    enabled: mkt === "TW",
    staleTime: 60_000,
  });

  function toggleSort(key: typeof sortKey) {
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

  function SortIndicator({ k }: { k: typeof sortKey }) {
    if (sortKey !== k) return <span className="text-muted-foreground/40 ml-1">↕</span>;
    return <span className="text-primary ml-1">{sortDir === "desc" ? "↓" : "↑"}</span>;
  }

  return (
    <div className="p-6 space-y-6">
      {/* header */}
      <div>
        <h1 className="text-2xl font-bold text-foreground">
          {mkt === "US" ? "US Market" : "台灣股市"}
        </h1>
        <p className="text-sm text-muted-foreground mt-1">
          {mkt === "US" ? "S&P 500 constituents" : "上市上櫃股票"}
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
            { symbol: "SPY", name: "S&P 500" },
            { symbol: "QQQ", name: "NASDAQ 100" },
            { symbol: "DIA", name: "Dow Jones" },
            { symbol: "IWM", name: "Russell 2000" },
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

      {/* search + table */}
      <div className="bg-card border border-border rounded-lg overflow-hidden">
        <div className="px-4 py-3 border-b border-border">
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={mkt === "US" ? "Search symbol or name…" : "搜尋股票代號或名稱…"}
            className="w-full max-w-sm bg-background border border-border rounded-md px-3 py-1.5 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:border-primary/50"
          />
        </div>

        {isLoading ? (
          <div className="p-8 text-center text-muted-foreground text-sm animate-pulse">Loading…</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-xs text-muted-foreground">
                  <th className="text-left px-4 py-2.5 font-medium">Symbol</th>
                  <th className="text-left px-4 py-2.5 font-medium">Name</th>
                  <th className="text-right px-4 py-2.5 font-medium">Price</th>
                  <th
                    className="text-right px-4 py-2.5 font-medium cursor-pointer select-none hover:text-foreground"
                    onClick={() => toggleSort("change_pct")}
                  >
                    Change<SortIndicator k="change_pct" />
                  </th>
                  <th
                    className="text-right px-4 py-2.5 font-medium cursor-pointer select-none hover:text-foreground"
                    onClick={() => toggleSort("volume")}
                  >
                    Volume<SortIndicator k="volume" />
                  </th>
                  <th
                    className="text-right px-4 py-2.5 font-medium cursor-pointer select-none hover:text-foreground"
                    onClick={() => toggleSort("market_cap")}
                  >
                    Mkt Cap<SortIndicator k="market_cap" />
                  </th>
                  <th className="text-right px-4 py-2.5 font-medium">P/E</th>
                  <th className="text-left px-4 py-2.5 font-medium">Sector</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((row) => (
                  <tr
                    key={row.symbol}
                    onClick={() => navigate(`/stock/${mkt}/${row.symbol}`)}
                    className="border-b border-border/40 hover:bg-accent/5 cursor-pointer transition-colors"
                  >
                    <td className="px-4 py-2.5 font-medium text-primary">{row.symbol}</td>
                    <td className="px-4 py-2.5 text-muted-foreground max-w-[180px] truncate">{row.name}</td>
                    <td className="px-4 py-2.5 text-right text-foreground">
                      {row.price.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                    </td>
                    <td className="px-4 py-2.5 text-right">
                      <ChangeCell value={row.change_pct} />
                    </td>
                    <td className="px-4 py-2.5 text-right text-muted-foreground">
                      {row.volume >= 1e6
                        ? `${(row.volume / 1e6).toFixed(1)}M`
                        : row.volume >= 1e3
                        ? `${(row.volume / 1e3).toFixed(0)}K`
                        : row.volume.toLocaleString()}
                    </td>
                    <td className="px-4 py-2.5 text-right text-muted-foreground">
                      {row.market_cap
                        ? row.market_cap >= 1e12
                          ? `$${(row.market_cap / 1e12).toFixed(2)}T`
                          : `$${(row.market_cap / 1e9).toFixed(1)}B`
                        : "—"}
                    </td>
                    <td className="px-4 py-2.5 text-right text-muted-foreground">
                      {row.pe_ratio ? row.pe_ratio.toFixed(1) : "—"}
                    </td>
                    <td className="px-4 py-2.5 text-muted-foreground max-w-[120px] truncate">
                      {row.sector ?? "—"}
                    </td>
                  </tr>
                ))}
                {filtered.length === 0 && (
                  <tr>
                    <td colSpan={8} className="px-4 py-8 text-center text-muted-foreground text-sm">
                      No results
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
