import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { apiGetMock } = vi.hoisted(() => ({ apiGetMock: vi.fn() }));

vi.mock("@/lib/api", () => ({
  default: { get: apiGetMock, post: vi.fn() },
}));

import ScreenerPage from "./ScreenerPage";

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter><ScreenerPage /></MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ScreenerPage factor mode", () => {
  beforeEach(() => {
    apiGetMock.mockReset();
    apiGetMock.mockImplementation((url: string) => Promise.resolve({ data: url.startsWith("/tw/factor-ranking") ? {
      as_of: "2026-07-15", profile: "balanced", methodology_version: "tw-explainable-multifactor-v1",
      weights: { value: .3, momentum: .25, low_volatility: .2, income: .1, liquidity: .15 },
      candidates: [],
      quality: { status: "unavailable", flags: ["low_momentum_coverage"], sources: [],
        universe_size: 0, eligible_count: 0, returned_count: 0, momentum_coverage_pct: 0 },
      methodology: { model: "deterministic" },
    } : [] }));
  });

  it("switches from rule filters to the explainable factor workspace", async () => {
    renderPage();
    expect(screen.getByText("AI Screen")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Multi-factor" }));

    expect(await screen.findByText("Explainable multi-factor ranking")).toBeInTheDocument();
    expect(screen.getByText("The local archive does not yet contain enough valuation and price history to rank stocks reliably.")).toBeInTheDocument();
    expect(screen.queryByText("AI Screen")).not.toBeInTheDocument();
  });
});
