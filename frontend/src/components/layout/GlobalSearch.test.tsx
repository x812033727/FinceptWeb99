/**
 * Unit tests for GlobalSearch.
 *
 * Covers:
 *   - Empty input → no API call, dropdown hidden.
 *   - Typed input → debounced (300 ms) → 3 parallel api.get calls
 *     fan out, then results merge in US/TW/CRYPTO order capped at 10.
 *   - Promise.allSettled: a single market failing doesn't drop the
 *     other two.
 *   - Hover / focus on a result → prefetchPage(`/stock/MARKET/SYM`).
 *   - Keyboard nav: ArrowDown / ArrowUp wrap, Enter selects (navigate),
 *     Escape closes.
 *   - mousedown on a result navigates immediately (preventDefault so
 *     focus doesn't shift before navigate).
 *   - Click outside closes the dropdown.
 */
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { navigateMock, prefetchPageMock, apiGetMock } = vi.hoisted(() => ({
  navigateMock: vi.fn(),
  prefetchPageMock: vi.fn(),
  apiGetMock: vi.fn(),
}));

vi.mock("react-router-dom", () => ({
  useNavigate: () => navigateMock,
}));

vi.mock("@/pageLoaders", () => ({
  prefetchPage: prefetchPageMock,
}));

vi.mock("@/lib/api", () => ({
  default: { get: apiGetMock },
}));

import GlobalSearch from "./GlobalSearch";

beforeEach(() => {
  vi.useFakeTimers();
  vi.clearAllMocks();
});

afterEach(() => {
  vi.useRealTimers();
});

// ── Helpers ──────────────────────────────────────────────────────

/** Drive the debounce + the three async api.get calls, then let the
 *  scheduled state-updates flush. Wraps in act() so React doesn't
 *  yell about state updates outside act. */
async function flushDebounceAndFetches() {
  await act(async () => {
    vi.advanceTimersByTime(300);
    // Two microtasks: one for Promise.allSettled, one for the
    // setState batch after the await.
    await Promise.resolve();
    await Promise.resolve();
  });
}

function setApiResults({
  us = [], tw = [], crypto = [],
}: { us?: unknown[]; tw?: unknown[]; crypto?: unknown[] }) {
  apiGetMock.mockImplementation((url: string) => {
    if (url.startsWith("/us/search"))     return Promise.resolve({ data: us });
    if (url.startsWith("/tw/search"))     return Promise.resolve({ data: tw });
    if (url.startsWith("/crypto/search")) return Promise.resolve({ data: crypto });
    return Promise.reject(new Error(`unexpected url: ${url}`));
  });
}

// ── Empty input ──────────────────────────────────────────────────

describe("GlobalSearch — empty input", () => {
  it("does not call the API when the query is empty", async () => {
    render(<GlobalSearch />);
    await flushDebounceAndFetches();
    expect(apiGetMock).not.toHaveBeenCalled();
  });

  it("clears + closes the dropdown when query goes back to empty", async () => {
    setApiResults({ us: [{ symbol: "AAPL", market: "US" }] });
    render(<GlobalSearch />);
    const input = screen.getByPlaceholderText(/search/i);

    fireEvent.change(input, { target: { value: "AAPL" } });
    await flushDebounceAndFetches();
    expect(screen.getByText("AAPL")).toBeInTheDocument();

    fireEvent.change(input, { target: { value: "" } });
    await flushDebounceAndFetches();
    expect(screen.queryByText("AAPL")).not.toBeInTheDocument();
  });
});

// ── Fan-out + merge ──────────────────────────────────────────────

describe("GlobalSearch — fan-out", () => {
  it("queries US / TW / crypto in parallel and merges in that order", async () => {
    setApiResults({
      us:     [{ symbol: "AAPL", market: "US" }],
      tw:     [{ symbol: "2330", market: "TW" }],
      crypto: [{ symbol: "BTC",  market: "CRYPTO" }],
    });
    render(<GlobalSearch />);
    fireEvent.change(screen.getByPlaceholderText(/search/i), { target: { value: "X" } });
    await flushDebounceAndFetches();

    // 3 parallel calls fired.
    expect(apiGetMock).toHaveBeenCalledTimes(3);

    const buttons = screen.getAllByRole("button");
    // Order matches the merge: US → TW → CRYPTO.
    expect(buttons[0]).toHaveTextContent("AAPL");
    expect(buttons[1]).toHaveTextContent("2330");
    expect(buttons[2]).toHaveTextContent("BTC");
  });

  it("keeps the surviving markets when one market's request fails", async () => {
    apiGetMock.mockImplementation((url: string) => {
      if (url.startsWith("/us/search"))     return Promise.reject(new Error("US 502"));
      if (url.startsWith("/tw/search"))     return Promise.resolve({ data: [{ symbol: "2330", market: "TW" }] });
      if (url.startsWith("/crypto/search")) return Promise.resolve({ data: [{ symbol: "BTC",  market: "CRYPTO" }] });
      return Promise.reject(new Error("unexpected"));
    });

    render(<GlobalSearch />);
    fireEvent.change(screen.getByPlaceholderText(/search/i), { target: { value: "X" } });
    await flushDebounceAndFetches();

    // US dropped via Promise.allSettled, but TW + CRYPTO still surface.
    expect(screen.queryByText("AAPL")).not.toBeInTheDocument();
    expect(screen.getByText("2330")).toBeInTheDocument();
    expect(screen.getByText("BTC")).toBeInTheDocument();
  });

  it("caps combined results at 10 entries", async () => {
    setApiResults({
      us: Array.from({ length: 10 }, (_, i) => ({ symbol: `US${i}`, market: "US" })),
      tw: Array.from({ length: 10 }, (_, i) => ({ symbol: `TW${i}`, market: "TW" })),
    });
    render(<GlobalSearch />);
    fireEvent.change(screen.getByPlaceholderText(/search/i), { target: { value: "X" } });
    await flushDebounceAndFetches();

    expect(screen.getAllByRole("button")).toHaveLength(10);
  });
});

// ── Hover / focus prefetch ───────────────────────────────────────

describe("GlobalSearch — prefetch", () => {
  it("hovering a result warms the corresponding stock-detail chunk", async () => {
    setApiResults({ us: [{ symbol: "AAPL", market: "US" }] });
    render(<GlobalSearch />);
    fireEvent.change(screen.getByPlaceholderText(/search/i), { target: { value: "A" } });
    await flushDebounceAndFetches();

    fireEvent.mouseEnter(screen.getByRole("button", { name: /AAPL/ }));
    expect(prefetchPageMock).toHaveBeenCalledWith("/stock/US/AAPL");
  });

  it("focusing a result also prefetches (keyboard users)", async () => {
    setApiResults({ tw: [{ symbol: "2330", market: "TW" }] });
    render(<GlobalSearch />);
    fireEvent.change(screen.getByPlaceholderText(/search/i), { target: { value: "2" } });
    await flushDebounceAndFetches();

    fireEvent.focus(screen.getByRole("button", { name: /2330/ }));
    expect(prefetchPageMock).toHaveBeenCalledWith("/stock/TW/2330");
  });
});

// ── Keyboard navigation ──────────────────────────────────────────

describe("GlobalSearch — keyboard navigation", () => {
  async function setupWithThreeResults() {
    setApiResults({
      us: [{ symbol: "A", market: "US" }, { symbol: "B", market: "US" }],
      tw: [{ symbol: "C", market: "TW" }],
    });
    render(<GlobalSearch />);
    const input = screen.getByPlaceholderText(/search/i);
    fireEvent.change(input, { target: { value: "X" } });
    await flushDebounceAndFetches();
    return input;
  }

  it("ArrowDown wraps from last back to first", async () => {
    // Initial active = 0. With 3 results, 3 ArrowDowns walks
    // 0 → 1 → 2 → 0 (wrap). The active row is highlighted via the
    // `bg-accent/20` class on its button.
    const input = await setupWithThreeResults();
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "ArrowDown" });
    const buttons = screen.getAllByRole("button");
    expect(buttons[0]).toHaveClass("bg-accent/20");
  });

  it("ArrowUp from first wraps to last", async () => {
    const input = await setupWithThreeResults();
    fireEvent.keyDown(input, { key: "ArrowUp" });
    const buttons = screen.getAllByRole("button");
    expect(buttons[buttons.length - 1]).toHaveClass("bg-accent/20");
  });

  it("Enter on highlighted result navigates to its stock-detail page", async () => {
    const input = await setupWithThreeResults();
    fireEvent.keyDown(input, { key: "ArrowDown" }); // active=1 (B/US)
    fireEvent.keyDown(input, { key: "Enter" });
    expect(navigateMock).toHaveBeenCalledWith("/stock/US/B");
  });

  it("Escape closes the dropdown without navigating", async () => {
    const input = await setupWithThreeResults();
    expect(screen.getAllByRole("button").length).toBeGreaterThan(0);
    fireEvent.keyDown(input, { key: "Escape" });
    expect(screen.queryAllByRole("button")).toHaveLength(0);
    expect(navigateMock).not.toHaveBeenCalled();
  });

  it("ignores arrow keys when the dropdown is closed", async () => {
    render(<GlobalSearch />);
    fireEvent.keyDown(screen.getByPlaceholderText(/search/i), { key: "ArrowDown" });
    expect(navigateMock).not.toHaveBeenCalled();
    expect(apiGetMock).not.toHaveBeenCalled();
  });
});

// ── Mouse selection ──────────────────────────────────────────────

describe("GlobalSearch — mouse selection", () => {
  it("mousedown on a result navigates immediately and clears the query", async () => {
    setApiResults({ us: [{ symbol: "AAPL", market: "US" }] });
    render(<GlobalSearch />);
    const input = screen.getByPlaceholderText(/search/i) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "AAPL" } });
    await flushDebounceAndFetches();

    fireEvent.mouseDown(screen.getByRole("button", { name: /AAPL/ }));
    expect(navigateMock).toHaveBeenCalledWith("/stock/US/AAPL");
    expect(input.value).toBe("");
  });
});

// ── Click outside ────────────────────────────────────────────────

describe("GlobalSearch — click outside", () => {
  it("closes the dropdown when clicking outside the input + dropdown", async () => {
    setApiResults({ us: [{ symbol: "AAPL", market: "US" }] });
    render(<GlobalSearch />);
    fireEvent.change(screen.getByPlaceholderText(/search/i), { target: { value: "A" } });
    await flushDebounceAndFetches();
    expect(screen.getByText("AAPL")).toBeInTheDocument();

    // Mousedown anywhere outside the input + dropdown closes it.
    fireEvent.mouseDown(document.body);
    expect(screen.queryByText("AAPL")).not.toBeInTheDocument();
  });
});
