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
import { render, screen, waitFor } from "@testing-library/react";
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
      expect(screen.getByText("討論數")).toBeInTheDocument(),
    );
    expect(screen.queryByText(/校準度量/)).not.toBeInTheDocument();
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
      expect(screen.getByText(/校準度量/)).toBeInTheDocument(),
    );
    expect(screen.getByText("0.250")).toBeInTheDocument();
    expect(screen.getByText("0.180")).toBeInTheDocument();
    // delta = -0.07 → improvement message
    expect(
      screen.getByText(/校準曲線降低 Brier/),
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
      screen.queryByText(/校準曲線降低/),
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
        screen.getByText(/校準後反而變差/),
      ).toBeInTheDocument(),
    );
  });
});

describe("SweepAggregateCard — FoldBadge", () => {
  it("hides badge for production fold (the legacy default)", async () => {
    mockFetchSweep.mockResolvedValue({ ...BASE, fold_kind: "production" });
    renderCard({ sweepId: BASE.sweep_id! });
    await waitFor(() =>
      expect(screen.getByText("討論數")).toBeInTheDocument(),
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
      expect(screen.getByText(/校準度量/)).toBeInTheDocument(),
    );
    expect(screen.queryByText(/Reliability/)).not.toBeInTheDocument();
  });
});
