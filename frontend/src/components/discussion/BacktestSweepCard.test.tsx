/**
 * Tests for `BacktestSweepCard` (PR #274 + #275, covered in
 * PR #280). The card hosts the create-sweep form + the
 * progress list with status-conditional action buttons. The
 * tests mock `_helpers`'s api functions so we exercise the
 * UI logic (status pills, progress bar, button visibility,
 * form payload shape) without booting the full backend stack.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

// ── api mocks ────────────────────────────────────────────────────
//
// The card pulls `fetchSweeps` / `createSweep` / `startSweep` /
// `cancelSweep` / `deleteSweep` from `_helpers`. Stub them at the
// module boundary so the test doesn't need axios + a backend.
const mockFetchSweeps = vi.fn();
const mockCreateSweep = vi.fn();
const mockStartSweep = vi.fn();
const mockCancelSweep = vi.fn();
const mockDeleteSweep = vi.fn();

vi.mock("./_helpers", async () => {
  // Pull in the real module so non-mocked exports (like
  // `useCollapsible` glue) keep working.
  const actual = await vi.importActual<typeof import("./_helpers")>(
    "./_helpers",
  );
  return {
    ...actual,
    fetchSweeps: (...args: unknown[]) => mockFetchSweeps(...args),
    createSweep: (...args: unknown[]) => mockCreateSweep(...args),
    startSweep: (...args: unknown[]) => mockStartSweep(...args),
    cancelSweep: (...args: unknown[]) => mockCancelSweep(...args),
    deleteSweep: (...args: unknown[]) => mockDeleteSweep(...args),
  };
});

import { BacktestSweepCard } from "./BacktestSweepCard";
import type { BacktestSweep } from "./_helpers";

beforeEach(() => {
  localStorage.clear();
  // Default: card renders open immediately so we don't have to
  // click the chevron before each assertion. `useCollapsible`
  // uses the raw storageKey verbatim — no prefix.
  localStorage.setItem("discussion.sweep_panel", "1");
  mockFetchSweeps.mockReset();
  mockCreateSweep.mockReset();
  mockStartSweep.mockReset();
  mockCancelSweep.mockReset();
  mockDeleteSweep.mockReset();
  // Default: empty list. Individual tests override.
  mockFetchSweeps.mockResolvedValue([]);
});

afterEach(() => {
  localStorage.clear();
});

function makeSweep(over: Partial<BacktestSweep> = {}): BacktestSweep {
  return {
    id: "sweep-1",
    status: "pending",
    topic: "test topic",
    rules: "test rules",
    market: "TW",
    persona_ids: ["buffett", "lynch"],
    anchor_date: "2026-01-15",
    trading_days_count: 5,
    rounds_per_discussion: 1,
    concurrency: 1,
    auto_post_mortem: true,
    resolved_dates: [],
    completed_dates: [],
    failed_dates: [],
    error_message: null,
    created_at: "2026-05-03T00:00:00Z",
    started_at: null,
    completed_at: null,
    cancelled_at: null,
    ...over,
  };
}

function renderCard(props: Partial<{
  topic: string; rules: string;
  market: "TW" | "US" | "GLOBAL"; personaIds: string[];
}> = {}) {
  // Each render gets a fresh QueryClient so tests don't share
  // cached query state.
  const qc = new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: Infinity },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={qc}>
      <BacktestSweepCard
        topic={props.topic ?? "2330 短線"}
        rules={props.rules ?? "規則一"}
        market={props.market ?? "TW"}
        personaIds={props.personaIds ?? ["buffett", "lynch"]}
      />
    </QueryClientProvider>,
  );
}

// Helper to advance promises until the query has settled — vitest
// doesn't auto-flush microtasks between `render` and assertions
// for async useQuery calls.
async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

// ── form submit ──────────────────────────────────────────────────


describe("BacktestSweepCard — form submit", () => {
  it("calls createSweep with the form payload + props (topic/rules/personas)", async () => {
    const created = makeSweep({ id: "freshly-made" });
    mockCreateSweep.mockResolvedValue(created);

    renderCard({
      topic: "2330 短線", rules: "rule X",
      market: "TW", personaIds: ["buffett", "lynch", "munger"],
    });
    await flush();

    fireEvent.click(screen.getByText("Create sweep (pending)"));
    await flush();

    expect(mockCreateSweep).toHaveBeenCalledOnce();
    const payload = mockCreateSweep.mock.calls[0][0];
    expect(payload.topic).toBe("2330 短線");
    expect(payload.rules).toBe("rule X");
    expect(payload.market).toBe("TW");
    expect(payload.persona_ids).toEqual(["buffett", "lynch", "munger"]);
    expect(payload.trading_days_count).toBe(5);    // form default
    expect(payload.rounds_per_discussion).toBe(1);
    expect(payload.concurrency).toBe(1);
    expect(payload.auto_post_mortem).toBe(true);   // checkbox default ON
  });

  it("disables submit when no personas are selected", async () => {
    renderCard({ personaIds: [] });
    await flush();

    const btn = screen.getByText("Create sweep (pending)") as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it("respects auto-post-mortem checkbox toggle", async () => {
    mockCreateSweep.mockResolvedValue(makeSweep());
    renderCard();
    await flush();

    // The checkbox label appears inline with the help text — find
    // it by role + the long label substring.
    const checkbox = screen.getByRole("checkbox") as HTMLInputElement;
    expect(checkbox.checked).toBe(true);

    fireEvent.click(checkbox);
    expect(checkbox.checked).toBe(false);

    fireEvent.click(screen.getByText("Create sweep (pending)"));
    await flush();

    expect(mockCreateSweep).toHaveBeenCalledOnce();
    const payload = mockCreateSweep.mock.calls[0][0];
    expect(payload.auto_post_mortem).toBe(false);
  });
});


// ── status-conditional action buttons ────────────────────────────


describe("BacktestSweepCard — action buttons per status", () => {
  it("pending → only Start button", async () => {
    mockFetchSweeps.mockResolvedValue([makeSweep({ status: "pending" })]);
    renderCard();

    expect(await screen.findByText("Start")).toBeInTheDocument();
    expect(screen.queryByText("Stop")).not.toBeInTheDocument();
    expect(screen.queryByText("Delete")).not.toBeInTheDocument();
  });

  it("running → only Cancel button", async () => {
    mockFetchSweeps.mockResolvedValue([
      makeSweep({ status: "running", started_at: "2026-05-03T00:00:00Z" }),
    ]);
    renderCard();

    expect(await screen.findByText("Stop")).toBeInTheDocument();
    expect(screen.queryByText("Start")).not.toBeInTheDocument();
    expect(screen.queryByText("Delete")).not.toBeInTheDocument();
  });

  it("completed → only Delete button", async () => {
    mockFetchSweeps.mockResolvedValue([
      makeSweep({
        status: "completed",
        completed_at: "2026-05-03T01:00:00Z",
        completed_dates: ["2026-01-15", "2026-01-16"],
      }),
    ]);
    renderCard();

    expect(await screen.findByText("Delete")).toBeInTheDocument();
    expect(screen.queryByText("Start")).not.toBeInTheDocument();
    expect(screen.queryByText("Stop")).not.toBeInTheDocument();
  });

  it("Start button calls startSweep with the row id", async () => {
    mockFetchSweeps.mockResolvedValue([makeSweep({ id: "abc" })]);
    mockStartSweep.mockResolvedValue(makeSweep({ id: "abc", status: "running" }));
    renderCard();

    fireEvent.click(await screen.findByText("Start"));
    await flush();

    // useMutation passes (variables, context) — assert variables only.
    expect(mockStartSweep.mock.calls[0][0]).toBe("abc");
  });

  it("Cancel button calls cancelSweep with the row id", async () => {
    mockFetchSweeps.mockResolvedValue([
      makeSweep({ id: "xyz", status: "running" }),
    ]);
    mockCancelSweep.mockResolvedValue(makeSweep({
      id: "xyz", status: "cancelled",
    }));
    renderCard();

    fireEvent.click(await screen.findByText("Stop"));
    await flush();

    expect(mockCancelSweep.mock.calls[0][0]).toBe("xyz");
  });
});


// ── progress display ─────────────────────────────────────────────


describe("BacktestSweepCard — progress display", () => {
  it("shows X/Y completed counts and a progress bar at the right percent", async () => {
    mockFetchSweeps.mockResolvedValue([
      makeSweep({
        status: "running",
        resolved_dates: ["2026-01-15", "2026-01-16", "2026-01-17", "2026-01-18"],
        completed_dates: ["2026-01-15", "2026-01-16"],
      }),
    ]);
    renderCard();

    // 2 of 4 = 50 %.
    const bar = await screen.findByRole("progressbar");
    expect(bar).toHaveAttribute("aria-valuenow", "50");
    expect(screen.getByText(/done: 2\/4/)).toBeInTheDocument();
  });

  it("includes failed counter when there are failed dates", async () => {
    mockFetchSweeps.mockResolvedValue([
      makeSweep({
        status: "completed",
        resolved_dates: ["2026-01-15", "2026-01-16", "2026-01-17"],
        completed_dates: ["2026-01-15"],
        failed_dates: [
          { date: "2026-01-16", error: "LLM timeout" },
          { date: "2026-01-17", error: "no ohlcv" },
        ],
      }),
    ]);
    renderCard();

    expect(await screen.findByText(/failed: 2/)).toBeInTheDocument();
    // Failed-dates expandable details surface the per-date error.
    expect(screen.getByText(/LLM timeout/)).toBeInTheDocument();
    expect(screen.getByText(/no ohlcv/)).toBeInTheDocument();
  });

  it("renders 「📋」 marker when auto_post_mortem is on", async () => {
    mockFetchSweeps.mockResolvedValue([
      makeSweep({ auto_post_mortem: true }),
    ]);
    renderCard();

    // The marker lives in the per-row config strip.
    expect(await screen.findByText("· 📋")).toBeInTheDocument();
  });

  it("omits the 「📋」 marker when auto_post_mortem is off", async () => {
    mockFetchSweeps.mockResolvedValue([
      makeSweep({ auto_post_mortem: false }),
    ]);
    renderCard();
    // Wait for the row to render (any element specific to it)
    // before asserting absence — if we assert before the query
    // settles, the missing marker is trivially absent for the
    // wrong reason.
    await screen.findByText(/×5 days/);

    expect(screen.queryByText("· 📋")).not.toBeInTheDocument();
  });
});


// ── empty state + loading ────────────────────────────────────────


describe("BacktestSweepCard — empty + loading", () => {
  it("renders the empty-state line when there are no sweeps", async () => {
    mockFetchSweeps.mockResolvedValue([]);
    renderCard();

    expect(await screen.findByText("No sweeps yet")).toBeInTheDocument();
  });
});
