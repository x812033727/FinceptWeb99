/**
 * Smoke tests for the FinMind public marketing/catalog page.
 *
 * Light-touch coverage — the page is a thin display layer over
 * /api/finmind/catalog. Tests verify: hero renders, pricing tiers
 * render with disabled CTAs, catalog table populates, search /
 * category filters narrow results, error and loading states show.
 *
 * Mocks the global axios instance to avoid the real network.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import api from "@/lib/api";

import FinmindPage from "./FinmindPage";

vi.mock("@/lib/api", () => ({
  default: { get: vi.fn() },
  errorDetail: (err: unknown) =>
    err instanceof Error ? err.message : String(err),
}));

const mockedApi = api as unknown as {
  get: ReturnType<typeof vi.fn>;
};

const SAMPLE_CATALOG = [
  {
    dataset_code: "TaiwanStockPrice",
    category: "technical",
    description_zh: "台股價量資料",
    ingest_freq: "daily",
    per_symbol: true,
    sponsor_tier: false,
    available: true,
  },
  {
    dataset_code: "TaiwanFuturesDaily",
    category: "derivative",
    description_zh: "台指期日線",
    ingest_freq: "daily",
    per_symbol: true,
    sponsor_tier: true,
    available: false,
  },
  {
    dataset_code: "TaiwanStockMonthRevenue",
    category: "fundamental",
    description_zh: "月營收",
    ingest_freq: "monthly",
    per_symbol: true,
    sponsor_tier: false,
    available: true,
  },
];

function renderPage() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <FinmindPage />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockedApi.get.mockResolvedValue({ data: SAMPLE_CATALOG });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("FinmindPage — render", () => {
  it("renders the hero section + dataset stats", async () => {
    renderPage();
    expect(
      screen.getByText(/FinMind Mirror API/i),
    ).toBeInTheDocument();
    // Stat chips: numbers and labels live in separate spans, so use
    // function matchers that walk the rendered text.
    await waitFor(() => {
      const chip = screen.getByText((_, el) =>
        el?.textContent === "3 個資料集",
      );
      expect(chip).toBeInTheDocument();
    });
    expect(
      screen.getByText((_, el) => el?.textContent === "2 個目前可查"),
    ).toBeInTheDocument();
  });

  it("renders all 3 pricing tiers with disabled CTAs", async () => {
    renderPage();
    // Section header.
    expect(await screen.findByText(/訂閱方案/)).toBeInTheDocument();
    // Tier names.
    expect(screen.getByText(/^Free$/)).toBeInTheDocument();
    expect(screen.getByText(/^Pro$/)).toBeInTheDocument();
    expect(screen.getByText(/^Enterprise$/)).toBeInTheDocument();
    // CTAs are present but disabled (Stripe Checkout not yet wired).
    const ctas = screen
      .getAllByRole("button")
      .filter((b) => b.hasAttribute("disabled"));
    expect(ctas.length).toBeGreaterThanOrEqual(3);
  });

  it("renders the catalog table with seeded rows", async () => {
    renderPage();
    expect(
      await screen.findByText("TaiwanStockPrice"),
    ).toBeInTheDocument();
    expect(screen.getByText("TaiwanFuturesDaily")).toBeInTheDocument();
    expect(
      screen.getByText("TaiwanStockMonthRevenue"),
    ).toBeInTheDocument();
    // Sponsor badge appears on the right row.
    expect(screen.getByText(/sponsor/i)).toBeInTheDocument();
  });

  it("renders the curl quickstart snippet", async () => {
    renderPage();
    expect(await screen.findByText(/快速上手/)).toBeInTheDocument();
    expect(
      screen.getByText(/X-Finmind-API-Key/i),
    ).toBeInTheDocument();
  });
});

describe("FinmindPage — filtering", () => {
  it("search box narrows results by dataset_code substring", async () => {
    renderPage();
    await screen.findByText("TaiwanStockPrice");

    const search = screen.getByPlaceholderText(/搜尋資料集/);
    fireEvent.change(search, { target: { value: "Futures" } });

    await waitFor(() => {
      expect(screen.queryByText("TaiwanStockPrice")).toBeNull();
      expect(screen.getByText("TaiwanFuturesDaily")).toBeInTheDocument();
    });
  });

  it("'only available' toggle hides datasets where available=false", async () => {
    renderPage();
    await screen.findByText("TaiwanFuturesDaily");

    const toggle = screen.getByLabelText(/僅顯示可查/);
    fireEvent.click(toggle);

    await waitFor(() => {
      expect(screen.queryByText("TaiwanFuturesDaily")).toBeNull();
      // The two `available=true` rows still visible.
      expect(screen.getByText("TaiwanStockPrice")).toBeInTheDocument();
    });
  });
});

describe("FinmindPage — error states", () => {
  it("renders an error banner when /catalog fails", async () => {
    mockedApi.get.mockReset();
    mockedApi.get.mockRejectedValue(new Error("backend offline"));
    renderPage();
    expect(
      await screen.findByText(/無法載入目錄/),
    ).toBeInTheDocument();
  });
});
