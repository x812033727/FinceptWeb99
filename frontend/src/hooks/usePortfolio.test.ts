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
  useExportTransactionImport,
  useExportPortfolioTransactions,
  useAddTransaction,
  useImportTransactions,
  useRollbackTransactionImport,
  useTransactionImportTransactions,
  useTransactionImports,
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
    qc.setQueryData(["portfolio-transactions", "p2", { symbol: "AAPL" }], {});
    qc.setQueryData(["portfolio-cash", "p2"], {});
    qc.setQueryData(["portfolio-cash-entries", "p2"], {});

    const { result } = renderHook(() => useAddTransaction("p2"), { wrapper });

    await act(async () => {
      await result.current.mutateAsync({
        symbol: "AAPL", market: "US", tx_type: "buy",
        quantity: 10, price: 150,
      } as any);
    });

    expect(qc.getQueryState(["portfolio", "p2"])?.isInvalidated).toBe(true);
    expect(qc.getQueryState(
      ["portfolio-transactions", "p2", { symbol: "AAPL" }],
    )?.isInvalidated).toBe(true);
    expect(qc.getQueryState(["portfolio-cash", "p2"])?.isInvalidated).toBe(true);
    expect(qc.getQueryState(["portfolio-cash-entries", "p2"])?.isInvalidated).toBe(true);
  });
});

describe("useImportTransactions", () => {
  it("previews without invalidation, then refreshes account data after import", async () => {
    mock.onPost("/portfolio/p2/transactions/import").reply((config) => {
      const body = JSON.parse(config.data as string) as { dry_run: boolean };
      return [200, {
        valid: true,
        valid_count: 1,
        imported_count: body.dry_run ? 0 : 1,
        errors: [],
      }];
    });
    const { qc, wrapper } = makeWrapper();
    qc.setQueryData(["portfolio", "p2"], { id: "p2", holdings: [] });
    qc.setQueryData(["portfolio-transactions", "p2"], []);
    const { result } = renderHook(() => useImportTransactions("p2"), { wrapper });
    const rows = [{
      tx_date: "2024-01-02", symbol: "AAPL", market: "US", tx_type: "buy",
      quantity: 1, price: 100,
    }];

    await act(async () => {
      await result.current.mutateAsync({ rows, dry_run: true });
    });
    expect(qc.getQueryState(["portfolio", "p2"])?.isInvalidated).toBe(false);

    await act(async () => {
      await result.current.mutateAsync({ rows, dry_run: false });
    });
    expect(qc.getQueryState(["portfolio", "p2"])?.isInvalidated).toBe(true);
    expect(qc.getQueryState(["portfolio-transactions", "p2"])?.isInvalidated).toBe(true);
  });
});

describe("transaction import history", () => {
  it("loads source batches and invalidates accounting data after rollback", async () => {
    const batches = [{
      id: "import-1", row_count: 2, linked_count: 2,
      provenance_complete: true, first_tx_date: "2024-01-02",
      last_tx_date: "2024-01-03",
      instruments: [{ symbol: "AAPL", market: "US" }],
      imported_at: "2026-07-15T14:00:00Z",
    }];
    mock.onGet("/portfolio/p2/transaction-imports").reply(200, batches);
    mock.onDelete("/portfolio/p2/transaction-imports/import-1").reply(200, {
      import_id: "import-1", removed_count: 2,
    });
    const { qc, wrapper } = makeWrapper();
    const history = renderHook(() => useTransactionImports("p2"), { wrapper });
    await waitFor(() => expect(history.result.current.isSuccess).toBe(true));
    expect(history.result.current.data).toEqual(batches);

    const batchDetails = [{
      id: "tx-1", import_id: "import-1", symbol: "AAPL", market: "US",
      tx_type: "buy", quantity: 2, price: 190, fx_rate: 1,
      tx_date: "2024-01-02", notes: null, created_at: "2026-07-15T14:00:00Z",
    }];
    mock.onGet("/portfolio/p2/transaction-imports/import-1/transactions")
      .reply(200, batchDetails);
    const details = renderHook(
      () => useTransactionImportTransactions("p2", "import-1"), { wrapper },
    );
    await waitFor(() => expect(details.result.current.isSuccess).toBe(true));
    expect(details.result.current.data).toEqual(batchDetails);

    const batchExport = renderHook(() => useExportTransactionImport("p2"), { wrapper });
    await act(async () => {
      expect(await batchExport.result.current.mutateAsync("import-1"))
        .toEqual(batchDetails);
    });
    expect(mock.history.get.filter(
      (request) => request.url === "/portfolio/p2/transaction-imports/import-1/transactions",
    )).toHaveLength(2);

    for (const key of [
      "portfolio-transactions", "portfolio", "portfolio-cash",
      "portfolio-cash-entries",
    ]) {
      qc.setQueryData([key, "p2"], {});
    }
    const rollback = renderHook(
      () => useRollbackTransactionImport("p2"), { wrapper },
    );
    await act(async () => {
      await rollback.result.current.mutateAsync("import-1");
    });

    expect(mock.history.get.filter(
      (request) => request.url === "/portfolio/p2/transaction-imports",
    )).toHaveLength(2);
    for (const key of [
      "portfolio-transactions", "portfolio", "portfolio-cash",
      "portfolio-cash-entries",
    ]) {
      expect(qc.getQueryState([key, "p2"])?.isInvalidated).toBe(true);
    }
  });
});

describe("portfolio transaction export", () => {
  it("fetches every server page instead of exporting only visible rows", async () => {
    const firstPage = Array.from({ length: 500 }, (_, index) => ({
      id: `tx-${index}`, import_id: null, symbol: "AAPL", market: "US",
      tx_type: "buy", quantity: 1, price: 100, fx_rate: 1,
      tx_date: "2024-01-02", notes: null, created_at: "2024-01-02T00:00:00Z",
    }));
    const finalPage = [{ ...firstPage[0], id: "tx-500" }];
    const filters = {
      symbol: "AAPL", market: "US" as const, tx_type: "buy" as const,
      date_from: "2024-01-01", date_to: "2024-12-31",
    };
    mock.onGet("/portfolio/p4/transactions/page", {
      params: { limit: 500, ...filters },
    }).reply(200, {
      items: firstPage, next_cursor: "cursor-500", total_count: 501,
    });
    mock.onGet("/portfolio/p4/transactions/page", {
      params: { limit: 500, ...filters, cursor: "cursor-500" },
    }).reply(200, { items: finalPage, next_cursor: null, total_count: null });

    const { wrapper } = makeWrapper();
    const exported = renderHook(() => useExportPortfolioTransactions("p4"), { wrapper });
    await act(async () => {
      expect(await exported.result.current.mutateAsync(filters)).toHaveLength(501);
    });
    expect(mock.history.get.map((request) => request.params?.cursor)).toEqual([
      undefined, "cursor-500",
    ]);
    expect(mock.history.get.every((request) => request.params?.symbol === "AAPL")).toBe(true);
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
