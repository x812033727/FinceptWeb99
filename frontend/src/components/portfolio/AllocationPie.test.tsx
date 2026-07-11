/**
 * Tests for AllocationPie.
 *
 * recharts is mocked at the module seam — the components become
 * inspectable wrappers that surface their props as data attributes
 * and JSON. This lets us assert on the data we pass into the chart
 * (the chart's own pixel-rendering behaviour is recharts' problem,
 * not ours, and ResponsiveContainer needs a real ResizeObserver
 * which jsdom doesn't ship).
 */
import { describe, expect, it, vi } from "vitest";
import { render } from "@testing-library/react";

import AllocationPie from "./AllocationPie";
import type { Holding } from "@/types/portfolio";

vi.mock("recharts", () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="responsive">{children}</div>
  ),
  PieChart: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="pie-chart">{children}</div>
  ),
  Pie: ({ data, dataKey, children }: {
    data: unknown[]; dataKey: string; children?: React.ReactNode
  }) => (
    <div data-testid="pie" data-data={JSON.stringify(data)} data-data-key={dataKey}>
      {children}
    </div>
  ),
  Cell: ({ fill }: { fill: string }) => (
    <span data-testid="cell" data-fill={fill} />
  ),
  Tooltip: ({ formatter }: { formatter: (v: number) => [string, string] }) => (
    <div data-testid="tooltip" data-sample={JSON.stringify(formatter(42.5))} />
  ),
  Legend: () => <div data-testid="legend" />,
}));

function makeHolding(overrides: Partial<Holding> = {}): Holding {
  return {
    id: "h1",
    symbol: "AAPL",
    market: "US",
    quantity: 10,
    avg_cost: 150,
    cost_currency: "USD",
    weight_pct: 50,
    ...overrides,
  };
}

function readPieData(container: HTMLElement) {
  const pie = container.querySelector('[data-testid="pie"]') as HTMLElement;
  return JSON.parse(pie.getAttribute("data-data")!);
}


// ── Empty state ──────────────────────────────────────────────────

describe("AllocationPie — empty state", () => {
  it("renders the 'No holdings to display' placeholder when there are no holdings", () => {
    const { getByText } = render(<AllocationPie holdings={[]} />);
    expect(getByText("No holdings to display")).toBeInTheDocument();
  });

  it("renders the placeholder when every holding has zero weight (e.g. fresh portfolio)", () => {
    const { getByText } = render(
      <AllocationPie holdings={[
        makeHolding({ id: "a", weight_pct: 0 }),
        makeHolding({ id: "b", weight_pct: undefined }),
      ]} />
    );
    expect(getByText("No holdings to display")).toBeInTheDocument();
  });
});

// ── Data shape ───────────────────────────────────────────────────

describe("AllocationPie — data shape", () => {
  it("maps each holding to {name: 'SYMBOL (MARKET)', value: weight_pct}", () => {
    const { container } = render(
      <AllocationPie holdings={[
        makeHolding({ symbol: "AAPL", market: "US", weight_pct: 60 }),
        makeHolding({ id: "h2", symbol: "2330", market: "TW", weight_pct: 40 }),
      ]} />
    );
    expect(readPieData(container)).toEqual([
      { name: "AAPL (US)", value: 60 },
      { name: "2330 (TW)", value: 40 },
    ]);
  });

  it("filters out zero-weight holdings even when other holdings have weight", () => {
    const { container } = render(
      <AllocationPie holdings={[
        makeHolding({ id: "a", symbol: "AAPL", weight_pct: 70 }),
        makeHolding({ id: "b", symbol: "MSFT", weight_pct: 0 }),
        makeHolding({ id: "c", symbol: "GOOGL", weight_pct: 30 }),
      ]} />
    );
    const data = readPieData(container);
    expect(data).toHaveLength(2);
    expect(data.map((d: { name: string }) => d.name)).toEqual([
      "AAPL (US)", "GOOGL (US)",
    ]);
  });

  it("treats undefined weight_pct as zero (filtered out)", () => {
    const { container } = render(
      <AllocationPie holdings={[
        makeHolding({ id: "a", symbol: "AAPL", weight_pct: 100 }),
        makeHolding({ id: "b", symbol: "MSFT", weight_pct: undefined }),
      ]} />
    );
    expect(readPieData(container)).toHaveLength(1);
  });

  it("uses `value` as the dataKey so recharts knows which field to size each slice from", () => {
    const { container } = render(<AllocationPie holdings={[makeHolding({ weight_pct: 100 })]} />);
    const pie = container.querySelector('[data-testid="pie"]') as HTMLElement;
    expect(pie.getAttribute("data-data-key")).toBe("value");
  });
});

// ── Cell colors ──────────────────────────────────────────────────

describe("AllocationPie — Cell colors", () => {
  it("emits one Cell per filtered data point", () => {
    const { container } = render(
      <AllocationPie holdings={[
        makeHolding({ id: "a", symbol: "AAPL", weight_pct: 33 }),
        makeHolding({ id: "b", symbol: "MSFT", weight_pct: 33 }),
        makeHolding({ id: "c", symbol: "GOOGL", weight_pct: 34 }),
      ]} />
    );
    expect(container.querySelectorAll('[data-testid="cell"]')).toHaveLength(3);
  });

  it("cycles through the 6-token --chart-N palette for portfolios with > 6 holdings", () => {
    const holdings = Array.from({ length: 8 }, (_, i) =>
      makeHolding({ id: `h${i}`, symbol: `S${i}`, weight_pct: 10 })
    );
    const { container } = render(<AllocationPie holdings={holdings} />);
    const cells = container.querySelectorAll('[data-testid="cell"]');
    expect(cells).toHaveLength(8);
    // 7th cell wraps back to the first token (i % 6 === 0 → palette[0]).
    expect(cells[0].getAttribute("data-fill")).toBe(cells[6].getAttribute("data-fill"));
    expect(cells[1].getAttribute("data-fill")).toBe(cells[7].getAttribute("data-fill"));
    // Cells 0-5 must all be distinct (no early repeats), and every fill
    // must be a theme token, not a hardcoded hex.
    const firstSix = Array.from(cells)
      .slice(0, 6)
      .map((c) => c.getAttribute("data-fill"));
    expect(new Set(firstSix).size).toBe(6);
    for (const fill of firstSix) {
      expect(fill).toMatch(/^hsl\(var\(--chart-[1-6]\)\)$/);
    }
  });
});

// ── Tooltip formatter ────────────────────────────────────────────

describe("AllocationPie — tooltip", () => {
  it("formats the slice value as '<n>.<d>%' with the 'Weight' label", () => {
    const { container } = render(<AllocationPie holdings={[makeHolding({ weight_pct: 50 })]} />);
    const tip = container.querySelector('[data-testid="tooltip"]') as HTMLElement;
    // The formatter mock above feeds a fixed sample value (42.5) and
    // serialises the [valueStr, label] return tuple.
    expect(JSON.parse(tip.getAttribute("data-sample")!)).toEqual(["42.5%", "Weight"]);
  });
});
