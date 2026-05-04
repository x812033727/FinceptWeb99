/**
 * Tests for FinmindAdminCard.
 *
 * Two layers covered:
 *   1. Empty + error states render gracefully without exploding the
 *      AdminPage layout (this card is one of many — a render crash
 *      here cascades).
 *   2. Catalog row interactions (toggle enabled, change source, run)
 *      fire the right axios calls — verifies the UI is wired to the
 *      proxy endpoints in `api/admin/finmind_proxy.py`.
 *
 * `api` (the global axios instance) is mocked so tests don't need a
 * running backend; useQuery returns the mocked data synchronously
 * via React Query's normal async flow.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import api from "@/lib/api";

import FinmindAdminCard from "./FinmindAdminCard";

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
  patch: ReturnType<typeof vi.fn>;
  post: ReturnType<typeof vi.fn>;
  delete: ReturnType<typeof vi.fn>;
};


function renderCard() {
  // Fresh QueryClient per test — keeps caches isolated so a stale
  // mutation from one test doesn't leak into the next.
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <FinmindAdminCard />
    </QueryClientProvider>,
  );
}

const SAMPLE_DATASET = {
  dataset_code: "TaiwanStockPrice",
  category: "technical",
  description_zh: "台灣股價資料表",
  local_table: "ohlcv_daily",
  per_symbol: true,
  primary_source: "finmind",
  fallback_source: "twse",
  active_source: "finmind",
  enabled: false,
  sponsor_tier: false,
  ingest_freq: "daily",
  last_ingest_at: null,
  last_ingest_rows: null,
  last_error: null,
};

const SAMPLE_STATUS = {
  alembic: { at_head: true, current: "0011", expected: "0011" },
  catalog: { seeded: 80, expected: 80, ok: true },
  phase1_coverage: {
    technical: { built: 13, total: 20 },
    chip: { built: 19, total: 20 },
  },
  active_ingestion: { enabled: 0, by_category: {} },
  backfill: {
    pending: 0, running: 0, done: 0, failed: 0, skipped: 0,
    stuck_running: 0,
  },
  recent_errors: [],
  generated_at: "2026-05-04T00:00:00+00:00",
};


beforeEach(() => {
  // Each test starts with the card open via localStorage so we don't
  // have to click the toggle just to render the body. The Collapsible
  // hook reads "1" from localStorage as "open".
  localStorage.setItem("admin-finmind", "1");
});

afterEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
});


describe("FinmindAdminCard — render", () => {
  it("renders the title even before queries resolve", () => {
    mockedApi.get.mockReturnValue(new Promise(() => {})); // never resolves
    renderCard();
    expect(screen.getByText("FinMind Clone Subsystem")).toBeInTheDocument();
  });

  it("renders the helpful error banner when the status query fails", async () => {
    mockedApi.get.mockRejectedValue(new Error("network down"));
    renderCard();
    expect(
      await screen.findByText(/Failed to load FinMind status/i),
    ).toBeInTheDocument();
    // Operator hint with the docker compose command
    expect(
      screen.getByText(/postgres_finmind/i),
    ).toBeInTheDocument();
  });

  it("renders status banner + dataset table on success", async () => {
    mockedApi.get.mockImplementation((url: string) => {
      if (url === "/admin/finmind/status") {
        return Promise.resolve({ data: SAMPLE_STATUS });
      }
      if (url === "/admin/finmind/datasets") {
        return Promise.resolve({ data: [SAMPLE_DATASET] });
      }
      return Promise.reject(new Error(`unexpected url: ${url}`));
    });

    renderCard();

    // Status section
    expect(await screen.findByText(/Migrations \+ Catalog/i)).toBeInTheDocument();
    expect(screen.getByText(/Phase 1 Schema Coverage/i)).toBeInTheDocument();

    // Dataset table cell
    expect(await screen.findByText("TaiwanStockPrice")).toBeInTheDocument();
    expect(screen.getByText("ohlcv_daily")).toBeInTheDocument();
  });
});


describe("FinmindAdminCard — interactions", () => {
  beforeEach(() => {
    mockedApi.get.mockImplementation((url: string) => {
      if (url === "/admin/finmind/status") {
        return Promise.resolve({ data: SAMPLE_STATUS });
      }
      if (url === "/admin/finmind/datasets") {
        return Promise.resolve({ data: [SAMPLE_DATASET] });
      }
      return Promise.reject(new Error(`unexpected url: ${url}`));
    });
  });

  it("PATCHes when the enabled checkbox is toggled", async () => {
    mockedApi.patch.mockResolvedValue({
      data: { ...SAMPLE_DATASET, enabled: true },
    });

    renderCard();
    const checkbox = await screen.findByRole("checkbox", { name: "" });
    // Filter row also has a checkbox ("Only enabled") — find the one
    // inside the table row by its initial unchecked state on the
    // dataset row. The "Only enabled" filter checkbox is unchecked too
    // by default, so disambiguate by counting expected checkboxes:
    // 1 filter + 1 per dataset row = 2 here. Dataset row is the
    // second one.
    const checkboxes = screen.getAllByRole("checkbox");
    expect(checkboxes).toHaveLength(2);
    const datasetCheckbox = checkboxes[1];

    fireEvent.click(datasetCheckbox);

    await waitFor(() => {
      expect(mockedApi.patch).toHaveBeenCalledWith(
        "/admin/finmind/datasets/TaiwanStockPrice",
        { enabled: true },
      );
    });
    expect(checkbox).toBeInTheDocument();
  });

  it("PATCHes active_source when the dropdown changes", async () => {
    mockedApi.patch.mockResolvedValue({
      data: { ...SAMPLE_DATASET, active_source: "twse" },
    });

    renderCard();
    await screen.findByText("TaiwanStockPrice");

    const dropdown = screen.getByDisplayValue("finmind");
    fireEvent.change(dropdown, { target: { value: "twse" } });

    await waitFor(() => {
      expect(mockedApi.patch).toHaveBeenCalledWith(
        "/admin/finmind/datasets/TaiwanStockPrice",
        { active_source: "twse" },
      );
    });
  });

  it("POSTs to /run-due when the top button is clicked", async () => {
    mockedApi.post.mockResolvedValue({
      data: {
        total: 0, done: 0, failed: 0, skipped: 0, rows_written: 0,
        outcomes: [],
      },
    });

    renderCard();
    const button = await screen.findByRole("button", {
      name: /Run all due now/i,
    });
    fireEvent.click(button);

    await waitFor(() => {
      expect(mockedApi.post).toHaveBeenCalledWith("/admin/finmind/run-due");
    });
  });

  it("POSTs to per-row run endpoint when the row Run button is clicked", async () => {
    mockedApi.post.mockResolvedValue({
      data: {
        dataset_code: "TaiwanStockPrice",
        symbol: null,
        range_start: "2024-01-01",
        range_end: "2024-01-08",
        status: "done",
        rows_written: 5,
        error: null,
      },
    });

    renderCard();
    await screen.findByText("TaiwanStockPrice");
    // The per-row Run button is small ("Run") — distinguish from
    // "Run all due now" by its exact text.
    const buttons = screen.getAllByRole("button");
    const rowRunButton = buttons.find(
      (b) => b.textContent === "Run",
    );
    expect(rowRunButton).toBeDefined();
    fireEvent.click(rowRunButton!);

    await waitFor(() => {
      expect(mockedApi.post).toHaveBeenCalledWith(
        "/admin/finmind/datasets/TaiwanStockPrice/run",
        { symbol: null },
      );
    });

    // After a successful run the per-row indicator shows "✓ N".
    expect(await screen.findByText(/✓ 5/)).toBeInTheDocument();
  });
});


describe("FinmindAdminCard — disabled affordances", () => {
  it("disables the enabled checkbox when local_table is empty (Phase 1 not built)", async () => {
    mockedApi.get.mockImplementation((url: string) => {
      if (url === "/admin/finmind/status") {
        return Promise.resolve({ data: SAMPLE_STATUS });
      }
      if (url === "/admin/finmind/datasets") {
        return Promise.resolve({
          data: [{ ...SAMPLE_DATASET, local_table: "" }],
        });
      }
      return Promise.reject(new Error(`unexpected url: ${url}`));
    });

    renderCard();
    await screen.findByText("TaiwanStockPrice");

    // "(not built)" label appears when local_table is empty.
    expect(screen.getByText(/\(not built\)/i)).toBeInTheDocument();

    // Dataset checkbox should be disabled.
    const checkboxes = screen.getAllByRole("checkbox");
    const datasetCheckbox = checkboxes[1];
    expect(datasetCheckbox).toBeDisabled();
  });
});
