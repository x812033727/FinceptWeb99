import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import api from "@/lib/api";
import { OptionsPanel } from "./OptionsPanel";
import type { OptionsAnalysisResponse } from "./_shared";

vi.mock("@/lib/api", () => ({ default: { get: vi.fn() } }));

const expiry = {
  expiration_date: "2026-08-15",
  days_to_expiry: 31,
  contract_count: 4,
  call_open_interest: 160,
  put_open_interest: 190,
  put_call_open_interest_ratio: 1.1875,
  call_volume: 70,
  put_volume: 85,
  put_call_volume_ratio: 85 / 70,
  atm_iv: .21,
  atm_call_iv: .20,
  atm_put_iv: .22,
  atm_call_strike: 100,
  atm_put_strike: 100,
  expected_move: 6.12,
  expected_move_pct: .0612,
  put_90_iv: .30,
  put_90_strike: 90,
  call_110_iv: .25,
  call_110_strike: 110,
  wing_skew_iv_points: 5,
  max_pain: 100,
  max_pain_distance_pct: 0,
  max_pain_total_payout: 10000,
};

const analysis: OptionsAnalysisResponse = {
  symbol: "TEST",
  spot: 100,
  spot_source: "polygon",
  as_of: "2026-07-15",
  contracts: [
    { ticker: "C100", contract_type: "call", expiration_date: "2026-08-15", strike_price: 100, last_price: 2, bid: 1.9, ask: 2.1, volume: 70, open_interest: 160, implied_volatility: .2, delta: .5, gamma: .02, theta: -.01, vega: .1, data_source: "polygon" },
    { ticker: "P100", contract_type: "put", expiration_date: "2026-08-15", strike_price: 100, last_price: 2.2, bid: 2.1, ask: 2.3, volume: 85, open_interest: 190, implied_volatility: .22, delta: -.5, gamma: .02, theta: -.01, vega: .1, data_source: "polygon" },
  ],
  expiries: [expiry],
  quality: { status: "good", flags: [], sources: ["polygon"], rows_received: 2, rows_usable: 2, iv_coverage_pct: 100, open_interest_coverage_pct: 100 },
  methodology: { version: "options-chain-analytics-v1", wing_skew: "not 25-delta skew" },
};

function renderPanel(data: OptionsAnalysisResponse = analysis) {
  vi.mocked(api.get).mockResolvedValue({ data });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <OptionsPanel symbol="TEST" />
    </QueryClientProvider>,
  );
}

describe("OptionsPanel analytics", () => {
  beforeEach(() => vi.clearAllMocks());

  it("renders evidence-bounded overview metrics and term structure", async () => {
    renderPanel();
    expect(await screen.findByText("Data completeness: good")).toBeInTheDocument();
    expect(screen.getAllByText("21.0%").length).toBeGreaterThan(0);
    expect(screen.getAllByText("±$6.12").length).toBeGreaterThan(0);
    expect(screen.getAllByText("1.19").length).toBeGreaterThan(0);
    expect(screen.getAllByText("$100.00").length).toBeGreaterThan(0);
    expect(api.get).toHaveBeenCalledWith("/us/options-analysis/TEST?max_expiries=8");
  });

  it("uses strike_price from the provider contract in the chain table", async () => {
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: "Chain" }));
    await waitFor(() => expect(screen.getByRole("table", { name: "Options chain" })).toBeInTheDocument());
    expect(screen.getByText("100.00")).toBeInTheDocument();
    expect(screen.getByText("20.0%")).toBeInTheDocument();
  });

  it("surfaces sparse-data reasons instead of presenting false precision", async () => {
    renderPanel({
      ...analysis,
      quality: {
        ...analysis.quality,
        status: "degraded",
        flags: ["spot_unavailable", "iv_sparse"],
        iv_coverage_pct: 25,
      },
    });
    expect(await screen.findByText("Data completeness: degraded")).toBeInTheDocument();
    expect(screen.getByText(/Spot price unavailable/)).toBeInTheDocument();
    expect(screen.getByText(/IV coverage below 50%/)).toBeInTheDocument();
  });

  it("shows an explicit unavailable state for an empty chain", async () => {
    renderPanel({
      ...analysis,
      contracts: [],
      expiries: [],
      quality: { ...analysis.quality, status: "unavailable", rows_usable: 0 },
    });
    expect(await screen.findByText("No usable options-chain data is available.")).toBeInTheDocument();
  });
});
