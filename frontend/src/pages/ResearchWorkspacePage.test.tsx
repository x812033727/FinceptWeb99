import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api", () => ({
  default: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
  errorDetail: (error: unknown) => String(error),
}));

import api from "@/lib/api";
import ResearchWorkspacePage from "./ResearchWorkspacePage";

const summary = {
  theses: { active: 1, events: 1, recent: [{ thesis_id: "t1", type: "news", title: "Demand update", occurred_at: "2026-07-15T00:00:00Z" }] },
  alerts: { count: 2 },
  ai: { d5_brier_score: 0.04, calibration_sample_size: 3 },
  pending: { thesis_reviews: [], decision_outcomes: [], watch_triggers: [] },
};
const thesis = {
  id: "t1", market: "TW", symbol: "2330", title: "Foundry demand", status: "active",
  core_case: "Advanced nodes compound.", catalysts: [], risks: [], valuation: {},
  watch_conditions: [], review_date: "2026-08-01", last_reviewed_at: null,
};
const journal = {
  entries: [{
    id: "d1", source_type: "paper_recommendation", market: "TW", symbol: "2330",
    confidence: 0.8, entry_price: 100, max_drawdown_pct: -3, observations: 20, status: "resolved",
    outcomes: { d1: { resolved: true, net_return_pct: 1 }, d5: { resolved: true, net_return_pct: 4 }, d20: { resolved: true, net_return_pct: 8 } },
  }],
  summary: { horizons: {
    d1: { average_net_return_pct: 1, win_rate_pct: 60, sample_size: 5 },
    d5: { average_net_return_pct: 4, win_rate_pct: 70, sample_size: 5 },
    d20: { average_net_return_pct: 8, win_rate_pct: 80, sample_size: 5 },
  } },
};
const pickRuns = {
  disclaimer: "Research candidates only; not investment advice.",
  runs: [{
    id: "p1", market: "TW", run_date: "2026-07-15",
    methodology_version: "trusted-report-ranking-v1", candidate_count: 1,
    generated_at: "2026-07-15T09:00:00Z",
    candidates: [{
      rank: 1, symbol: "2330", market: "TW", score: 92, confidence: 0.92,
      rationale: "偏多觀察，先進製程需求延續。", source_report_id: "r1",
      report_quality: 0.95, quality_details: { band: "high", issue_counts: {} },
      evidence: [{ id: "E1", path: "fundamentals.pe", source: "twse", as_of: "2026-07-15" }],
    }],
  }],
};

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}><ResearchWorkspacePage /></QueryClientProvider>);
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.get).mockImplementation((url: string) => {
    if (url === "/research/weekly-summary") return Promise.resolve({ data: summary });
    if (url === "/theses") return Promise.resolve({ data: [thesis] });
    if (url === "/decision-journal") return Promise.resolve({ data: journal });
    if (url === "/research/daily-picks/latest") return Promise.resolve({ data: pickRuns });
    if (url === "/theses/t1/timeline") return Promise.resolve({ data: [] });
    return Promise.reject(new Error(`unexpected ${url}`));
  });
  vi.mocked(api.post).mockResolvedValue({ data: {} });
});

describe("ResearchWorkspacePage", () => {
  it("renders weekly summary and D1/D5/D20 journal", async () => {
    renderPage();
    expect(await screen.findByText("Demand update")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "journal" }));
    expect(await screen.findByText("TW:2330")).toBeInTheDocument();
    expect(screen.getAllByText("+8.00%")).toHaveLength(2);
  });

  it("shows traceable daily candidates and can request a new market run", async () => {
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: "picks" }));
    expect(await screen.findByText("TW:2330")).toBeInTheDocument();
    expect(screen.getByText(/1 evidence refs/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Generate US" }));
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/research/daily-picks/generate?market=US"));
  });

  it("creates a thesis through the public form", async () => {
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: /New thesis/i }));
    fireEvent.change(screen.getByPlaceholderText("Symbol"), { target: { value: "2330" } });
    fireEvent.change(screen.getByPlaceholderText("Thesis title"), { target: { value: "AI demand" } });
    fireEvent.change(screen.getByPlaceholderText(/Core case/), { target: { value: "Advanced nodes remain constrained." } });
    fireEvent.change(screen.getByLabelText("Condition label"), { target: { value: "Revenue growth floor" } });
    fireEvent.change(screen.getByLabelText("Condition threshold"), { target: { value: "10" } });
    fireEvent.click(screen.getByRole("button", { name: "Add condition" }));
    fireEvent.click(screen.getByRole("button", { name: "Create thesis" }));
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/theses", expect.objectContaining({
      market: "TW",
      symbol: "2330",
      title: "AI demand",
      watch_conditions: [{
        label: "Revenue growth floor",
        metric: "revenue_yoy_pct",
        operator: "lt",
        threshold: 10,
      }],
    })));
  });

  it("submits an actionable data-quality report", async () => {
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: "Report data issue" }));
    fireEvent.change(screen.getByPlaceholderText("Symbol (optional)"), { target: { value: "2330" } });
    fireEvent.change(screen.getByPlaceholderText("Endpoint or page (optional)"), { target: { value: "/api/tw/quote/2330" } });
    fireEvent.change(screen.getByPlaceholderText(/Describe what appears wrong/), { target: { value: "The quote timestamp is stale." } });
    fireEvent.click(screen.getByRole("button", { name: "Submit issue" }));
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/feedback/data-quality", expect.objectContaining({ market: "TW", symbol: "2330", category: "stale", endpoint: "/api/tw/quote/2330" })));
  });
});
