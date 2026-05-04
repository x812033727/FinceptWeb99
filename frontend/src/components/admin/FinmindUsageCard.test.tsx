/**
 * Tests for FinmindUsageCard.
 *
 * Recharts is rendered into a `<svg>`; RTL text queries on it work
 * fine for axis labels but not for bar values themselves. Test the
 * rendering shell + range selector + empty-state copy + that the
 * range selector actually changes the query key (which determines
 * the URL fetched).
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import api from "@/lib/api";

import FinmindUsageCard from "./FinmindUsageCard";

vi.mock("@/lib/api", () => ({
  default: {
    get: vi.fn(),
    patch: vi.fn(),
    post: vi.fn(),
    delete: vi.fn(),
  },
}));

const mockedApi = api as unknown as {
  get: ReturnType<typeof vi.fn>;
};


function renderCard() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <FinmindUsageCard />
    </QueryClientProvider>,
  );
}


beforeEach(() => {
  // Open by default — same trick as the AdminCard test.
  localStorage.setItem("admin-finmind-usage", "1");
});

afterEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
});


describe("FinmindUsageCard — empty state", () => {
  it("renders the empty-state hint when by_day is empty", async () => {
    mockedApi.get.mockResolvedValue({
      data: { window_days: 7, by_day: [], by_dataset: [] },
    });
    renderCard();
    expect(
      await screen.findByText(/No requests in the last 7 days yet/i),
    ).toBeInTheDocument();
    // Hint includes the curl-able endpoint name so operators know
    // what to test.
    expect(
      screen.getByText(/\/api\/finmind\/data\/\.\.\./i),
    ).toBeInTheDocument();
  });
});


describe("FinmindUsageCard — populated state", () => {
  const POPULATED = {
    window_days: 7,
    by_day: [
      { day: "2026-05-01", calls: 100, rows: 5_000 },
      { day: "2026-05-02", calls: 250, rows: 12_500 },
    ],
    by_dataset: [
      { dataset_code: "TaiwanStockPrice", calls: 200, rows: 10_000 },
      { dataset_code: "TaiwanStockMarginPurchaseShortSale", calls: 150, rows: 7_500 },
    ],
  };

  it("renders the chart container + top-dataset table", async () => {
    mockedApi.get.mockResolvedValue({ data: POPULATED });
    renderCard();

    expect(
      await screen.findByText(/Calls \+ rows per UTC day/i),
    ).toBeInTheDocument();
    // Top datasets table shows dataset codes
    expect(screen.getByText("TaiwanStockPrice")).toBeInTheDocument();
    expect(
      screen.getByText("TaiwanStockMarginPurchaseShortSale"),
    ).toBeInTheDocument();
  });

  it("formats large numbers in the subtitle (K / M)", async () => {
    mockedApi.get.mockResolvedValue({ data: POPULATED });
    renderCard();

    // Header subtitle aggregates totals — 350 calls + 17500 rows
    // → "350 calls · 17.5K rows · last 7d"
    await waitFor(() => {
      const subtitle = screen.getByText(/350 calls · 17\.5K rows/i);
      expect(subtitle).toBeInTheDocument();
    });
  });
});


describe("FinmindUsageCard — range selector", () => {
  it("refetches with the new days param when range changes", async () => {
    mockedApi.get.mockResolvedValue({
      data: { window_days: 7, by_day: [], by_dataset: [] },
    });
    renderCard();

    // Wait for the initial 7d fetch.
    await waitFor(() => {
      expect(mockedApi.get).toHaveBeenCalledWith(
        "/admin/finmind/usage?days=7",
      );
    });

    // Click 30d.
    const thirty = screen.getByRole("button", { name: "30d" });
    fireEvent.click(thirty);

    await waitFor(() => {
      expect(mockedApi.get).toHaveBeenCalledWith(
        "/admin/finmind/usage?days=30",
      );
    });
  });
});


describe("FinmindUsageCard — error state", () => {
  it("surfaces the error message inside the card", async () => {
    mockedApi.get.mockRejectedValue(new Error("backend down"));
    renderCard();
    expect(
      await screen.findByText(/Failed to load usage/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/backend down/i)).toBeInTheDocument();
  });
});
