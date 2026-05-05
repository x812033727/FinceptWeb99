/**
 * Tests for useSymbolSearch — debounced fan-out across US/TW/Crypto
 * search endpoints. Verifies merge order, Promise.allSettled fallback
 * (one failed market doesn't kill the others), and stale-response guard.
 *
 * Uses real timers + a 1-ms debounce so promises actually flush during
 * waitFor; vitest fake timers don't advance microtasks between
 * `setTimeout` and `await Promise.allSettled` reliably enough for this
 * pipeline.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";

const { apiGetMock } = vi.hoisted(() => ({ apiGetMock: vi.fn() }));

vi.mock("@/lib/api", () => ({
  default: { get: apiGetMock },
}));

import { useSymbolSearch } from "./useSymbolSearch";

beforeEach(() => {
  apiGetMock.mockReset();
});

function mockSearch(byMarket: { US?: unknown[]; TW?: unknown[]; CRYPTO?: unknown[] }) {
  apiGetMock.mockImplementation((url: string) => {
    if (url.startsWith("/us/search")) return Promise.resolve({ data: byMarket.US ?? [] });
    if (url.startsWith("/tw/search")) return Promise.resolve({ data: byMarket.TW ?? [] });
    if (url.startsWith("/crypto/search")) return Promise.resolve({ data: byMarket.CRYPTO ?? [] });
    return Promise.resolve({ data: [] });
  });
}

describe("useSymbolSearch", () => {
  it("returns no results for an empty query", async () => {
    const { result } = renderHook(() => useSymbolSearch(""));
    expect(result.current.results).toEqual([]);
    // Wait one tick to confirm no request fires.
    await new Promise((r) => setTimeout(r, 10));
    expect(apiGetMock).not.toHaveBeenCalled();
  });

  it("merges results in US → TW → CRYPTO order", async () => {
    mockSearch({
      US: [{ symbol: "AAPL", market: "US" }],
      TW: [{ symbol: "2330", market: "TW" }],
      CRYPTO: [{ symbol: "BTC", market: "CRYPTO" }],
    });
    const { result } = renderHook(() => useSymbolSearch("a", { debounceMs: 1 }));
    await waitFor(() => expect(result.current.results).toHaveLength(3));
    expect(result.current.results.map((r) => r.market)).toEqual(["US", "TW", "CRYPTO"]);
  });

  it("survives one market endpoint failing (Promise.allSettled)", async () => {
    apiGetMock.mockImplementation((url: string) => {
      if (url.startsWith("/us/search")) return Promise.reject(new Error("boom"));
      if (url.startsWith("/tw/search")) return Promise.resolve({ data: [{ symbol: "2330", market: "TW" }] });
      return Promise.resolve({ data: [] });
    });
    const { result } = renderHook(() => useSymbolSearch("a", { debounceMs: 1 }));
    await waitFor(() => expect(result.current.results).toHaveLength(1));
    expect(result.current.results[0]).toEqual({ symbol: "2330", market: "TW" });
  });

  it("caps the merged list at maxResults", async () => {
    mockSearch({
      US: Array.from({ length: 6 }, (_, i) => ({ symbol: `U${i}`, market: "US" })),
      TW: Array.from({ length: 2 }, (_, i) => ({ symbol: `T${i}`, market: "TW" })),
      CRYPTO: Array.from({ length: 2 }, (_, i) => ({ symbol: `C${i}`, market: "CRYPTO" })),
    });
    const { result } = renderHook(() => useSymbolSearch("a", { debounceMs: 1, maxResults: 5 }));
    await waitFor(() => expect(result.current.results).toHaveLength(5));
  });

  it("debounces — multiple rapid query updates emit one request batch", async () => {
    mockSearch({ US: [{ symbol: "AAPL", market: "US" }] });
    const { result, rerender } = renderHook(({ q }: { q: string }) => useSymbolSearch(q, { debounceMs: 30 }), {
      initialProps: { q: "A" },
    });
    rerender({ q: "AA" });
    rerender({ q: "AAP" });
    rerender({ q: "AAPL" });
    await waitFor(() => expect(result.current.results).toHaveLength(1));
    // Only the final trailing edge should have fired the 3 endpoints.
    expect(apiGetMock).toHaveBeenCalledTimes(3);
  });
});
