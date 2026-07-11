/**
 * Tests for AlertsPage's fired-alert history section (PR-D5/D4).
 * CRUD behaviour is exercised at the API layer (backend
 * test_alerts_api.py); here we verify the 歷史 list renders events
 * from GET /alerts/history, including the distinct strategy_health
 * badge.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
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
    t: (k: string) => k,
    i18n: { language: "en", changeLanguage: vi.fn() },
  }),
}));

import AlertsPage from "./AlertsPage";

const priceEvent = {
  id: "ev-1",
  alert_id: "al-1",
  symbol: "AAPL",
  market: "US",
  kind: "price",
  message: "AAPL 價格高於目標 200(現價 210)",
  fired_at: "2026-07-10T12:00:00Z",
  payload: { condition: "above", target_price: 200, current_price: 210 },
};

const strategyEvent = {
  id: "ev-2",
  alert_id: null,
  symbol: "台股動能策略",
  market: "TW",
  kind: "strategy_health",
  message: "策略「台股動能策略」健康度劣化:brier_drift",
  fired_at: "2026-07-10T02:00:00Z",
  payload: { strategy_id: "st-1", status_flags: ["brier_drift"] },
};

function mockApi(history: unknown[]) {
  apiGetMock.mockImplementation((url: string) => {
    if (url.startsWith("/alerts/history")) {
      return Promise.resolve({ data: history });
    }
    return Promise.resolve({ data: [] });
  });
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AlertsPage />
    </QueryClientProvider>
  );
}

beforeEach(() => {
  apiGetMock.mockReset();
});

describe("AlertsPage history section", () => {
  it("shows the empty state when there are no fired events", async () => {
    mockApi([]);
    renderPage();
    expect(await screen.findByText("alerts.no_history")).toBeTruthy();
  });

  it("renders fired events with time, symbol and message", async () => {
    mockApi([priceEvent, strategyEvent]);
    renderPage();
    expect(await screen.findByText("AAPL")).toBeTruthy();
    expect(screen.getByText(priceEvent.message)).toBeTruthy();
    expect(screen.getByText(strategyEvent.symbol)).toBeTruthy();
    expect(screen.getByText(strategyEvent.message)).toBeTruthy();
    expect(screen.queryByText("alerts.no_history")).toBeNull();
  });

  it("renders a price badge for kind=price rows", async () => {
    mockApi([priceEvent]);
    renderPage();
    expect(await screen.findByText("alerts.kind_price")).toBeTruthy();
    expect(screen.queryByText("alerts.kind_strategy_health")).toBeNull();
  });

  it("renders a distinct warning badge for kind=strategy_health rows", async () => {
    mockApi([strategyEvent]);
    renderPage();
    const badge = await screen.findByText("alerts.kind_strategy_health");
    expect(badge.className).toContain("text-warning");
  });
});
