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

// ── rule summaries (PR-D1) ─────────────────────────────────────

function baseAlert(over: Record<string, unknown>) {
  return {
    id: `al-${Math.random().toString(36).slice(2)}`,
    symbol: "AAPL",
    market: "US",
    condition: null,
    target_price: null,
    params: null,
    cooldown_seconds: 0,
    repeat: false,
    last_fired_at: null,
    triggered: false,
    triggered_at: null,
    created_at: "2026-07-10T00:00:00Z",
    ...over,
  };
}

function mockApiAlerts(alerts: unknown[]) {
  apiGetMock.mockImplementation((url: string) => {
    if (url.startsWith("/alerts/history")) return Promise.resolve({ data: [] });
    if (url.startsWith("/alerts")) return Promise.resolve({ data: alerts });
    return Promise.resolve({ data: [] });
  });
}

describe("AlertsPage rule summaries", () => {
  it("renders legacy price rule with target price", async () => {
    mockApiAlerts([
      baseAlert({
        condition: "above",
        condition_type: "price_above",
        target_price: 200,
      }),
    ]);
    renderPage();
    expect(await screen.findByText("alerts.above")).toBeTruthy();
    expect(screen.getByText("$200.00")).toBeTruthy();
  });

  it("renders pct-change summary", async () => {
    mockApiAlerts([
      baseAlert({ condition_type: "pct_change_above", params: { pct: 5 } }),
    ]);
    renderPage();
    expect(await screen.findByText("alerts.summary_pct_above")).toBeTruthy();
  });

  it("renders breakout summary", async () => {
    mockApiAlerts([
      baseAlert({
        symbol: "2330",
        market: "TW",
        condition_type: "breakout_high",
        params: { lookback_days: 60 },
      }),
    ]);
    renderPage();
    expect(await screen.findByText("alerts.summary_breakout_high")).toBeTruthy();
  });

  it("renders volume-surge and streak summaries", async () => {
    mockApiAlerts([
      baseAlert({
        condition_type: "volume_surge",
        params: { multiple: 2, lookback_days: 20 },
      }),
      baseAlert({
        symbol: "2330",
        market: "TW",
        condition_type: "foreign_net_buy_streak",
        params: { days: 3 },
      }),
    ]);
    renderPage();
    expect(await screen.findByText("alerts.summary_volume_surge")).toBeTruthy();
    expect(screen.getByText("alerts.summary_streak")).toBeTruthy();
  });

  it("renders dynamic trend crossing summaries", async () => {
    mockApiAlerts([
      baseAlert({
        condition_type: "trend_cross_above",
        params: {
          start_time: "2026-01-01", start_price: 100,
          end_time: "2026-01-11", end_price: 110,
        },
      }),
      baseAlert({
        symbol: "2330", market: "TW",
        condition_type: "trend_cross_below",
        params: {
          start_time: "2026-01-01", start_price: 600,
          end_time: "2026-01-11", end_price: 580,
        },
      }),
    ]);
    renderPage();
    expect(await screen.findByText("alerts.summary_trend_cross_above")).toBeTruthy();
    expect(screen.getByText("alerts.summary_trend_cross_below")).toBeTruthy();
  });

  it("renders RSI crossing summaries", async () => {
    mockApiAlerts([
      baseAlert({
        condition_type: "rsi_cross_above",
        params: { period: 14, level: 70 },
      }),
      baseAlert({
        symbol: "2330", market: "TW",
        condition_type: "rsi_cross_below",
        params: { period: 10, level: 30 },
      }),
    ]);
    renderPage();
    expect(await screen.findByText("alerts.summary_rsi_cross_above")).toBeTruthy();
    expect(screen.getByText("alerts.summary_rsi_cross_below")).toBeTruthy();
  });

  it("shows a repeat badge with cooldown label for repeat alerts", async () => {
    mockApiAlerts([
      baseAlert({
        condition_type: "pct_change_above",
        params: { pct: 5 },
        repeat: true,
        cooldown_seconds: 3600,
      }),
    ]);
    renderPage();
    expect((await screen.findByText(/alerts\.repeat_badge/)).textContent).toContain("1h");
  });

  it("does not show a repeat badge for once-only alerts", async () => {
    mockApiAlerts([
      baseAlert({
        condition: "above",
        condition_type: "price_above",
        target_price: 100,
      }),
    ]);
    renderPage();
    await screen.findByText("alerts.above");
    expect(screen.queryByText(/alerts\.repeat_badge/)).toBeNull();
  });
});
