/**
 * WatchlistPage virtualization smoke test (PR-9).
 *
 * A watchlist can hold hundreds of symbols; the item rows are
 * virtualized with @tanstack/react-virtual (ScreenerPage pattern).
 * We render a 200-item list and assert only a window of rows is
 * mounted in the DOM.
 */
import { describe, it, expect, beforeAll, afterAll, beforeEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const { apiGetMock } = vi.hoisted(() => ({ apiGetMock: vi.fn() }));

vi.mock("@/lib/api", () => ({
  default: {
    get: apiGetMock,
    post: vi.fn().mockResolvedValue({ data: {} }),
    delete: vi.fn().mockResolvedValue({ data: {} }),
  },
}));

import WatchlistPage from "./WatchlistPage";

const ITEM_COUNT = 200;

function makeWatchlist() {
  return [
    {
      id: "wl-1",
      name: "Big list",
      created_at: "2026-01-01T00:00:00Z",
      items: Array.from({ length: ITEM_COUNT }, (_, i) => ({
        id: `item-${i}`,
        symbol: `SYM${i}`,
        market: "US",
        added_at: "2026-01-01T00:00:00Z",
        price: 100 + i,
        change_pct: 1.5,
        name: `Symbol ${i}`,
        quoted_at: "2026-01-01T00:00:00Z",
        data_source: "polygon",
      })),
    },
  ];
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <WatchlistPage />
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
    if (url.startsWith("/watchlist")) {
      return Promise.resolve({ data: makeWatchlist() });
    }
    return Promise.resolve({ data: [] });
  });
});

describe("WatchlistPage virtualization", () => {
  it("mounts only a window of a 200-item list", async () => {
    const { container } = renderPage();

    // List title renders with the full count…
    expect(await screen.findByText("(200)")).toBeInTheDocument();

    // …but only a virtualized subset of rows is in the DOM. The
    // virtualizer measures the scroll element in a layout effect, so
    // give the resulting re-render a tick to land.
    await waitFor(() => {
      const rows = container.querySelectorAll(
        '[data-testid="watchlist-virtual-scroll"] [data-index]',
      );
      expect(rows.length).toBeGreaterThan(0);
      expect(rows.length).toBeLessThan(50);
    });
  });

  it("keeps row content (symbol link, price, change) for mounted rows", async () => {
    const { container } = renderPage();
    await screen.findByText("(200)");

    const firstRow = await waitFor(() => {
      const row = container.querySelector(
        '[data-testid="watchlist-virtual-scroll"] [data-index]',
      ) as HTMLElement;
      expect(row).not.toBeNull();
      return row;
    });

    const idx = Number(firstRow.dataset.index);
    const link = firstRow.querySelector("a") as HTMLAnchorElement;
    expect(link.textContent).toBe(`SYM${idx}`);
    expect(link.getAttribute("href")).toBe(`/stock/US/SYM${idx}`);
    expect(firstRow.textContent).toContain((100 + idx).toFixed(2));
    expect(firstRow.textContent).toContain("+1.50%");
  });
});
