import { useEffect, useRef, useState } from "react";
import api from "@/lib/api";

export type SearchResult = { symbol: string; market: "US" | "TW" | "CRYPTO" };

interface Options {
  /** ms before firing the request after the user stops typing */
  debounceMs?: number;
  /** trim and short-circuit when the trimmed query is shorter than this */
  minLength?: number;
  /** total cap on the merged result list */
  maxResults?: number;
}

// Debounced fan-out across the three market search endpoints. Originally
// inlined inside GlobalSearch; lifted out so the Cmd+K palette can reuse
// the exact same query shape and result merge order.
export function useSymbolSearch(query: string, opts: Options = {}) {
  const { debounceMs = 300, minLength = 1, maxResults = 10 } = opts;
  const [results, setResults] = useState<SearchResult[]>([]);
  const [loading, setLoading] = useState(false);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const reqIdRef = useRef(0);

  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    const trimmed = query.trim();
    if (trimmed.length < minLength) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setResults([]);
      setLoading(false);
      return;
    }
    setLoading(true);
    const myId = ++reqIdRef.current;
    debounceRef.current = setTimeout(async () => {
      const [usRes, twRes, cryptoRes] = await Promise.allSettled([
        api.get<SearchResult[]>(`/us/search?q=${encodeURIComponent(trimmed)}&limit=6`),
        api.get<SearchResult[]>(`/tw/search?q=${encodeURIComponent(trimmed)}&limit=2`),
        api.get<SearchResult[]>(`/crypto/search?q=${encodeURIComponent(trimmed)}&limit=2`),
      ]);
      // Stale-response guard — a slower earlier request must not overwrite
      // the latest one's results.
      if (reqIdRef.current !== myId) return;
      const merged: SearchResult[] = [
        ...(usRes.status === "fulfilled" ? usRes.value.data : []),
        ...(twRes.status === "fulfilled" ? twRes.value.data : []),
        ...(cryptoRes.status === "fulfilled" ? cryptoRes.value.data : []),
      ];
      setResults(merged.slice(0, maxResults));
      setLoading(false);
    }, debounceMs);

    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, [query, debounceMs, minLength, maxResults]);

  return { results, loading };
}
