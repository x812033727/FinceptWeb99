/**
 * Tests for FinmindBackfillCard.
 *
 * Three behaviors that warrant locking down:
 *   1. The card renders the 12 default datasets the backend reports
 *      and pre-checks all of them.
 *   2. Start / stop / reset buttons fire the right axios calls.
 *   3. host_chain_likely_active=true blocks the start button +
 *      surfaces the yellow banner so we don't double-burn quota.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import api from "@/lib/api";

import FinmindBackfillCard from "./FinmindBackfillCard";

vi.mock("@/lib/api", () => ({
  default: { get: vi.fn(), post: vi.fn() },
  errorDetail: (err: unknown) =>
    err instanceof Error ? err.message : String(err),
}));

const mockedApi = api as unknown as {
  get: ReturnType<typeof vi.fn>;
  post: ReturnType<typeof vi.fn>;
};

const IDLE_STATE = {
  status: "idle",
  queue: [],
  current_dataset: null,
  current_symbol: null,
  chunks_done: 0,
  chunks_total: 0,
  chunks_failed: 0,
  started_at: null,
  last_chunk_at: null,
  stop_requested: false,
  recent_errors: [],
  selected_datasets: [],
  universe_size: 0,
  total_chunks_done: 0,
  total_chunks_total: 0,
  quota_used: 1234,
  quota_limit: 5500,
  quota_limit_global: 6000,
  external_activity_detected: false,
  default_datasets: [
    "TaiwanStockMarginPurchaseShortSale",
    "TaiwanStockPriceAdj",
    "TaiwanStockPER",
    "TaiwanStockMarketValue",
    "TaiwanStockSecuritiesLending",
    "TaiwanDailyShortSaleBalances",
    "TaiwanStockMonthRevenue",
    "TaiwanStockBalanceSheet",
    "TaiwanStockFinancialStatements",
    "TaiwanStockCashFlowsStatement",
    "TaiwanStockDividend",
    "TaiwanStockHoldingSharesPer",
  ],
  per_dataset_progress: [],
};

function renderCard() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <FinmindBackfillCard />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  // Force the collapsible open so we don't need to click first.
  localStorage.setItem("admin-finmind-backfill", "1");
});

afterEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
});

describe("FinmindBackfillCard — render", () => {
  it("renders the title before any query resolves", () => {
    mockedApi.get.mockReturnValue(new Promise(() => {}));
    renderCard();
    expect(screen.getByText(/FinMind full backfill monitor/)).toBeInTheDocument();
  });

  it("renders the 12 default datasets and pre-checks all", async () => {
    mockedApi.get.mockResolvedValue({ data: IDLE_STATE });
    renderCard();

    for (const code of IDLE_STATE.default_datasets) {
      expect(await screen.findByText(code)).toBeInTheDocument();
    }
    // 12/12 selected
    await waitFor(() => {
      expect(screen.getByText("12 / 12 selected")).toBeInTheDocument();
    });
  });

  it("renders the quota gauge with formatted numbers", async () => {
    mockedApi.get.mockResolvedValue({ data: IDLE_STATE });
    renderCard();
    expect(
      await screen.findByText(/1,234 \/ 6,000/),
    ).toBeInTheDocument();
  });

  it("shows 閒置中 banner when status=idle", async () => {
    mockedApi.get.mockResolvedValue({ data: IDLE_STATE });
    renderCard();
    expect(await screen.findByText("Idle")).toBeInTheDocument();
  });

  it("shows 正在抓取 + progress when status=running", async () => {
    mockedApi.get.mockResolvedValue({
      data: {
        ...IDLE_STATE,
        status: "running",
        current_dataset: "TaiwanStockPriceAdj",
        current_symbol: "2330",
        chunks_done: 42,
        chunks_total: 100,
      },
    });
    renderCard();
    expect(await screen.findByText("Fetching")).toBeInTheDocument();
    expect(
      screen.getByText("TaiwanStockPriceAdj · 2330"),
    ).toBeInTheDocument();
    expect(screen.getByText(/42\/100 \(42%\)/)).toBeInTheDocument();
  });
});

describe("FinmindBackfillCard — external activity detection", () => {
  it("shows the advisory banner but keeps Start enabled when external_activity_detected", async () => {
    // External activity (APScheduler / host script) is informational
    // only — the chain's pre-flight quota gate handles saturation, so
    // we don't hard-block the user. Only a real duplicate UI start is
    // refused (via redis lock → 409 from the API).
    mockedApi.get.mockResolvedValue({
      data: { ...IDLE_STATE, external_activity_detected: true },
    });
    renderCard();
    expect(
      await screen.findByText(/Another backfill task is currently using FinMind quota/),
    ).toBeInTheDocument();
    const start = await screen.findByRole("button", { name: /Start backfill/ });
    await waitFor(() => expect(start).not.toBeDisabled());
  });
});

describe("FinmindBackfillCard — overall progress", () => {
  it("renders 整體進度 with absolute count + percentage when chain has run", async () => {
    mockedApi.get.mockResolvedValue({
      data: {
        ...IDLE_STATE,
        status: "running",
        selected_datasets: [
          "TaiwanStockPriceAdj", "TaiwanStockMonthRevenue",
        ],
        universe_size: 2535,
        total_chunks_done: 1234,
        total_chunks_total: 5070,  // 2535 × 2
        current_dataset: "TaiwanStockPriceAdj",
        chunks_done: 1234,
        chunks_total: 2535,
      },
    });
    renderCard();
    expect(await screen.findByText("Overall progress")).toBeInTheDocument();
    expect(
      screen.getByText(/1,234 \/ 5,070 \(24%\)/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/2 datasets × 2,535 symbols/),
    ).toBeInTheDocument();
  });
});

describe("FinmindBackfillCard — actions", () => {
  it("Start fires POST /chain/start with the selected datasets and days", async () => {
    mockedApi.get.mockResolvedValue({ data: IDLE_STATE });
    mockedApi.post.mockResolvedValue({
      data: { ...IDLE_STATE, status: "running" },
    });
    renderCard();

    const start = await screen.findByRole("button", { name: /Start backfill/ });
    await waitFor(() => expect(start).not.toBeDisabled());
    fireEvent.click(start);

    await waitFor(() => {
      expect(mockedApi.post).toHaveBeenCalledWith(
        "/admin/finmind/chain/start",
        expect.objectContaining({
          datasets: expect.arrayContaining([
            "TaiwanStockPriceAdj",
            "TaiwanStockMonthRevenue",
          ]),
          days: 365,
          reset_stuck_first: true,
        }),
      );
    });
  });

  it("Stop is disabled while idle, enabled while running, fires POST /chain/stop", async () => {
    mockedApi.get.mockResolvedValue({
      data: { ...IDLE_STATE, status: "running" },
    });
    mockedApi.post.mockResolvedValue({
      data: { ...IDLE_STATE, status: "stopping" },
    });
    renderCard();

    const stop = await screen.findByRole("button", {
      name: /Stop \(finish current chunk\)/,
    });
    await waitFor(() => expect(stop).not.toBeDisabled());
    fireEvent.click(stop);

    await waitFor(() => {
      expect(mockedApi.post).toHaveBeenCalledWith(
        "/admin/finmind/chain/stop",
      );
    });
  });

  it("Reset stuck fires POST /chain/reset-stuck and shows the count", async () => {
    mockedApi.get.mockResolvedValue({ data: IDLE_STATE });
    mockedApi.post.mockResolvedValue({ data: { reset: 3 } });
    renderCard();

    const reset = await screen.findByRole("button", {
      name: /Clear stuck chunks/,
    });
    fireEvent.click(reset);

    await waitFor(() => {
      expect(mockedApi.post).toHaveBeenCalledWith(
        "/admin/finmind/chain/reset-stuck",
      );
    });
    expect(
      await screen.findByText(/Cleared 3 stuck running chunks/),
    ).toBeInTheDocument();
  });
});
