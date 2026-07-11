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
import { render, screen, act } from "@testing-library/react";
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

// lightweight-charts needs a real canvas; not under test here.
vi.mock("@/components/charts/CandlestickChart", () => ({
  default: () => <div data-testid="chart" />,
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
