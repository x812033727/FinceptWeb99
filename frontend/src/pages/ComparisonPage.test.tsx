import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api", () => ({ default: { get: vi.fn() } }));
vi.mock("@/hooks/useSymbolSearch", () => ({
  useSymbolSearch: () => ({ results: [{ market: "CRYPTO", symbol: "BTC" }], loading: false }),
}));
vi.mock("recharts", async () => {
  const actual = await vi.importActual<typeof import("recharts")>("recharts");
  return { ...actual, ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div> };
});

import api from "@/lib/api";
import ComparisonPage from "./ComparisonPage";

const payload = {
  period: "3m", common_base_date: "2026-04-01", currency_note: "native currency",
  excluded: [],
  series: [
    { instrument: "US:AAPL", market: "US", symbol: "AAPL", base_date: "2026-04-01", end_date: "2026-07-01", observations: 60, return_pct: 12.5, max_drawdown_pct: -5.2, annualised_volatility_pct: 22.1, data_source: "yfinance", points: [{ date: "2026-04-01", value: 100 }, { date: "2026-07-01", value: 112.5 }] },
    { instrument: "TW:2330", market: "TW", symbol: "2330", base_date: "2026-04-01", end_date: "2026-07-01", observations: 61, return_pct: -2, max_drawdown_pct: -8, annualised_volatility_pct: 18, data_source: "twse", points: [{ date: "2026-04-01", value: 100 }, { date: "2026-07-01", value: 98 }] },
  ],
};

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<MemoryRouter initialEntries={["/compare?symbols=US:AAPL,TW:2330&period=3m"]}><QueryClientProvider client={client}><ComparisonPage /></QueryClientProvider></MemoryRouter>);
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.get).mockResolvedValue({ data: payload });
});

describe("ComparisonPage", () => {
  it("loads URL instruments and renders relative metrics", async () => {
    renderPage();
    expect(await screen.findByText("+12.50%")).toBeInTheDocument();
    expect(screen.getByText("-2.00%")).toBeInTheDocument();
    expect(api.get).toHaveBeenCalledWith(expect.stringContaining("instruments=US%3AAAPL%2CTW%3A2330"));
  });

  it("changes period and adds a searched crypto instrument", async () => {
    renderPage();
    await screen.findByText("+12.50%");
    fireEvent.click(screen.getByRole("button", { name: "6M" }));
    await waitFor(() => expect(api.get).toHaveBeenCalledWith(expect.stringContaining("period=6m")));
    fireEvent.change(screen.getByLabelText("Search and add a symbol"), { target: { value: "btc" } });
    fireEvent.click(screen.getByRole("button", { name: /BTC/ }));
    await waitFor(() => expect(api.get).toHaveBeenCalledWith(expect.stringContaining("CRYPTO%3ABTC")));
  });
});
