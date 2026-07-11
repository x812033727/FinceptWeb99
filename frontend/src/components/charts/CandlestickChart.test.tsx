import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createChart } from "lightweight-charts";
import CandlestickChart from "./CandlestickChart";
import { INDICATOR_STORAGE_KEY } from "./indicatorPrefs";
import type { OHLCVBar } from "@/types/market";

// lightweight-charts renders to <canvas>, which jsdom can't do — replace the
// whole module with call-recording stubs and assert on the series wiring.
vi.mock("lightweight-charts", () => {
  const makeSeries = () => ({
    setData: vi.fn(),
    applyOptions: vi.fn(),
    options: vi.fn(() => ({})),
  });
  const makeChart = () => {
    const priceScales = new Map<string, { applyOptions: ReturnType<typeof vi.fn> }>();
    return {
      addCandlestickSeries: vi.fn(() => makeSeries()),
      addHistogramSeries: vi.fn(() => makeSeries()),
      addLineSeries: vi.fn(() => makeSeries()),
      removeSeries: vi.fn(),
      priceScale: vi.fn((id: string) => {
        if (!priceScales.has(id)) priceScales.set(id, { applyOptions: vi.fn() });
        return priceScales.get(id)!;
      }),
      timeScale: vi.fn(() => ({ fitContent: vi.fn() })),
      applyOptions: vi.fn(),
      remove: vi.fn(),
    };
  };
  return {
    createChart: vi.fn(() => makeChart()),
    ColorType: { Solid: "solid" },
    LineStyle: { Solid: 0, Dotted: 1, Dashed: 2 },
  };
});

interface ChartMock {
  addLineSeries: ReturnType<typeof vi.fn>;
  addHistogramSeries: ReturnType<typeof vi.fn>;
  removeSeries: ReturnType<typeof vi.fn>;
  priceScale: ((id: string) => { applyOptions: ReturnType<typeof vi.fn> }) &
    ReturnType<typeof vi.fn>;
}

function lastChart(): ChartMock {
  const results = vi.mocked(createChart).mock.results;
  return results[results.length - 1].value as ChartMock;
}

function makeBars(n: number): OHLCVBar[] {
  return Array.from({ length: n }, (_, i) => {
    const base = 100 + Math.sin(i / 4) * 5 + i * 0.1;
    return {
      time: `2024-01-${String((i % 28) + 1).padStart(2, "0")}`,
      open: base,
      high: base + 2,
      low: base - 2,
      close: base + (i % 2 === 0 ? 1 : -1),
      volume: 1000 + i,
    };
  });
}

beforeEach(() => {
  localStorage.clear();
  vi.mocked(createChart).mockClear();
});

describe("CandlestickChart indicators", () => {
  it("renders the chart plus the indicator toolbar, with no indicator series by default", () => {
    render(<CandlestickChart bars={makeBars(40)} height={300} />);
    for (const label of ["MA", "EMA", "BOLL", "RSI", "MACD", "KD", "Off"]) {
      expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
    }
    expect(lastChart().addLineSeries).not.toHaveBeenCalled();
    // volume histogram only
    expect(lastChart().addHistogramSeries).toHaveBeenCalledTimes(1);
  });

  it("adds three MA line series when the MA chip is toggled on, and persists the choice", async () => {
    const user = userEvent.setup();
    render(<CandlestickChart bars={makeBars(40)} height={300} />);
    await user.click(screen.getByRole("button", { name: "MA" }));

    const chart = lastChart();
    expect(chart.addLineSeries).toHaveBeenCalledTimes(3); // MA5 / MA10 / MA20
    for (const call of chart.addLineSeries.mock.calls) {
      expect(call[0]).toMatchObject({
        lineWidth: 1,
        priceScaleId: "right",
        priceLineVisible: false,
      });
    }
    expect(JSON.parse(localStorage.getItem(INDICATOR_STORAGE_KEY)!)).toEqual({
      overlays: ["ma"],
      sub: null,
    });
  });

  it("restores persisted indicators on mount and builds the matching series", () => {
    localStorage.setItem(
      INDICATOR_STORAGE_KEY,
      JSON.stringify({ overlays: ["ma", "boll"], sub: "macd" })
    );
    render(<CandlestickChart bars={makeBars(40)} height={300} />);

    const chart = lastChart();
    // 3 MA + 3 BOLL (mid/upper/lower) + MACD line + signal = 8 line series
    expect(chart.addLineSeries).toHaveBeenCalledTimes(8);
    // volume + MACD histogram
    expect(chart.addHistogramSeries).toHaveBeenCalledTimes(2);
    // sub-pane band carved via its own priceScaleId, volume band shrunk
    expect(chart.priceScale).toHaveBeenCalledWith("sub");
    const volScale = chart.priceScale("vol");
    expect(volScale.applyOptions).toHaveBeenCalledWith({
      scaleMargins: { top: 0.6, bottom: 0.28 },
    });
    expect(screen.getByRole("button", { name: "MA" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "MACD" })).toHaveAttribute("aria-pressed", "true");
  });

  it("removes sub-pane series and restores the layout when switched back to Off", async () => {
    localStorage.setItem(
      INDICATOR_STORAGE_KEY,
      JSON.stringify({ overlays: [], sub: "rsi" })
    );
    const user = userEvent.setup();
    render(<CandlestickChart bars={makeBars(40)} height={300} />);

    const chart = lastChart();
    expect(chart.addLineSeries).toHaveBeenCalledTimes(1); // RSI line

    await user.click(screen.getByRole("button", { name: "Off" }));
    expect(chart.removeSeries).toHaveBeenCalledTimes(1);
    const volScale = chart.priceScale("vol");
    expect(volScale.applyOptions).toHaveBeenLastCalledWith({
      scaleMargins: { top: 0.82, bottom: 0 },
    });
    expect(JSON.parse(localStorage.getItem(INDICATOR_STORAGE_KEY)!)).toEqual({
      overlays: [],
      sub: null,
    });
  });

  it("still renders the empty state without crashing when there are no bars", () => {
    render(<CandlestickChart bars={[]} height={300} />);
    expect(screen.getByText("No price data available")).toBeInTheDocument();
    expect(lastChart().addLineSeries).not.toHaveBeenCalled();
  });
});
