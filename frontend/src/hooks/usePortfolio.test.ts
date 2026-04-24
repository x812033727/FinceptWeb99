/**
 * Unit tests for usePortfolio hooks.
 *
 * Each hook is a thin TanStack Query wrapper around the /portfolio API.
 * We verify: correct endpoint called, query key, enabled-gate, and cache
 * invalidation after mutations.
 */
import { renderHook, waitFor, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import AxiosMockAdapter from "axios-mock-adapter";
import { createElement } from "react";
import { describe, it, expect, beforeEach, afterEach } from "vitest";
import api from "@/lib/api";
import {
  usePortfolios,
  usePortfolioDetail,
  useCreatePortfolio,
  useDeletePortfolio,
  useAddTransaction,
  useOptimise,
} from "./usePortfolio";

// Mock the api instance (has baseURL "/api"; paths below are relative to it)
const mock = new AxiosMockAdapter(api);

function makeWrapper() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return {
    qc,
    wrapper: ({ children }: { children: React.ReactNode }) =>
      createElement(QueryClientProvider, { client: qc }, children),
  };
}

beforeEach(() => mock.reset());
afterEach(() => mock.reset());

// ── usePortfolios ────────────────────────────────────────────────────

describe("usePortfolios", () => {
  it("calls GET /api/portfolio and returns data", async () => {
    const data = [{ id: "1", name: "My Portfolio", currency: "USD" }];
    mock.onGet("/portfolio").reply(200, data);

    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => usePortfolios(), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(data);
  });

  it("exposes error when API fails", async () => {
    mock.onGet("/portfolio").reply(500);

    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => usePortfolios(), { wrapper });

    await waitFor(() => expect(result.current.isError).toBe(true));
  });
});

// ── usePortfolioDetail ───────────────────────────────────────────────

describe("usePortfolioDetail", () => {
  it("calls GET /api/portfolio/:id when id is provided", async () => {
    const detail = { id: "abc", name: "Growth", currency: "USD", holdings: [] };
    mock.onGet("/portfolio/abc").reply(200, detail);

    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => usePortfolioDetail("abc"), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.id).toBe("abc");
  });

  it("is disabled and never fetches when id is null", async () => {
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => usePortfolioDetail(null), { wrapper });

    // fetchStatus 'idle' means the query was gated by `enabled: false`
    expect(result.current.fetchStatus).toBe("idle");
    expect(result.current.data).toBeUndefined();
  });
});

// ── useCreatePortfolio ───────────────────────────────────────────────

describe("useCreatePortfolio", () => {
  it("POSTs to /api/portfolio and invalidates portfolios query", async () => {
    const newPf = { id: "x1", name: "New", currency: "USD" };
    mock.onPost("/portfolio").reply(201, newPf);
    mock.onGet("/portfolio").reply(200, [newPf]);

    const { qc, wrapper } = makeWrapper();
    qc.setQueryData(["portfolios"], []);

    const { result } = renderHook(() => useCreatePortfolio(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({ name: "New", currency: "USD" });
    });

    expect(qc.getQueryState(["portfolios"])?.isInvalidated).toBe(true);
  });
});

// ── useDeletePortfolio ───────────────────────────────────────────────

describe("useDeletePortfolio", () => {
  it("DELETEs /api/portfolio/:id and invalidates portfolios query", async () => {
    mock.onDelete("/portfolio/p1").reply(204);

    const { qc, wrapper } = makeWrapper();
    qc.setQueryData(["portfolios"], [{ id: "p1", name: "Old", currency: "USD" }]);

    const { result } = renderHook(() => useDeletePortfolio(), { wrapper });

    await act(async () => {
      await result.current.mutateAsync("p1");
    });

    expect(qc.getQueryState(["portfolios"])?.isInvalidated).toBe(true);
  });
});

// ── useAddTransaction ────────────────────────────────────────────────

describe("useAddTransaction", () => {
  it("POSTs to /api/portfolio/:id/transaction and invalidates detail", async () => {
    const tx = { id: "t1", symbol: "AAPL", market: "US", tx_type: "buy", quantity: 10, price: 150 };
    mock.onPost("/portfolio/p2/transaction").reply(201, tx);

    const { qc, wrapper } = makeWrapper();
    qc.setQueryData(["portfolio", "p2"], { id: "p2", holdings: [] });

    const { result } = renderHook(() => useAddTransaction("p2"), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({
        symbol: "AAPL", market: "US", tx_type: "buy",
        quantity: 10, price: 150,
      } as any);
    });

    expect(qc.getQueryState(["portfolio", "p2"])?.isInvalidated).toBe(true);
  });
});

// ── useOptimise ──────────────────────────────────────────────────────

describe("useOptimise", () => {
  it("POSTs to /api/portfolio/:id/optimise and returns result", async () => {
    const resultData = { weights: { AAPL: 0.6, MSFT: 0.4 }, metrics: { annual_return: 0.15 } };
    mock.onPost("/portfolio/p3/optimise").reply(200, resultData);

    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useOptimise("p3"), { wrapper });

    let mutationResult: unknown;
    await act(async () => {
      mutationResult = await result.current.mutateAsync({ target_risk: "moderate", max_weight: 0.4 });
    });

    expect(mutationResult).toEqual(resultData);
  });
});
