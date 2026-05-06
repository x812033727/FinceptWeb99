/**
 * Tests for the per-strategy Brier trend chart (audit Workflow
 * Win #1).
 *
 * recharts is mocked at the module boundary — the components
 * become inspectable wrappers that surface props as data
 * attributes / JSON. We don't need to test recharts' rendering;
 * we test our own data shaping (point ordering, label format,
 * series mapping) and the API plumbing.
 *
 * Mocking also avoids `ResponsiveContainer` needing
 * `ResizeObserver` which jsdom doesn't ship — the same trick
 * AllocationPie's tests use.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const mockFetch = vi.fn();

vi.mock("./_helpers", async () => {
  const actual = await vi.importActual<typeof import("./_helpers")>(
    "./_helpers",
  );
  return {
    ...actual,
    fetchStrategyBrierHistory: (...args: unknown[]) =>
      mockFetch(...args),
  };
});

vi.mock("recharts", () => ({
  ResponsiveContainer: ({ children }: { children: ReactNode }) => (
    <div data-testid="responsive">{children}</div>
  ),
  LineChart: ({
    data, children,
  }: {
    data: unknown[]; children?: ReactNode;
  }) => (
    <div data-testid="line-chart" data-points={JSON.stringify(data)}>
      {children}
    </div>
  ),
  Line: ({ dataKey, name, stroke }: {
    dataKey: string; name: string; stroke: string;
  }) => (
    <div
      data-testid={`line-${dataKey}`}
      data-name={name}
      data-stroke={stroke}
    />
  ),
  CartesianGrid: () => <div data-testid="grid" />,
  XAxis: ({ dataKey }: { dataKey: string }) => (
    <div data-testid="xaxis" data-key={dataKey} />
  ),
  YAxis: () => <div data-testid="yaxis" />,
  Tooltip: () => <div data-testid="tooltip" />,
  Legend: () => <div data-testid="legend" />,
  ReferenceLine: ({ y }: { y: number }) => (
    <div data-testid="ref-line" data-y={y} />
  ),
}));

import { BrierTrendChart } from "./BrierTrendChart";
import type { BrierHistoryPoint } from "./_helpers";

beforeEach(() => {
  mockFetch.mockReset();
});

afterEach(() => {
  vi.clearAllMocks();
});

function renderChart(
  strategyId: string = "11111111-1111-1111-1111-111111111111",
  windowDays?: number,
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <BrierTrendChart
        strategyId={strategyId}
        {...(windowDays !== undefined ? { windowDays } : {})}
      />
    </QueryClientProvider>,
  );
}

describe("BrierTrendChart", () => {
  it("renders the warming-up placeholder when API returns []", async () => {
    mockFetch.mockResolvedValue([]);
    renderChart();
    await waitFor(() =>
      expect(
        screen.getByText(/還在累積|warming up/),
      ).toBeInTheDocument(),
    );
  });

  it("renders error message when API rejects", async () => {
    mockFetch.mockRejectedValue(new Error("kaboom"));
    renderChart();
    await waitFor(() =>
      expect(screen.getByText("kaboom")).toBeInTheDocument(),
    );
  });

  it("emits both raw and calibrated lines with correct dataKey", async () => {
    const points: BrierHistoryPoint[] = [
      {
        sweep_id: "a",
        anchor_date: "2026-04-01",
        completed_at: "2026-04-02T00:00:00Z",
        fold_kind: "production",
        raw_brier: 0.30,
        calibrated_brier: 0.25,
        samples: 3,
      },
    ];
    mockFetch.mockResolvedValue(points);
    renderChart();

    await waitFor(() =>
      expect(screen.getByTestId("line-chart")).toBeInTheDocument(),
    );

    expect(screen.getByTestId("line-raw_brier")).toBeInTheDocument();
    expect(
      screen.getByTestId("line-calibrated_brier"),
    ).toBeInTheDocument();

    // Reference line at the 0.25 baseline.
    const ref = screen.getByTestId("ref-line");
    expect(ref.getAttribute("data-y")).toBe("0.25");
  });

  it("orders chart points by completed_at ascending", async () => {
    const out_of_order: BrierHistoryPoint[] = [
      {
        sweep_id: "newer",
        anchor_date: "2026-05-01",
        completed_at: "2026-05-02T00:00:00Z",
        fold_kind: "production",
        raw_brier: 0.10,
        calibrated_brier: 0.08,
        samples: 5,
      },
      {
        sweep_id: "older",
        anchor_date: "2026-04-01",
        completed_at: "2026-04-02T00:00:00Z",
        fold_kind: "production",
        raw_brier: 0.30,
        calibrated_brier: 0.25,
        samples: 3,
      },
    ];
    mockFetch.mockResolvedValue(out_of_order);
    renderChart();
    await waitFor(() =>
      expect(screen.getByTestId("line-chart")).toBeInTheDocument(),
    );
    const chart = screen.getByTestId("line-chart");
    const pts = JSON.parse(
      chart.getAttribute("data-points") ?? "[]",
    );
    // older point (raw 0.30) appears first; newer (0.10) second.
    expect(pts[0].raw_brier).toBe(0.30);
    expect(pts[1].raw_brier).toBe(0.10);
  });

  it("formats the X-axis label as MM-DD from anchor_date", async () => {
    mockFetch.mockResolvedValue([
      {
        sweep_id: "a",
        anchor_date: "2026-04-01",
        completed_at: "2026-04-02T00:00:00Z",
        fold_kind: "production",
        raw_brier: 0.30,
        calibrated_brier: null,
        samples: 3,
      },
    ]);
    renderChart();
    await waitFor(() =>
      expect(screen.getByTestId("line-chart")).toBeInTheDocument(),
    );
    const pts = JSON.parse(
      screen.getByTestId("line-chart").getAttribute("data-points") ?? "[]",
    );
    expect(pts[0].label).toBe("04-01");
  });

  it("calls fetchStrategyBrierHistory with the supplied window", async () => {
    mockFetch.mockResolvedValue([]);
    renderChart("99999999-9999-9999-9999-999999999999", 30);
    await waitFor(() =>
      expect(mockFetch).toHaveBeenCalledWith(
        "99999999-9999-9999-9999-999999999999",
        30,
      ),
    );
  });

  it("defaults the window to 90 days when not supplied", async () => {
    mockFetch.mockResolvedValue([]);
    renderChart();
    await waitFor(() =>
      expect(mockFetch).toHaveBeenLastCalledWith(
        "11111111-1111-1111-1111-111111111111",
        90,
      ),
    );
  });
});
