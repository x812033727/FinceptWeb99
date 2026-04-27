/**
 * Global symbol search — debounced fan-out across US / TW / Crypto
 * with keyboard navigation. Results route to `/stock/<MARKET>/<SYMBOL>`.
 *
 * Hover (or focus, for keyboard users) prefetches the StockDetailPage
 * chunk so the click is instant — the heaviest lazy chunk in the app
 * (~62 kB gzip) usually finishes loading while the user is reading
 * the first result.
 */
import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { prefetchPage } from "@/pageLoaders";
import api from "@/lib/api";

export type SearchResult = { symbol: string; market: "US" | "TW" | "CRYPTO" };

export default function GlobalSearch() {
  const { t } = useTranslation();
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [open, setOpen] = useState(false);
  const [activeIdx, setActiveIdx] = useState(0);
  const navigate = useNavigate();
  const inputRef = useRef<HTMLInputElement>(null);
  const dropdownRef = useRef<HTMLDivElement>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    if (query.trim().length === 0) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setResults([]);
      setOpen(false);
      return;
    }
    debounceRef.current = setTimeout(async () => {
      try {
        const [usRes, twRes, cryptoRes] = await Promise.allSettled([
          api.get<SearchResult[]>(`/us/search?q=${encodeURIComponent(query)}&limit=6`),
          api.get<SearchResult[]>(`/tw/search?q=${encodeURIComponent(query)}&limit=2`),
          api.get<SearchResult[]>(`/crypto/search?q=${encodeURIComponent(query)}&limit=2`),
        ]);
        const combined: SearchResult[] = [
          ...(usRes.status === "fulfilled" ? usRes.value.data : []),
          ...(twRes.status === "fulfilled" ? twRes.value.data : []),
          ...(cryptoRes.status === "fulfilled" ? cryptoRes.value.data : []),
        ];
        setResults(combined.slice(0, 10));
        setActiveIdx(0);
        setOpen(combined.length > 0);
      } catch {
        setResults([]);
        setOpen(false);
      }
    }, 300);
  }, [query]);

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (
        inputRef.current &&
        !inputRef.current.contains(e.target as Node) &&
        dropdownRef.current &&
        !dropdownRef.current.contains(e.target as Node)
      ) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  function selectResult(r: SearchResult) {
    navigate(`/stock/${r.market}/${r.symbol}`);
    setQuery("");
    setOpen(false);
    inputRef.current?.blur();
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (!open || results.length === 0) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveIdx((i) => (i + 1) % results.length);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveIdx((i) => (i - 1 + results.length) % results.length);
    } else if (e.key === "Enter") {
      e.preventDefault();
      selectResult(results[activeIdx]);
    } else if (e.key === "Escape") {
      setOpen(false);
    }
  }

  return (
    <div className="relative flex-1 max-w-xs">
      <input
        ref={inputRef}
        type="text"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        onKeyDown={handleKeyDown}
        onFocus={() => results.length > 0 && setOpen(true)}
        placeholder={t("topbar.search_placeholder")}
        className="w-full h-7 rounded bg-muted/40 border border-border px-3 text-xs placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary"
      />
      {open && results.length > 0 && (
        <div
          ref={dropdownRef}
          className="absolute top-full left-0 mt-1 w-full bg-card border border-border rounded-lg shadow-xl z-50 overflow-hidden"
        >
          {results.map((r, i) => (
            <button
              key={`${r.market}:${r.symbol}`}
              onMouseDown={(e) => { e.preventDefault(); selectResult(r); }}
              onMouseEnter={() => { setActiveIdx(i); prefetchPage(`/stock/${r.market}/${r.symbol}`); }}
              onFocus={() => prefetchPage(`/stock/${r.market}/${r.symbol}`)}
              className={`w-full flex items-center gap-2 px-3 py-2 text-xs text-left transition-colors ${
                i === activeIdx ? "bg-accent/20 text-foreground" : "hover:bg-accent/10 text-foreground"
              }`}
            >
              <span className="font-medium">{r.symbol}</span>
              <span className="ml-auto text-muted-foreground text-[10px] border border-border rounded px-1">
                {r.market}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
