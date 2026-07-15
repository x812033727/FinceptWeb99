import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api", () => ({ default: { get: vi.fn() } }));

import api from "@/lib/api";
import AttributionPanel from "./AttributionPanel";

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><AttributionPanel portfolioId="p1" /></QueryClientProvider>);
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.get).mockResolvedValue({ data: {
    currency: "USD", methodology_version: "modified-dietz-cash-ledger-v2", empty: false,
    portfolio_return_pct: 12.4, benchmark: "SPY", benchmark_return_pct: 8.1,
    active_return_pct: 4.3, disclaimer: "Modified Dietz", excluded: [],
    markets: [{ market: "US", start_weight_pct: 100, market_return_pct: 12.4, contribution_pct: 12.4, pnl_after_flows: 1240 }],
    positions: [{ symbol: "AAPL", market: "US", start_weight_pct: 60,
      contribution_pct: 7.2, position_return_pct: 12, pnl_after_flows: 720,
      net_cash_flow: 100 }],
  } });
});

describe("AttributionPanel", () => {
  it("renders active return and fetches a selected period", async () => {
    renderPanel();
    expect(await screen.findByText("AAPL")).toBeInTheDocument();
    expect(screen.getByText("+4.30%")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "180D" }));
    await waitFor(() => expect(api.get).toHaveBeenCalledWith("/portfolio/p1/attribution?days=180"));
  });
});
