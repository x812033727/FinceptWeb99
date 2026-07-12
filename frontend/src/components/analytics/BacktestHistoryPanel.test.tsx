/**
 * C3 — BacktestHistoryPanel: saved-runs list rendering, delete confirm,
 * and the compare view (normalized-equity chart + metrics table)
 * against mocked API data. Persistence/scoping semantics live in
 * backend test_backtest_runs_api.py.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const { apiGetMock, apiDeleteMock } = vi.hoisted(() => ({
  apiGetMock: vi.fn(),
  apiDeleteMock: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  default: {
    get: apiGetMock,
    post: vi.fn().mockResolvedValue({ data: {} }),
    delete: apiDeleteMock,
  },
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (k: string, fallback?: string) => fallback ?? k,
    i18n: { language: "en", changeLanguage: vi.fn() },
  }),
}));

import BacktestHistoryPanel from "./BacktestHistoryPanel";
import { toChartRows } from "./compare";

const runA = {
  id: "11111111-1111-1111-1111-111111111111",
  name: "SMA 快慢線",
  strategy: "sma_crossover",
  created_at: "2026-07-11T10:00:00Z",
  config: { symbols: ["AAPL"] },
  metrics: { total_return_pct: 12.34, sharpe_ratio: 1.1, total_trades: 8, final_value: 112340 },
};
const runB = {
  id: "22222222-2222-2222-2222-222222222222",
  name: null,
  strategy: "momentum",
  created_at: "2026-07-10T09:00:00Z",
  config: { symbols: ["2330"] },
  metrics: { total_return_pct: -3.5, sharpe_ratio: -0.2, total_trades: 4, final_value: 96500 },
};

const compareData = {
  dates: ["2024-01-01", "2024-01-02", "2024-01-03"],
  runs: [
    { id: runA.id, name: runA.name, strategy: runA.strategy, created_at: runA.created_at,
      metrics: runA.metrics, values: [100, 110, 120] },
    { id: runB.id, name: runB.name, strategy: runB.strategy, created_at: runB.created_at,
      metrics: runB.metrics, values: [null, 100, 90] },
  ],
};

function mockApi({ items = [runA, runB], compare = compareData } = {}) {
  apiGetMock.mockImplementation((url: string) => {
    if (url.startsWith("/analytics/backtest-runs/compare")) {
      return Promise.resolve({ data: compare });
    }
    if (url.startsWith("/analytics/backtest-runs")) {
      return Promise.resolve({
        data: { items, total: items.length, limit: 50, offset: 0 },
      });
    }
    return Promise.resolve({ data: [] });
  });
}

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <BacktestHistoryPanel />
    </QueryClientProvider>
  );
}

beforeEach(() => {
  apiGetMock.mockReset();
  apiDeleteMock.mockReset();
  apiDeleteMock.mockResolvedValue({ data: { status: "deleted" } });
});

describe("BacktestHistoryPanel list", () => {
  it("shows the empty state when there are no saved runs", async () => {
    mockApi({ items: [] });
    renderPanel();
    expect(await screen.findByText("analytics.backtest.history_empty")).toBeTruthy();
  });

  it("renders name / strategy / date / total return per run", async () => {
    mockApi();
    renderPanel();
    expect(await screen.findByText("SMA 快慢線")).toBeTruthy();
    // Unnamed run falls back to the placeholder label.
    expect(screen.getByText("analytics.backtest.unnamed")).toBeTruthy();
    // Strategy label falls back to the raw name under the mocked t().
    expect(screen.getByText("momentum")).toBeTruthy();
    expect(screen.getByText("2026-07-11")).toBeTruthy();
    expect(screen.getByText("+12.34%")).toBeTruthy();
    expect(screen.getByText("-3.50%")).toBeTruthy();
  });

  it("deletes a run after confirm", async () => {
    mockApi();
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    renderPanel();
    await screen.findByText("SMA 快慢線");
    fireEvent.click(screen.getAllByText("common.delete")[0]);
    await waitFor(() =>
      expect(apiDeleteMock).toHaveBeenCalledWith(`/analytics/backtest-runs/${runA.id}`)
    );
    confirmSpy.mockRestore();
  });

  it("does not delete when confirm is cancelled", async () => {
    mockApi();
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    renderPanel();
    await screen.findByText("SMA 快慢線");
    fireEvent.click(screen.getAllByText("common.delete")[0]);
    expect(apiDeleteMock).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
  });
});

describe("BacktestHistoryPanel compare", () => {
  it("keeps compare disabled until two runs are selected", async () => {
    mockApi();
    renderPanel();
    await screen.findByText("SMA 快慢線");
    const compareBtn = screen.getByText(/analytics\.backtest\.compare \(/).closest("button")!;
    expect(compareBtn.hasAttribute("disabled")).toBe(true);
    fireEvent.click(screen.getByLabelText(`select-${runA.id}`));
    expect(compareBtn.hasAttribute("disabled")).toBe(true);
    fireEvent.click(screen.getByLabelText(`select-${runB.id}`));
    expect(compareBtn.hasAttribute("disabled")).toBe(false);
  });

  it("mounts the compare view (chart + metrics table) with mocked data", async () => {
    mockApi();
    renderPanel();
    await screen.findByText("SMA 快慢線");
    fireEvent.click(screen.getByLabelText(`select-${runA.id}`));
    fireEvent.click(screen.getByLabelText(`select-${runB.id}`));
    fireEvent.click(screen.getByText(/analytics\.backtest\.compare \(/));

    // Compare section mounts once the (mocked) compare query resolves.
    expect(await screen.findByTestId("backtest-compare")).toBeTruthy();
    expect(
      apiGetMock.mock.calls.some(([url]) =>
        url === `/analytics/backtest-runs/compare?ids=${runA.id},${runB.id}`)
    ).toBe(true);
    expect(screen.getByText("analytics.backtest.normalized_equity")).toBeTruthy();
    // Metrics table: header per run + side-by-side values.
    expect(screen.getByText("analytics.backtest.metric")).toBeTruthy();
    expect(screen.getByText("1.10")).toBeTruthy();   // sharpe run A
    expect(screen.getByText("-0.20")).toBeTruthy();  // sharpe run B
  });
});

describe("toChartRows", () => {
  it("pivots aligned compare series into per-date rows", () => {
    const rows = toChartRows(compareData);
    expect(rows).toEqual([
      { date: "2024-01-01", r0: 100, r1: null },
      { date: "2024-01-02", r0: 110, r1: 100 },
      { date: "2024-01-03", r0: 120, r1: 90 },
    ]);
  });
});
