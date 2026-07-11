import { useRef, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import api from "@/lib/api";
import { formatPct } from "@/lib/formatters";
import { formatQuoteFreshness } from "@/lib/freshness";

// ── types ──────────────────────────────────────────────────────────

interface WatchlistItem {
  id: string;
  symbol: string;
  market: string;
  added_at: string;
  price: number | null;
  change_pct: number | null;
  name: string | null;
  quoted_at: string | null;     // ISO 8601 of when the quote was fetched
  data_source: string | null;   // "twse" | "finmind" | "db" | …
}

interface Watchlist {
  id: string;
  name: string;
  created_at: string;
  items: WatchlistItem[];
}

// ── API helpers ────────────────────────────────────────────────────

// Pass `refresh=true` so the backend bypasses its Redis quote cache
// and re-fetches each symbol from the upstream provider. Slower (5-10
// s for a TW-heavy list because of TWSE's 1 req/s rate limit) but
// gives the user fresh prices every time they open the page —
// especially important off-hours when the live polling job is idle.
const fetchWatchlists = (refresh: boolean = false) =>
  api
    .get<Watchlist[]>("/watchlist", { params: refresh ? { refresh: true } : {} })
    .then((r) => r.data);

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
      {error && <span className="text-xs text-danger self-center">{error}</span>}
    </form>
  );
}

function WatchlistFreshnessFooter({ items }: { items: WatchlistItem[] }) {
  const { t, i18n } = useTranslation();
  // Use the most recent `quoted_at` across all rows — they're
  // typically all fetched together, so picking the max gives the
  // correct "fetched at" without picking up one stale per-row
  // timestamp from a transient connector retry.
  const latestIso = items.reduce<string | null>((acc, it) => {
    if (!it.quoted_at) return acc;
    if (!acc) return it.quoted_at;
    return it.quoted_at > acc ? it.quoted_at : acc;
  }, null);
  const tsMs = latestIso ? new Date(latestIso).getTime() : null;
  const localTime = formatQuoteFreshness(tsMs, i18n.language);

  // Distinct data sources — usually 1 ("twse" market hours,
  // "finmind" off-hours, "db" during outage). Show all of them so
  // the user knows when prices are EOD / stale.
  const sources = Array.from(
    new Set(
      items
        .map((it) => it.data_source)
        .filter((s): s is string => Boolean(s)),
    ),
  );

  return (
    <div className="px-4 pt-1 pb-3 flex items-center gap-3 text-[10px] text-muted-foreground border-b border-border/30">
      <span>
        {t("watchlist.last_quoted")}：
        <span className="text-foreground/80 font-mono">
          {localTime ?? "—"}
        </span>
      </span>
      {sources.length > 0 && (
        <span>
          {t("watchlist.data_source")}：
          <span className="text-foreground/80">{sources.join(", ")}</span>
        </span>
      )}
    </div>
  );
}


// Shared grid template for the header row and virtualized item rows —
// keep the two identical or columns drift out of alignment. Name is
// hidden <sm (mirrors the old `hidden sm:table-cell` column).
const WATCHLIST_GRID_COLS =
  "grid grid-cols-[minmax(0,1fr)_95px_85px_36px] sm:grid-cols-[150px_minmax(0,1fr)_100px_90px_36px]";

/**
 * Virtualized watchlist rows (PR-9). A list can hold hundreds of
 * symbols; only the visible window is mounted (ScreenerPage pattern:
 * header row outside the scroll element so it stays pinned, absolute
 * rows inside a relative spacer). Short lists render without a
 * scrollbar — the container is max-height-capped, not fixed.
 */
function WatchlistItemRows({
  items,
  removeItem,
}: {
  items: WatchlistItem[];
  removeItem: (itemId: string) => void;
}) {
  const { t } = useTranslation();
  const parentRef = useRef<HTMLDivElement>(null);
  const rowVirtualizer = useVirtualizer({
    count: items.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 44,
    overscan: 10,
  });

  return (
    <div className="text-sm">
      <div className={`${WATCHLIST_GRID_COLS} gap-x-2 text-xs text-muted-foreground border-b border-border px-3 sm:px-4 py-2`}>
        <span className="font-medium text-left">{t("market.table.symbol")}</span>
        <span className="font-medium text-left hidden sm:block">{t("market.table.name")}</span>
        <span className="font-medium text-right">{t("market.table.price")}</span>
        <span className="font-medium text-right">{t("market.table.change")}</span>
        <span />
      </div>
      <div ref={parentRef} className="overflow-y-auto max-h-[60vh]" data-testid="watchlist-virtual-scroll">
        <div style={{ height: rowVirtualizer.getTotalSize(), position: "relative" }} className="tabular-nums">
          {rowVirtualizer.getVirtualItems().map((virtualRow) => {
            const item = items[virtualRow.index];
            const pos = (item.change_pct ?? 0) >= 0;
            return (
              <div
                key={item.id}
                data-index={virtualRow.index}
                ref={rowVirtualizer.measureElement}
                className={`${WATCHLIST_GRID_COLS} gap-x-2 absolute w-full px-3 sm:px-4 items-center border-b border-border/30 hover:bg-accent/5 group`}
                style={{ top: virtualRow.start, height: 44 }}
              >
                <span className="truncate">
                  <Link
                    to={`/stock/${item.market}/${item.symbol}`}
                    className="font-medium text-primary hover:underline"
                  >
                    {item.symbol}
                  </Link>
                  <span className="text-xs text-muted-foreground ml-1.5">{item.market}</span>
                </span>
                <span className="hidden sm:block text-muted-foreground text-xs truncate">
                  {item.name ?? "—"}
                </span>
                <span className="text-right text-foreground">
                  {item.price != null
                    ? item.price.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
                    : "—"}
                </span>
                <span className={`text-right text-sm font-medium ${pos ? "text-up" : "text-down"}`}>
                  {formatPct(item.change_pct)}
                </span>
                <span className="text-right">
                  <button
                    onClick={() => removeItem(item.id)}
                    aria-label={t("watchlist.remove") || "Remove"}
                    className="text-base text-muted-foreground hover:text-danger sm:text-xs sm:opacity-0 sm:group-hover:opacity-100 transition-all"
                  >
                    ✕
                  </button>
                </span>
              </div>
            );
          })}
        </div>
      </div>
    </div>
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
          className="text-xs text-muted-foreground hover:text-danger transition-colors"
        >
          {t("common.delete")}
        </button>
      </div>

      {expanded && (
        <div>
          {wl.items.length === 0 ? (
            <div className="px-4 py-3 text-xs text-muted-foreground">{t("common.no_data")}</div>
          ) : (
            <WatchlistItemRows
              items={wl.items}
              removeItem={(itemId) => removeIt.mutate(itemId)}
            />
          )}
          {wl.items.length > 0 && (
            <WatchlistFreshnessFooter items={wl.items} />
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
  // `refetchOnMount: "always"` + the `?refresh=true` query param means
  // every time the user navigates to 自選股, prices re-fetch from upstream.
  // staleTime stays at 30 s so in-page mutations (add/remove symbol)
  // don't trigger another round-trip when the cache is still warm.
  const { data: watchlists = [], isLoading } = useQuery({
    queryKey: ["watchlists"],
    queryFn: () => fetchWatchlists(true),
    staleTime: 30_000,
    refetchOnMount: "always",
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
