/**
 * DashboardPage IndexCard WS-aware quote tests (PR-9).
 *
 * The index cards subscribe to the live WS quote stream (useLiveQuote).
 * When a tick has landed for a symbol, the WS value must win over the
 * REST snapshot; symbols without a tick keep rendering the REST quote.
 * (Polling pause is driven by the same `wsConnected && live` condition
 * that selects the displayed price.)
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const { apiGetMock, wsState } = vi.hoisted(() => ({
  apiGetMock: vi.fn(),
  wsState: {
    connected: true,
    live: {} as Record<string, { price: number; change: number | null; changePct: number | null; volume: number | null; ts: number | null; dataSource: string | null } | undefined>,
  },
}));

vi.mock("@/lib/api", () => ({
  default: { get: apiGetMock },
}));

// Replace the WS layer: no real socket in jsdom. The mock serves
// per-symbol live quotes from `wsState.live`.
vi.mock("@/hooks/useWebSocket", () => ({
  useLiveQuote: (symbol: string) => wsState.live[symbol],
  useWsConnected: () => wsState.connected,
}));

vi.mock("@/store/authStore", () => ({
  useAuthStore: Object.assign(
    (selector?: (s: { user: null; token: null }) => unknown) => {
      const state = { user: null, token: null };
      return selector ? selector(state) : state;
    },
    { getState: () => ({ user: null, token: null }) },
  ),
}));

import DashboardPage from "./DashboardPage";

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <DashboardPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  wsState.connected = true;
  wsState.live = {};
  apiGetMock.mockReset();
  apiGetMock.mockImplementation((url: string) => {
    if (url.startsWith("/us/quote/")) {
      return Promise.resolve({
        data: { price: 500, change_pct: 0.1, ts: Date.now(), data_source: "polygon" },
      });
    }
    return Promise.resolve({ data: [] });
  });
});

describe("DashboardPage IndexCard live-quote precedence", () => {
  it("renders the WS tick over the REST snapshot once one lands", async () => {
    wsState.live["SPY"] = {
      price: 512.34,
      change: 2.1,
      changePct: 0.42,
      volume: null,
      ts: Date.now(),
      dataSource: "ws",
    };
    renderPage();

    // SPY card shows the live price…
    expect(await screen.findByText("512.34")).toBeInTheDocument();
    // …while the other index cards (no tick yet) show the REST price.
    expect((await screen.findAllByText("500.00")).length).toBeGreaterThan(0);
  });

  it("falls back to the REST snapshot when no tick has landed", async () => {
    renderPage();
    // All four index cards render the REST quote (each card's query
    // resolves independently, so wait for all of them).
    await waitFor(() => {
      expect(screen.getAllByText("500.00").length).toBe(4);
    });
  });
});
