import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { apiGetMock } = vi.hoisted(() => ({ apiGetMock: vi.fn() }));

vi.mock("@/lib/api", () => ({
  default: { get: apiGetMock },
}));

import { HealthPanel } from "./HealthPanel";

const response = {
  symbol: "2330",
  market: "TW",
  periods: [{
    date: "2025-12-31", revenue: 1000, net_income: 200, eps: 10,
    gross_margin: 55, operating_margin: 45, net_margin: 20,
    debt_ratio: 30, current_ratio: 2, operating_cf: 240, free_cf: 180,
    total_equity: 2000, total_assets: 3000, total_liabilities: 1000,
    current_assets: 800, current_liabilities: 400, cash: 500, capex: -60,
    revenue_yoy: 12, net_income_yoy: 15, eps_yoy: 15,
    cash_conversion: 1.2, free_cf_margin: 18,
  }],
  summary: {
    latest_roe: 20, latest_roa: 13.3, latest_debt_ratio: 30,
    latest_gross_margin: 55, latest_net_margin: 20, revenue_yoy: 12,
    cf_positive_streak_4q: 4, ttm_revenue: 4000, ttm_net_income: 800,
    ttm_operating_cf: 960, ttm_free_cf: 720, ttm_net_margin: 20,
    cash_conversion_ttm: 1.2, asset_turnover: 1.33,
    equity_multiplier: 1.5, dupont_roe: 39.9,
  },
  lights: { profitability: "green", safety: "green", growth: "green", cash_flow: "green" },
  signals: [{ code: "strong_cash_conversion", direction: "positive", value: 1.2, unit: "ratio" }],
  quality: {
    status: "good", flags: [], sources: ["finmind"],
    statement_periods: { income: 8, balance_sheet: 8, cash_flow: 8 },
    latest_core_coverage_pct: 100,
  },
  methodology: { ttm: "complete quarters only" },
};

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <HealthPanel symbol="2330" />
    </QueryClientProvider>,
  );
}

describe("HealthPanel statement analysis", () => {
  beforeEach(() => {
    apiGetMock.mockReset();
    apiGetMock.mockResolvedValue({ data: response });
  });

  it("shows quality, DuPont decomposition, and evidence-bounded signals", async () => {
    renderPanel();

    expect(await screen.findByText("All three statements are available")).toBeInTheDocument();
    expect(screen.getByText("Core-field coverage 100% · finmind")).toBeInTheDocument();
    expect(screen.getByText("DuPont Analysis (TTM)")).toBeInTheDocument();
    expect(screen.getByText("Earnings have strong cash support")).toBeInTheDocument();
    expect(screen.getByText("TTM requires four complete quarters; cash-flow YTD facts are first converted to standalone quarters and partial windows are not annualized.")).toBeInTheDocument();
    expect(apiGetMock).toHaveBeenCalledWith("/tw/health/2330?periods=8");
  });
});
