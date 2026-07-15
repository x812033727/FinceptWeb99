import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import AxiosMockAdapter from "axios-mock-adapter";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import api from "@/lib/api";

const mocks = vi.hoisted(() => ({
  exportCSV: vi.fn(),
  exportMutateAsync: vi.fn(),
}));

vi.mock("@/hooks/usePortfolio", () => ({
  useDeleteTransaction: () => ({ mutate: vi.fn() }),
  useExportPortfolioTransactions: () => ({
    mutateAsync: mocks.exportMutateAsync,
    isPending: false,
    isError: false,
    error: null,
  }),
}));

vi.mock("./_shared", () => ({ exportCSV: mocks.exportCSV }));

import { TransactionHistory } from "./TransactionHistory";

const mock = new AxiosMockAdapter(api);

function transaction(index: number) {
  return {
    id: `tx-${index}`,
    symbol: `SYM${index}`,
    market: "US",
    tx_type: "buy",
    quantity: 1,
    price: 100,
    fx_rate: 1,
    tx_date: "2024-01-02",
    notes: null,
    created_at: "2024-01-02T00:00:00Z",
  };
}

describe("TransactionHistory", () => {
  beforeEach(() => {
    mock.reset();
    mocks.exportCSV.mockReset();
    mocks.exportMutateAsync.mockReset();
  });

  afterEach(() => mock.reset());

  it("loads additional pages and exports the complete server history", async () => {
    const all = Array.from({ length: 101 }, (_, index) => transaction(index));
    mock.onGet("/portfolio/portfolio-1/transactions", {
      params: { limit: 101, offset: 0 },
    }).reply(200, all);
    mock.onGet("/portfolio/portfolio-1/transactions", {
      params: { limit: 101, offset: 100 },
    }).reply(200, all.slice(100));
    mocks.exportMutateAsync.mockResolvedValue(all);
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    render(
      <QueryClientProvider client={client}>
        <TransactionHistory portfolioId="portfolio-1" />
      </QueryClientProvider>,
    );

    await screen.findByText("100 transactions loaded");
    expect(screen.queryByText("SYM100")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    await screen.findByText("101 transactions loaded");
    expect(screen.getByText("SYM100")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Export CSV" }));
    await waitFor(() => expect(mocks.exportCSV).toHaveBeenCalled());
    expect(mocks.exportCSV.mock.calls[0][0]).toHaveLength(101);
    expect(mocks.exportCSV.mock.calls[0][1]).toBe("transactions-portfolio-1.csv");
  });
});
