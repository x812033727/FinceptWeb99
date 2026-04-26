import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import api from "@/lib/api";

// ── types ──────────────────────────────────────────────────────────

interface WatchlistItem {
  id: string;
  symbol: string;
  market: string;
  added_at: string;
  price: number | null;
  change_pct: number | null;
  name: string | null;
}

interface Watchlist {
  id: string;
  name: string;
  created_at: string;
  items: WatchlistItem[];
}

// ── API helpers ────────────────────────────────────────────────────

const fetchWatchlists = () =>
  api.get<Watchlist[]>("/watchlist").then((r) => r.data);

const createWatchlist = (name: string) =>
  api.post<Watchlist>("/watchlist", { name }).then((r) => r.data);

const deleteWatchlist = (id: string) =>
  api.delete(`/watchlist/${id}`);

const addItem = (wid: string, symbol: string, market: string) =>
  api.post<WatchlistItem>(`/watchlist/${wid}/items`, { symbol, market }).then((r) => r.data);

const removeItem = (wid: string, itemId: string) =>
  api.delete(`/watchlist/${wid}/items/${itemId}`);

// ── sub-components ─────────────────────────────────────────────────

function AddSymbolRow({ watchlistId }: { watchlistId: string }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [symbol, setSymbol] = useState("");
  const [market, setMarket] = useState("US");
  const [error, setError] = useState("");

  const add = useMutation({
    mutationFn: () => addItem(watchlistId, symbol.trim().toUpperCase(), market),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["watchlists"] });
      setSymbol("");
      setError("");
    },
    onError: () => setError(t("common.error")),
  });

  function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!symbol.trim()) return;
    add.mutate();
  }

  return (
    <form onSubmit={submit} className="flex gap-2 mt-3">
      <input
        value={symbol}
        onChange={(e) => setSymbol(e.target.value)}
        placeholder={`${t("watchlist.add_symbol")} (AAPL, 2330)`}
        className="flex-1 bg-background border border-border rounded px-3 py-1.5 text-sm text-foreground placeholder:text-muted-foreground/50 focus:outline-none focus:border-primary/50"
      />
      <select
        value={market}
        onChange={(e) => setMarket(e.target.value)}
        className="bg-background border border-border rounded px-2 py-1.5 text-sm text-foreground focus:outline-none"
      >
        <option value="US">US</option>
        <option value="TW">TW</option>
        <option value="CRYPTO">CRYPTO</option>
      </select>
      <button
        type="submit"
        disabled={add.isPending}
        className="px-3 py-1.5 bg-primary text-primary-foreground text-sm rounded hover:opacity-90 disabled:opacity-50"
      >
        {add.isPending ? "…" : `+ ${t("common.add")}`}
      </button>
      {error && <span className="text-xs text-red-400 self-center">{error}</span>}
    </form>
  );
}

function WatchlistCard({ wl }: { wl: Watchlist }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [expanded, setExpanded] = useState(true);

  const del = useMutation({
    mutationFn: () => deleteWatchlist(wl.id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["watchlists"] }),
  });

  const removeIt = useMutation({
    mutationFn: (itemId: string) => removeItem(wl.id, itemId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["watchlists"] }),
  });

  return (
    <div className="bg-card border border-border rounded-lg overflow-hidden">
      {/* header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-border">
        <button
          onClick={() => setExpanded((e) => !e)}
          className="flex items-center gap-2 text-sm font-medium text-foreground hover:text-primary transition-colors"
        >
          <span className={`text-xs transition-transform ${expanded ? "rotate-90" : ""}`}>▶</span>
          {wl.name}
          <span className="text-xs text-muted-foreground font-normal">({wl.items.length})</span>
        </button>
        <button
          onClick={() => { if (confirm(`${t("common.delete")} "${wl.name}"?`)) del.mutate(); }}
          className="text-xs text-muted-foreground hover:text-red-400 transition-colors"
        >
          {t("common.delete")}
        </button>
      </div>

      {expanded && (
        <div>
          {wl.items.length === 0 ? (
            <div className="px-4 py-3 text-xs text-muted-foreground">{t("common.no_data")}</div>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="text-xs text-muted-foreground border-b border-border">
                  <th className="text-left px-3 sm:px-4 py-2 font-medium">{t("market.table.symbol")}</th>
                  <th className="hidden sm:table-cell text-left px-2 py-2 font-medium">{t("market.table.name")}</th>
                  <th className="text-right px-3 sm:px-4 py-2 font-medium">{t("market.table.price")}</th>
                  <th className="text-right px-3 sm:px-4 py-2 font-medium">{t("market.table.change")}</th>
                  <th className="px-2 py-2" />
                </tr>
              </thead>
              <tbody className="tabular-nums">
                {wl.items.map((item) => {
                  const pos = (item.change_pct ?? 0) >= 0;
                  return (
                    <tr key={item.id} className="border-b border-border/30 hover:bg-accent/5 group">
                      <td className="px-3 sm:px-4 py-2.5">
                        <Link
                          to={`/stock/${item.market}/${item.symbol}`}
                          className="font-medium text-primary hover:underline"
                        >
                          {item.symbol}
                        </Link>
                        <span className="text-xs text-muted-foreground ml-1.5">{item.market}</span>
                      </td>
                      <td className="hidden sm:table-cell px-2 py-2.5 text-muted-foreground text-xs max-w-[180px] truncate">
                        {item.name ?? "—"}
                      </td>
                      <td className="text-right px-3 sm:px-4 py-2.5 text-foreground">
                        {item.price != null
                          ? item.price.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
                          : "—"}
                      </td>
                      <td className={`text-right px-3 sm:px-4 py-2.5 text-sm font-medium ${pos ? "text-green-400" : "text-red-400"}`}>
                        {item.change_pct != null
                          ? `${pos ? "+" : ""}${item.change_pct.toFixed(2)}%`
                          : "—"}
                      </td>
                      <td className="px-2 py-2.5 text-right">
                        <button
                          onClick={() => removeIt.mutate(item.id)}
                          aria-label={t("watchlist.remove") || "Remove"}
                          className="text-base text-muted-foreground hover:text-red-400 sm:text-xs sm:opacity-0 sm:group-hover:opacity-100 transition-all"
                        >
                          ✕
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
          <div className="px-4 pb-4">
            <AddSymbolRow watchlistId={wl.id} />
          </div>
        </div>
      )}
    </div>
  );
}

// ── main page ──────────────────────────────────────────────────────

export default function WatchlistPage() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const { data: watchlists = [], isLoading } = useQuery({
    queryKey: ["watchlists"],
    queryFn: fetchWatchlists,
    staleTime: 30_000,
  });

  const [newName, setNewName] = useState("");
  const create = useMutation({
    mutationFn: () => createWatchlist(newName.trim() || "My Watchlist"),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["watchlists"] });
      setNewName("");
    },
  });

  return (
    <div className="p-4 sm:p-6 space-y-5 sm:space-y-6">
      <div className="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-3">
        <div>
          <h1 className="text-xl sm:text-2xl font-bold text-foreground">{t("watchlist.title")}</h1>
          <p className="text-xs sm:text-sm text-muted-foreground mt-1">{t("watchlist.live_prices")}</p>
        </div>
        <form
          onSubmit={(e) => { e.preventDefault(); create.mutate(); }}
          className="flex gap-2"
        >
          <input
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            placeholder={t("watchlist.list_name")}
            className="flex-1 sm:flex-none sm:w-40 bg-background border border-border rounded px-3 py-1.5 text-sm text-foreground placeholder:text-muted-foreground/50 focus:outline-none focus:border-primary/50"
          />
          <button
            type="submit"
            disabled={create.isPending}
            className="px-3 sm:px-4 py-1.5 bg-primary text-primary-foreground text-sm rounded hover:opacity-90 disabled:opacity-50 whitespace-nowrap"
          >
            + {t("watchlist.new_list")}
          </button>
        </form>
      </div>

      {isLoading ? (
        <div className="text-muted-foreground text-sm animate-pulse">{t("common.loading")}</div>
      ) : watchlists.length === 0 ? (
        <div className="bg-card border border-border rounded-lg p-8 text-center text-muted-foreground text-sm">
          {t("watchlist.no_lists")}
        </div>
      ) : (
        <div className="space-y-4">
          {watchlists.map((wl) => (
            <WatchlistCard key={wl.id} wl={wl} />
          ))}
        </div>
      )}
    </div>
  );
}
