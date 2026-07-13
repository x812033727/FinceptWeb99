/**
 * Tests for `SweepAggregateCard` Brier + reliability + fold-badge
 * UI (PR-A0 / PR-C1 / PR-C2 follow-up renders).
 *
 * Mocks `_helpers`'s `fetchSweepAggregate` so we control the payload
 * shape and don't need a backend. Each test renders a single
 * card, asserts the visible elements, and tears down via
 * `afterEach`'s React Testing Library cleanup (set in
 * `src/test/setup.ts`).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const mockFetchSweep = vi.fn();
const mockFetchStrategy = vi.fn();

vi.mock("./_helpers", async () => {
  const actual = await vi.importActual<typeof import("./_helpers")>(
    "./_helpers",
  );
  return {
    ...actual,
    fetchSweepAggregate: (...args: unknown[]) => mockFetchSweep(...args),
    fetchStrategyAggregate: (...args: unknown[]) =>
      mockFetchStrategy(...args),
  };
});

import { SweepAggregateCard } from "./SweepAggregateCard";
import type { SweepAggregate } from "./_helpers";

const BASE: SweepAggregate = {
  scope: "sweep",
  sweep_id: "11111111-1111-1111-1111-111111111111",
  strategy_id: null,
  anchor_date: "2026-04-01",
  trading_days_count: 5,
  completed_count: 5,
  failed_count: 0,
  fold_kind: "production",
  parent_sweep_id: null,
  discussions_total: 5,
  verdict_counts: { win: 3, loss: 2, unverifiable: 0, pending: 0 },
  win_rate: 0.6,
  avg_pnl_pct: [0.01, 0.015, 0.02, 0.018, 0.022],
  brier_score: null,
  brier_samples: 0,
  calibrated_brier_score: null,
  calibrated_brier_samples: 0,
  reliability: [],
  per_persona: [],
  lessons: [],
};

beforeEach(() => {
  mockFetchSweep.mockReset();
  mockFetchStrategy.mockReset();
});

afterEach(() => {
  vi.clearAllMocks();
});

function renderCard(props: { sweepId?: string; strategyId?: string }) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <SweepAggregateCard {...props} />
    </QueryClientProvider>,
  );
}

describe("SweepAggregateCard — BrierRow", () => {
  it("renders nothing when brier_score is null", async () => {
    mockFetchSweep.mockResolvedValue(BASE);
    renderCard({ sweepId: BASE.sweep_id! });
    // Wait for the card to render at all (verdict tile)
    await waitFor(() =>
      expect(screen.getByText("Discussions")).toBeInTheDocument(),
    );
    expect(
      screen.queryByText(/校準度量|Calibration metric/),
    ).not.toBeInTheDocument();
  });

  it("renders raw + calibrated brier with improvement message", async () => {
    mockFetchSweep.mockResolvedValue({
      ...BASE,
      brier_score: 0.25,
      brier_samples: 4,
      calibrated_brier_score: 0.18,
      calibrated_brier_samples: 4,
    });
    renderCard({ sweepId: BASE.sweep_id! });
    await waitFor(() =>
      expect(
        screen.getByText(/校準度量|Calibration metric/),
      ).toBeInTheDocument(),
    );
    expect(screen.getByText("0.250")).toBeInTheDocument();
    expect(screen.getByText("0.180")).toBeInTheDocument();
    // delta = -0.07 → improvement message
    expect(
      screen.getByText(/校準曲線降低 Brier|Calibration curve lowers Brier/),
    ).toBeInTheDocument();
  });

  it("renders 'n/a' for calibrated when only raw is available", async () => {
    mockFetchSweep.mockResolvedValue({
      ...BASE,
      brier_score: 0.22,
      brier_samples: 5,
      calibrated_brier_score: null,
    });
    renderCard({ sweepId: BASE.sweep_id! });
    await waitFor(() =>
      expect(screen.getByText("0.220")).toBeInTheDocument(),
    );
    expect(screen.getByText("n/a")).toBeInTheDocument();
    // No improvement message — delta can't be computed
    expect(
      screen.queryByText(/校準曲線降低|Calibration curve lowers/),
    ).not.toBeInTheDocument();
  });

  it("warns when calibrated is worse than raw", async () => {
    mockFetchSweep.mockResolvedValue({
      ...BASE,
      brier_score: 0.18,
      calibrated_brier_score: 0.30,
    });
    renderCard({ sweepId: BASE.sweep_id! });
    await waitFor(() =>
      expect(
        screen.getByText(/校準後反而變差|Calibration made it worse/),
      ).toBeInTheDocument(),
    );
  });
});

describe("SweepAggregateCard — FoldBadge", () => {
  it("hides badge for production fold (the legacy default)", async () => {
    mockFetchSweep.mockResolvedValue({ ...BASE, fold_kind: "production" });
    renderCard({ sweepId: BASE.sweep_id! });
    await waitFor(() =>
      expect(screen.getByText("Discussions")).toBeInTheDocument(),
    );
    expect(screen.queryByText(/Train fold/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Test fold/)).not.toBeInTheDocument();
  });

  it("renders train-fold badge", async () => {
    mockFetchSweep.mockResolvedValue({ ...BASE, fold_kind: "train" });
    renderCard({ sweepId: BASE.sweep_id! });
    await waitFor(() =>
      expect(screen.getByText(/Train fold/)).toBeInTheDocument(),
    );
  });

  it("renders test-fold badge with parent sweep link", async () => {
    mockFetchSweep.mockResolvedValue({
      ...BASE,
      fold_kind: "test",
      parent_sweep_id: "abcd1234-0000-0000-0000-000000000000",
    });
    renderCard({ sweepId: BASE.sweep_id! });
    await waitFor(() =>
      expect(screen.getByText(/Test fold/)).toBeInTheDocument(),
    );
    expect(screen.getByText("abcd1234")).toBeInTheDocument();
  });
});

describe("SweepAggregateCard — WalkForwardCompareSection", () => {
  it("hidden when sweep is production fold", async () => {
    mockFetchSweep.mockResolvedValue({
      ...BASE,
      brier_score: 0.20,
      fold_kind: "production",
    });
    renderCard({ sweepId: BASE.sweep_id! });
    await waitFor(() =>
      expect(screen.getByText("Discussions")).toBeInTheDocument(),
    );
    expect(
      screen.queryByText(/train vs test|train 折比較/),
    ).not.toBeInTheDocument();
  });

  it("hidden when test fold has no parent_sweep_id", async () => {
    mockFetchSweep.mockResolvedValue({
      ...BASE,
      brier_score: 0.20,
      fold_kind: "test",
      parent_sweep_id: null,
    });
    renderCard({ sweepId: BASE.sweep_id! });
    await waitFor(() =>
      expect(screen.getByText("Discussions")).toBeInTheDocument(),
    );
    expect(
      screen.queryByText(/train 折比較|train vs test/),
    ).not.toBeInTheDocument();
  });

  it("expands and renders side-by-side KPIs for paired train fold", async () => {
    const TRAIN_ID = "aaaa1111-0000-0000-0000-000000000000";
    const TEST_ID = "bbbb2222-0000-0000-0000-000000000000";

    // First call (test fold) returns the test aggregate. Second
    // call (train fold) returns the train aggregate. Both come
    // through the same mocked `fetchSweepAggregate`.
    mockFetchSweep.mockImplementation((id: unknown) => {
      if (id === TEST_ID) {
        return Promise.resolve({
          ...BASE,
          sweep_id: TEST_ID,
          fold_kind: "test",
          parent_sweep_id: TRAIN_ID,
          brier_score: 0.30,
          calibrated_brier_score: 0.28,
          win_rate: 0.4,
          avg_pnl_pct: [0.001, 0.002, 0.003, 0.004, 0.005],
          verdict_counts: { win: 2, loss: 3, unverifiable: 0, pending: 0 },
        });
      }
      if (id === TRAIN_ID) {
        return Promise.resolve({
          ...BASE,
          sweep_id: TRAIN_ID,
          fold_kind: "train",
          parent_sweep_id: null,
          // In-sample looks much better — typical overfitting
          // shape for the test to assert on.
          brier_score: 0.10,
          calibrated_brier_score: 0.08,
          win_rate: 0.85,
          avg_pnl_pct: [0.01, 0.02, 0.03, 0.04, 0.05],
          verdict_counts: { win: 17, loss: 3, unverifiable: 0, pending: 0 },
        });
      }
      return Promise.reject(new Error(`unknown id ${String(id)}`));
    });

    renderCard({ sweepId: TEST_ID });
    await waitFor(() =>
      expect(screen.getByText(/train vs test/)).toBeInTheDocument(),
    );

    // Section starts collapsed.
    expect(
      screen.queryByText(/D5 平均報酬|Avg D5 return/),
    ).not.toBeInTheDocument();

    // Click to expand.
    fireEvent.click(
      screen.getByRole("button", {
        name: /train vs test|train 折比較/,
      }),
    );

    // Headers + at least one KPI row.
    await waitFor(() =>
      expect(
        screen.getByText(/D5 平均報酬|Avg D5 return/),
      ).toBeInTheDocument(),
    );
    // The compare grid has its own column header "Test (OOS)" (the 🔬
    // glyph is now a lucide FlaskConical icon sibling); the FoldBadge
    // also shows "Test fold (OOS)". Match the grid header specifically.
    expect(screen.getByText(/Train.*in-sample/)).toBeInTheDocument();
    expect(screen.getByText("Test (OOS)")).toBeInTheDocument();
    // Train brier 0.100 should appear; test brier 0.300 also
    // appears multiple times (BrierRow tile + CompareGrid cell).
    // We don't assert color directly — just on the values.
    expect(screen.getByText("0.100")).toBeInTheDocument();
    expect(screen.getAllByText("0.300").length).toBeGreaterThanOrEqual(1);
    // Win-rate column: train 85% vs test 40%. The 40% appears in
    // BrierRow's KpiRow tile (also 40%) so allow >= 1.
    expect(screen.getByText("85%")).toBeInTheDocument();
    expect(screen.getAllByText("40%").length).toBeGreaterThanOrEqual(1);
  });

  it("renders fallback when parent train fold has been deleted", async () => {
    const TEST_ID = "cccc3333-0000-0000-0000-000000000000";
    const MISSING_PARENT = "dddd4444-0000-0000-0000-000000000000";

    mockFetchSweep.mockImplementation((id: unknown) => {
      if (id === TEST_ID) {
        return Promise.resolve({
          ...BASE,
          sweep_id: TEST_ID,
          fold_kind: "test",
          parent_sweep_id: MISSING_PARENT,
          brier_score: 0.30,
        });
      }
      return Promise.reject(new Error("Not Found"));
    });

    renderCard({ sweepId: TEST_ID });
    await waitFor(() =>
      expect(screen.getByText(/train vs test/)).toBeInTheDocument(),
    );

    fireEvent.click(
      screen.getByRole("button", {
        name: /train vs test|train 折比較/,
      }),
    );

    await waitFor(() =>
      expect(
        screen.getByText(/Not Found|找不到對應/),
      ).toBeInTheDocument(),
    );
  });
});

describe("SweepAggregateCard — ReliabilityChart", () => {
  it("renders 10 buckets when reliability is populated", async () => {
    const buckets = Array.from({ length: 10 }, (_, i) => ({
      bucket_lower: i / 10,
      bucket_upper: (i + 1) / 10,
      mean_confidence: i === 0 ? null : (i + 0.5) / 10,
      hit_rate: i === 0 ? null : Math.min(1, (i + 0.5) / 10),
      count: i === 0 ? 0 : 5,
    }));
    mockFetchSweep.mockResolvedValue({
      ...BASE,
      brier_score: 0.15,
      reliability: buckets,
    });
    renderCard({ sweepId: BASE.sweep_id! });
    await waitFor(() =>
      expect(screen.getByText(/Reliability/)).toBeInTheDocument(),
    );
  });

  it("hides reliability when buckets array is empty", async () => {
    mockFetchSweep.mockResolvedValue({
      ...BASE,
      brier_score: 0.2,
      reliability: [],
    });
    renderCard({ sweepId: BASE.sweep_id! });
    await waitFor(() =>
      expect(
        screen.getByText(/校準度量|Calibration metric/),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByText(/Reliability/)).not.toBeInTheDocument();
  });
});
