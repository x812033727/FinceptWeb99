/**
 * StockDetailPage re-render isolation test (blueprint §4.6).
 *
 * The regression this pins down: WS ticks used to drive four page-level
 * setStates, re-rendering the ENTIRE page (K-line container included)
 * on every delta. After PR-6, ticks flow through the rAF-batched
 * quoteStore into <LiveQuoteHeader> only — the page body must not
 * render again when a tick lands.
 *
 * TabStrip is mocked with a render counter as the page-body probe: it
 * is re-created on every page render (tabDefs is rebuilt inline), so a
 * stable count across ticks proves the page function didn't re-run.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, act, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { bufferQuoteUpdate, _resetQuoteStoreForTests } from "@/store/quoteStore";

const { apiGetMock, tabStripRenders } = vi.hoisted(() => ({
  apiGetMock: vi.fn(),
  tabStripRenders: { count: 0 },
}));

vi.mock("@/lib/api", () => ({
  default: {
    get: apiGetMock,
    post: vi.fn().mockResolvedValue({ data: {} }),
    delete: vi.fn().mockResolvedValue({ data: {} }),
  },
}));

// Page-body render probe. Rendered by StockDetailPage on every render
// pass; never re-rendered by React unless the page itself re-renders
// (its props are rebuilt inline each pass, so parent renders always
// propagate here).
vi.mock("@/components/stock/TabStrip", () => ({
  TabStrip: () => {
    tabStripRenders.count++;
    return <div data-testid="tabstrip" />;
  },
}));

// lightweight-charts needs a real canvas; not under test here. The mock
// exposes the bar count so the keepPreviousData test below can assert
// the chart keeps its data while a new period loads.
vi.mock("@/components/charts/CandlestickChart", () => ({
  default: ({ bars }: { bars: unknown[] }) => (
    <div data-testid="chart">{bars.length}</div>
  ),
}));

import StockDetailPage from "./StockDetailPage";

// ── manual rAF queue ──────────────────────────────────────────────

let rafQueue: FrameRequestCallback[] = [];

function runFrame(): void {
  const cbs = rafQueue;
  rafQueue = [];
  cbs.forEach((cb) => cb(performance.now()));
}

beforeEach(() => {
  rafQueue = [];
  tabStripRenders.count = 0;
  apiGetMock.mockReset();
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
    rafQueue.push(cb);
    return rafQueue.length;
  });
  vi.stubGlobal("cancelAnimationFrame", () => {});
});

afterEach(() => {
  _resetQuoteStoreForTests();
  vi.unstubAllGlobals();
});

function mockApi() {
  apiGetMock.mockImplementation((url: string) => {
    if (url.startsWith("/us/quote/")) {
      return Promise.resolve({
        data: {
          price: 180.5,
          change_pct: 1.0,
          name: "Apple Inc.",
          currency: "USD",
          data_source: "polygon",
        },
      });
    }
    if (url.startsWith("/us/earnings/")) {
      return Promise.resolve({
        data: { earnings_date: null, eps_estimate: null, revenue_estimate: null },
      });
    }
    // history / fundamentals / anything else
    return Promise.resolve({ data: url.includes("/history/") ? [] : {} });
  });
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/stock/US/AAPL"]}>
        <Routes>
          <Route path="/stock/:market/:symbol" element={<StockDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("StockDetailPage tick isolation", () => {
  it("updates the live price header WITHOUT re-rendering the page body", async () => {
    mockApi();
    renderPage();

    // Initial paint from the REST snapshot
    expect(await screen.findByText("180.50")).toBeInTheDocument();
    // Let the remaining queries (history/fundamentals/earnings) settle
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    const bodyRendersBefore = tabStripRenders.count;
    expect(bodyRendersBefore).toBeGreaterThan(0);

    // Simulate a WS tick landing in the quoteStore buffer + rAF flush
    act(() => {
      bufferQuoteUpdate("AAPL:US", { price: 182.25, change_pct: 1.4 });
      runFrame();
    });

    // Header went live…
    expect(screen.getByText("182.25")).toBeInTheDocument();
    expect(screen.getByText("+1.40%")).toBeInTheDocument();
    // …and the page body did not render again
    expect(tabStripRenders.count).toBe(bodyRendersBefore);

    // A second burst of ticks in one frame: still zero body re-renders
    act(() => {
      bufferQuoteUpdate("AAPL:US", { price: 182.5 });
      bufferQuoteUpdate("AAPL:US", { price: 182.75 });
      runFrame();
    });
    expect(screen.getByText("182.75")).toBeInTheDocument();
    expect(tabStripRenders.count).toBe(bodyRendersBefore);
  });
});

// ── PR-9: history keepPreviousData ────────────────────────────────

function makeBars(n: number) {
  return Array.from({ length: n }, (_, i) => ({
    time: `2026-01-0${i + 1}`,
    open: 100 + i,
    high: 101 + i,
    low: 99 + i,
    close: 100.5 + i,
    volume: 1000,
  }));
}

describe("StockDetailPage history keepPreviousData", () => {
  it("keeps the previous period's bars on screen while the new period loads", async () => {
    let resolve5d: ((v: { data: unknown }) => void) | undefined;
    apiGetMock.mockImplementation((url: string) => {
      if (url.startsWith("/us/quote/")) {
        return Promise.resolve({
          data: { price: 180.5, change_pct: 1.0, name: "Apple Inc.", currency: "USD", data_source: "polygon" },
        });
      }
      if (url.startsWith("/us/earnings/")) {
        return Promise.resolve({
          data: { earnings_date: null, eps_estimate: null, revenue_estimate: null },
        });
      }
      if (url.includes("/history/")) {
        // Default period (1y) resolves immediately with 3 bars; the 5d
        // request stays pending until the test releases it.
        if (url.includes("period=5d")) {
          return new Promise((res) => { resolve5d = res; });
        }
        return Promise.resolve({ data: makeBars(3) });
      }
      return Promise.resolve({ data: {} });
    });

    renderPage();
    expect(await screen.findByTestId("chart")).toHaveTextContent("3");

    // Switch period → new queryKey. Without placeholderData the chart
    // would unmount into the loading state; with keepPreviousData the
    // old 3 bars must stay on screen.
    fireEvent.click(screen.getByText("5d"));
    expect(screen.getByTestId("chart")).toHaveTextContent("3");
    expect(screen.queryByText("No data available")).toBeNull();

    // Release the 5d response — the chart swaps to the new bars.
    await act(async () => {
      resolve5d!({ data: makeBars(7) });
    });
    await waitFor(() => {
      expect(screen.getByTestId("chart")).toHaveTextContent("7");
    });
  });
});
