import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api", () => ({
  default: { post: vi.fn() }, errorDetail: (error: unknown) => String(error),
}));

import api from "@/lib/api";
import StressTestPanel from "./StressTestPanel";

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}><StressTestPanel portfolioId="p1" /></QueryClientProvider>);
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.post).mockResolvedValue({ data: {
    currency: "TWD", portfolio_value: 100000, gap_symbol: "2330",
    disclaimer: "Decision support only.",
    scenarios: [{
      scenario: "taiex_drawdown", label: "TAIEX -10%", pnl: -7000, pnl_pct: -7,
      post_scenario_value: 93000,
      holdings: [{ symbol: "2330", market: "TW", shock_pct: -10, pnl: -6000, risk_contribution_pct: 85.71 }],
      rebalance_suggestions: [{ symbol: "2330", current_stressed_weight_pct: 58, target_weight_pct: 30, indicative_amount: 26000, reason: "concentration" }],
    }],
  } });
});

describe("StressTestPanel", () => {
  it("submits explicit scenarios and renders P&L plus concentration", async () => {
    renderPanel();
    fireEvent.change(screen.getByPlaceholderText("2330"), { target: { value: "2454" } });
    fireEvent.click(screen.getByRole("button", { name: "Run scenarios" }));
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/portfolio/p1/stress-test", expect.objectContaining({ gap_symbol: "2454", gap_pct: -20 })));
    expect(await screen.findByText("-7,000 TWD")).toBeInTheDocument();
    expect(screen.getByText("Concentration review")).toBeInTheDocument();
  });
});
