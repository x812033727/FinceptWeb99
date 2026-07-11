/**
 * MarketPage virtualization smoke test (PR-9).
 *
 * The TW screener returns up to 200 rows; the table is virtualized
 * with @tanstack/react-virtual (ScreenerPage pattern). We render 200
 * rows and assert only a window of them is mounted in the DOM.
 */
import { describe, it, expect, beforeAll, afterAll, beforeEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const { apiGetMock } = vi.hoisted(() => ({ apiGetMock: vi.fn() }));

vi.mock("@/lib/api", () => ({
  default: {
    get: apiGetMock,
    post: vi.fn().mockResolvedValue({ data: {} }),
  },
}));

import MarketPage from "./MarketPage";

const ROW_COUNT = 200;

function makeTWScreener() {
  return Array.from({ length: ROW_COUNT }, (_, i) => ({
    symbol: `${1000 + i}`,
    market: "TW",
    exchange: "TWSE",
    name_zh: `台股${i}`,
    price: 50 + i,
    change_pct: 0.5,
    volume: 1_000_000,
    data_source: "twse",
  }));
}

function renderAt(url: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[url]}>
        <Routes>
          <Route path="/market/:market" element={<MarketPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

// jsdom has no layout — offsetHeight (which @tanstack/virtual-core uses
// for both the scroll rect and measureElement) is always 0, so the
// virtualizer would mount nothing. Pin it to the row height (44px, same
// as estimateSize) so measurements are deterministic.
const originalOffsetHeight = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetHeight");
const originalOffsetWidth = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetWidth");
beforeAll(() => {
  Object.defineProperties(HTMLElement.prototype, {
    offsetHeight: { get: () => 44, configurable: true },
    offsetWidth: { get: () => 800, configurable: true },
  });
});

afterAll(() => {
  if (originalOffsetHeight) Object.defineProperty(HTMLElement.prototype, "offsetHeight", originalOffsetHeight);
  if (originalOffsetWidth) Object.defineProperty(HTMLElement.prototype, "offsetWidth", originalOffsetWidth);
});

beforeEach(() => {
  apiGetMock.mockReset();
  apiGetMock.mockImplementation((url: string) => {
    if (url.startsWith("/tw/screener")) {
      return Promise.resolve({ data: makeTWScreener() });
    }
    if (url.startsWith("/tw/indices")) {
      return Promise.resolve({ data: { index: "TAIEX", value: null, change: null, time: null } });
    }
    return Promise.resolve({ data: [] });
  });
});

describe("MarketPage virtualization", () => {
  it("mounts only a window of 200 screener rows", async () => {
    const { container } = renderAt("/market/TW");

    // Wait for the query to land — the scroll container appears once
    // isLoading flips and rows exist.
    const scroller = await screen.findByTestId("market-virtual-scroll");

    // The virtualizer measures the scroll element in a layout effect,
    // so give the resulting re-render a tick to land.
    const rows = await waitFor(() => {
      const mounted = scroller.querySelectorAll("[data-index]");
      expect(mounted.length).toBeGreaterThan(0);
      return mounted;
    });
    expect(rows.length).toBeLessThan(50);

    // The spacer advertises the full list height (200 x 44 px) so the
    // scrollbar reflects all rows even though few are mounted.
    const spacer = scroller.firstElementChild as HTMLElement;
    expect(spacer.style.height).toBe(`${ROW_COUNT * 44}px`);

    // Sanity: mounted rows render real cell content.
    const firstRow = rows[0] as HTMLElement;
    const idx = Number(firstRow.dataset.index);
    expect(firstRow.textContent).toContain(`${1000 + idx}`);
    expect(firstRow.textContent).toContain(`台股${idx}`);
    expect(container.querySelectorAll("[data-index]").length).toBeLessThan(ROW_COUNT);
  });
});
