/**
 * Tests for RiskDashboardPanel (feature C1).
 *
 * The API is mocked at the `@/lib/api` seam; react-i18next is mocked
 * to echo keys (with interpolation values appended) so assertions are
 * locale-independent. Covers: metrics rendering from a mocked risk
 * payload, the empty state, concentration warning banners, the
 * insufficient-history excluded note, and the diverging heatmap
 * color helper.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const { apiGetMock } = vi.hoisted(() => ({ apiGetMock: vi.fn() }));

vi.mock("@/lib/api", () => ({
  default: {
    get: apiGetMock,
    post: vi.fn().mockResolvedValue({ data: {} }),
    delete: vi.fn().mockResolvedValue({ data: {} }),
  },
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (k: string, opts?: Record<string, unknown>) =>
      opts && Object.keys(opts).length
        ? `${k} ${Object.values(opts).join(" ")}`
        : k,
    i18n: { language: "en", changeLanguage: vi.fn() },
  }),
}));

import RiskDashboardPanel from "./RiskDashboardPanel";
import { correlationCellStyle } from "./_shared";
import type { PortfolioRisk } from "@/types/portfolio";

function makeRisk(overrides: Partial<PortfolioRisk> = {}): PortfolioRisk {
  return {
    portfolio_id: "p1",
    currency: "USD",
    as_of: "2026-07-11",
    portfolio_value: 100_000,
    observations: 250,
    empty: false,
    benchmark: "SPY",
    metrics: {
      annualised_return: 0.12,
      annualised_volatility: 0.18,
      sharpe_ratio: 0.65,
      sortino_ratio: 0.9,
      calmar_ratio: 0.5,
      max_drawdown: -0.22,
      beta: 1.05,
      alpha: 0.01,
    },
    var: [
      { method: "historical", confidence_level: 0.95, horizon_days: 1, var_pct: 0.021, var_amount: 2100, cvar_pct: 0.03 },
      { method: "historical", confidence_level: 0.99, horizon_days: 1, var_pct: 0.034, var_amount: 3400, cvar_pct: 0.04 },
      { method: "parametric", confidence_level: 0.95, horizon_days: 1, var_pct: 0.02, var_amount: 2000 },
      { method: "parametric", confidence_level: 0.99, horizon_days: 1, var_pct: 0.03, var_amount: 3000 },
      { method: "monte_carlo", confidence_level: 0.95, horizon_days: 1, var_pct: 0.022, var_amount: 2200, cvar_pct: 0.031 },
      { method: "monte_carlo", confidence_level: 0.99, horizon_days: 1, var_pct: 0.035, var_amount: 3500, cvar_pct: 0.045 },
    ],
    weights: [
      { symbol: "AAPL", market: "US", weight_pct: 60, risk_contribution_pct: 70 },
      { symbol: "MSFT", market: "US", weight_pct: 40, risk_contribution_pct: 30 },
    ],
    correlation: {
      symbols: ["AAPL", "MSFT"],
      matrix: [
        [1, 0.62],
        [0.62, 1],
      ],
    },
    warnings: [
      { kind: "single_position", key: "AAPL", weight_pct: 60, threshold_pct: 25 },
      { kind: "market_bucket", key: "US", weight_pct: 100, threshold_pct: 50 },
    ],
    excluded: [],
    ...overrides,
  };
}

function mockApi(risk: PortfolioRisk) {
  apiGetMock.mockImplementation((url: string) => {
    if (url.includes("/risk")) return Promise.resolve({ data: risk });
    return Promise.resolve({ data: [] });
  });
}

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <RiskDashboardPanel portfolioId="p1" />
    </QueryClientProvider>
  );
}

beforeEach(() => {
  apiGetMock.mockReset();
});

// ── metrics rendering ─────────────────────────────────────────────

describe("RiskDashboardPanel metrics", () => {
  it("renders VaR tiles for all three methods with 95% headline", async () => {
    mockApi(makeRisk());
    renderPanel();

    await waitFor(() => {
      expect(screen.getByText("portfolio.risk.method_historical")).toBeInTheDocument();
    });
    expect(screen.getByText("portfolio.risk.method_parametric")).toBeInTheDocument();
    expect(screen.getByText("portfolio.risk.method_monte_carlo")).toBeInTheDocument();
    // historical 95% VaR: 0.021 → 2.10%
    expect(screen.getByText("2.10%")).toBeInTheDocument();
    // monte carlo 95% VaR: 0.022 → 2.20%
    expect(screen.getByText("2.20%")).toBeInTheDocument();
  });

  it("renders vol / sharpe / maxDD / beta stat tiles", async () => {
    mockApi(makeRisk());
    renderPanel();

    await waitFor(() => {
      expect(screen.getByText("portfolio.risk.vol")).toBeInTheDocument();
    });
    // annualised_volatility 0.18 → 18.0%
    expect(screen.getByText("18.0%")).toBeInTheDocument();
    // sharpe 0.65
    expect(screen.getByText("0.65")).toBeInTheDocument();
    // max drawdown −22.0% (formatPct is unsigned in Num, magnitude check)
    expect(screen.getByText(/22\.0%/)).toBeInTheDocument();
    // beta 1.05
    expect(screen.getByText("1.05")).toBeInTheDocument();
  });

  it("renders the correlation heatmap with per-cell values", async () => {
    mockApi(makeRisk());
    renderPanel();

    await waitFor(() => {
      expect(screen.getByTestId("correlation-heatmap")).toBeInTheDocument();
    });
    // off-diagonal 0.62 appears twice (symmetric)
    expect(screen.getAllByText("0.62")).toHaveLength(2);
    // unit diagonal
    expect(screen.getAllByText("1.00")).toHaveLength(2);
  });

  it("renders the holdings weight bar with all symbols", async () => {
    mockApi(makeRisk());
    renderPanel();

    await waitFor(() => {
      expect(screen.getByTestId("weight-bar")).toBeInTheDocument();
    });
    // Symbols appear in the weight legend AND the heatmap axes
    expect(screen.getAllByText("AAPL").length).toBeGreaterThan(0);
    expect(screen.getAllByText("MSFT").length).toBeGreaterThan(0);
  });
});

// ── warning banners ───────────────────────────────────────────────

describe("RiskDashboardPanel warnings", () => {
  it("shows a banner per concentration warning", async () => {
    mockApi(makeRisk());
    renderPanel();

    await waitFor(() => {
      expect(screen.getByTestId("risk-warnings")).toBeInTheDocument();
    });
    const alerts = screen.getAllByRole("alert");
    expect(alerts).toHaveLength(2);
    expect(alerts[0].textContent).toContain("portfolio.risk.warning_single_position");
    expect(alerts[0].textContent).toContain("AAPL");
    expect(alerts[1].textContent).toContain("portfolio.risk.warning_market_bucket");
    expect(alerts[1].textContent).toContain("US");
  });

  it("renders no banner when there are no warnings", async () => {
    mockApi(makeRisk({ warnings: [] }));
    renderPanel();

    await waitFor(() => {
      expect(screen.getByText("portfolio.risk.method_historical")).toBeInTheDocument();
    });
    expect(screen.queryByTestId("risk-warnings")).not.toBeInTheDocument();
  });
});

// ── empty / degraded states ───────────────────────────────────────

describe("RiskDashboardPanel empty and degraded states", () => {
  it("shows the empty state for a portfolio without holdings", async () => {
    mockApi(makeRisk({
      empty: true, metrics: null, var: [], weights: [],
      correlation: null, warnings: [], portfolio_value: 0, observations: 0,
    }));
    renderPanel();

    await waitFor(() => {
      expect(screen.getByText("portfolio.risk.empty")).toBeInTheDocument();
    });
    expect(screen.queryByTestId("weight-bar")).not.toBeInTheDocument();
    expect(screen.queryByTestId("correlation-heatmap")).not.toBeInTheDocument();
  });

  it("lists excluded holdings when history was insufficient", async () => {
    mockApi(makeRisk({
      excluded: [{ symbol: "NEWIPO", market: "US", reason: "insufficient_history", observations: 5 }],
    }));
    renderPanel();

    await waitFor(() => {
      expect(screen.getByTestId("risk-excluded")).toBeInTheDocument();
    });
    expect(screen.getByTestId("risk-excluded").textContent).toContain("NEWIPO");
  });

  it("shows a loading skeleton while fetching", () => {
    apiGetMock.mockImplementation(() => new Promise(() => {}));   // never resolves
    renderPanel();
    expect(screen.getByTestId("risk-skeleton")).toBeInTheDocument();
  });
});

// ── diverging heatmap color helper ────────────────────────────────

describe("correlationCellStyle", () => {
  it("uses --up for positive and --down for negative correlation", () => {
    expect(correlationCellStyle(0.8).backgroundColor).toContain("--up");
    expect(correlationCellStyle(-0.8).backgroundColor).toContain("--down");
  });

  it("fades toward transparent at the neutral midpoint", () => {
    expect(correlationCellStyle(0).backgroundColor).toContain("/ 0.000");
  });

  it("clamps out-of-range values", () => {
    expect(correlationCellStyle(1.7).backgroundColor).toContain("/ 0.850");
    expect(correlationCellStyle(-1.7).backgroundColor).toContain("--down");
  });
});
