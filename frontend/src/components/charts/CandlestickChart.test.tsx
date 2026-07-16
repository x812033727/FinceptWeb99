import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  createChart,
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
} from "lightweight-charts";
import CandlestickChart from "./CandlestickChart";
import { INDICATOR_STORAGE_KEY } from "./indicatorPrefs";
import type { OHLCVBar } from "@/types/market";

vi.mock("@/lib/api", () => ({
  default: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

import api from "@/lib/api";

// lightweight-charts renders to <canvas>, which jsdom can't do — replace the
// whole module with call-recording stubs and assert on the series wiring.
vi.mock("lightweight-charts", () => {
  const makeSeries = () => ({
    setData: vi.fn(),
    applyOptions: vi.fn(),
    options: vi.fn(() => ({})),
    coordinateToPrice: vi.fn((y: number) => y * 2),
    createPriceLine: vi.fn(() => ({ id: Math.random() })),
    removePriceLine: vi.fn(),
  });
  const makeChart = () => {
    const priceScales = new Map<string, { applyOptions: ReturnType<typeof vi.fn> }>();
    return {
      // v5 replaced addCandlestickSeries/addHistogramSeries/addLineSeries
      // with one addSeries(SeriesType, options). The series type is now the
      // first argument rather than part of the method name, so the tests
      // assert on it via `callsOf()` below.
      addSeries: vi.fn(() => makeSeries()),
      removeSeries: vi.fn(),
      priceScale: vi.fn((id: string) => {
        if (!priceScales.has(id)) priceScales.set(id, { applyOptions: vi.fn() });
        return priceScales.get(id)!;
      }),
      timeScale: vi.fn(() => ({ fitContent: vi.fn() })),
      applyOptions: vi.fn(),
      subscribeClick: vi.fn(),
      unsubscribeClick: vi.fn(),
      remove: vi.fn(),
    };
  };
  return {
    createChart: vi.fn(() => makeChart()),
    // v5 requires the series types to be imported and passed to addSeries.
    // Identity is all the component needs and all the tests match on.
    CandlestickSeries: { type: "Candlestick" },
    HistogramSeries: { type: "Histogram" },
    LineSeries: { type: "Line" },
    ColorType: { Solid: "solid" },
    LineStyle: { Solid: 0, Dotted: 1, Dashed: 2 },
  };
});

/** The addSeries calls for one series type — v5's replacement for asking
 *  "how many times was addLineSeries called?". */
function callsOf(chart: ChartMock, seriesType: unknown): unknown[][] {
  return chart.addSeries.mock.calls.filter((call: unknown[]) => call[0] === seriesType);
}

/** The series object addSeries returned for the Nth call of a given type.
 *  Every series now comes back through the same mock, so the call index
 *  has to be matched rather than assuming results[0]. */
function seriesOf(chart: ChartMock, seriesType: unknown, nth = 0) {
  const indexes = chart.addSeries.mock.calls
    .map((call: unknown[], i: number) => (call[0] === seriesType ? i : -1))
    .filter((i: number) => i >= 0);
  return chart.addSeries.mock.results[indexes[nth]].value;
}

interface ChartMock {
  addSeries: ReturnType<typeof vi.fn>;
  removeSeries: ReturnType<typeof vi.fn>;
  priceScale: ((id: string) => { applyOptions: ReturnType<typeof vi.fn> }) &
    ReturnType<typeof vi.fn>;
  subscribeClick: ReturnType<typeof vi.fn>;
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
  vi.mocked(api.get).mockReset().mockResolvedValue({ data: [] });
  vi.mocked(api.post).mockReset();
  vi.mocked(api.patch).mockReset();
  vi.mocked(api.delete).mockReset().mockResolvedValue({});
});

describe("CandlestickChart indicators", () => {
  it("renders the chart plus the indicator toolbar, with no indicator series by default", () => {
    render(<CandlestickChart bars={makeBars(40)} height={300} />);
    for (const label of ["MA", "EMA", "BOLL", "RSI", "MACD", "KD", "Off"]) {
      expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
    }
    expect(callsOf(lastChart(), LineSeries)).toHaveLength(0);
    // volume histogram only
    expect(callsOf(lastChart(), HistogramSeries)).toHaveLength(1);
  });

  it("adds three MA line series when the MA chip is toggled on, and persists the choice", async () => {
    const user = userEvent.setup();
    render(<CandlestickChart bars={makeBars(40)} height={300} />);
    await user.click(screen.getByRole("button", { name: "MA" }));

    const chart = lastChart();
    expect(callsOf(chart, LineSeries)).toHaveLength(3); // MA5 / MA10 / MA20
    for (const call of callsOf(chart, LineSeries)) {
      // addSeries(SeriesType, options) — options is the second argument now.
      expect(call[1]).toMatchObject({
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
    expect(callsOf(chart, LineSeries)).toHaveLength(8);
    // volume + MACD histogram
    expect(callsOf(chart, HistogramSeries)).toHaveLength(2);
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
    expect(callsOf(chart, LineSeries)).toHaveLength(1); // RSI line

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
    expect(callsOf(lastChart(), LineSeries)).toHaveLength(0);
  });

  it("places a persisted horizontal line from chart coordinates", async () => {
    vi.mocked(api.post).mockResolvedValue({ data: {
      id: "d1", market: "TW", symbol: "2330", kind: "horizontal",
      points: [{ price: 246 }], label: "Price level", color: "#f59e0b", alert_id: null,
    } });
    render(<CandlestickChart bars={makeBars(40)} market="TW" symbol="2330" height={300} />);
    await waitFor(() => expect(api.get).toHaveBeenCalledWith("/charts/drawings/TW/2330"));
    fireEvent.click(screen.getByRole("button", { name: /Horizontal level/ }));
    const callback = lastChart().subscribeClick.mock.calls[0][0];
    callback({ point: { y: 123 } });
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/charts/drawings", expect.objectContaining({
      market: "TW", symbol: "2330", kind: "horizontal", points: [{ price: 246 }],
    })));
    const candle = seriesOf(lastChart(), CandlestickSeries);
    await waitFor(() => expect(candle.createPriceLine).toHaveBeenCalledWith(expect.objectContaining({ price: 246 })));
  });

  it("converts a saved level to an alert and can delete it", async () => {
    vi.mocked(api.get).mockResolvedValue({ data: [{
      id: "d2", market: "US", symbol: "AAPL", kind: "horizontal",
      points: [{ price: 150 }], label: "Support", color: "#f59e0b", alert_id: null,
    }] });
    vi.mocked(api.post).mockResolvedValue({ data: { id: "a1" } });
    render(<CandlestickChart bars={makeBars(40)} market="US" symbol="AAPL" height={300} />);
    const alertButton = await screen.findByRole("button", { name: "Create price alert" });
    fireEvent.click(alertButton);
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/charts/drawings/d2/alert", expect.objectContaining({ condition: "above" })));
    expect(await screen.findByRole("button", { name: "Alert created" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Delete drawing" }));
    await waitFor(() => expect(api.delete).toHaveBeenCalledWith("/charts/drawings/d2"));
  });

  it("creates and renders a persisted two-point trend line", async () => {
    vi.mocked(api.post).mockResolvedValue({ data: {
      id: "t1", market: "TW", symbol: "2330", kind: "trend",
      points: [
        { time: "2024-01-03", price: 200 },
        { time: "2024-01-10", price: 240 },
      ],
      label: "Trend line", color: "#38bdf8", alert_id: null,
    } });
    render(<CandlestickChart bars={makeBars(40)} market="TW" symbol="2330" height={300} />);
    await waitFor(() => expect(api.get).toHaveBeenCalledWith("/charts/drawings/TW/2330"));
    fireEvent.click(screen.getByRole("button", { name: "Trend line" }));
    const callback = lastChart().subscribeClick.mock.calls[0][0];
    callback({ point: { y: 100 }, time: "2024-01-03" });
    expect(await screen.findByRole("button", { name: "Click second point" })).toBeInTheDocument();
    callback({ point: { y: 120 }, time: "2024-01-10" });

    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/charts/drawings", expect.objectContaining({
      market: "TW", symbol: "2330", kind: "trend",
      points: [
        { time: "2024-01-03", price: 200 },
        { time: "2024-01-10", price: 240 },
      ],
    })));
    await waitFor(() => expect(callsOf(lastChart(), LineSeries)).toHaveLength(1));
    const trend = seriesOf(lastChart(), LineSeries);
    const rendered = trend.setData.mock.calls[0][0];
    expect(rendered.slice(0, 2)).toEqual([
      { time: "2024-01-03", value: 200 },
      { time: "2024-01-10", value: 240 },
    ]);
    expect(rendered[2].time).toBe("2024-01-28");
    expect(rendered[2].value).toBeCloseTo(342.8571, 4);
  });

  it("repositions both endpoints of a saved trend line through PATCH", async () => {
    const original = {
      id: "t2", market: "US", symbol: "AAPL", kind: "trend" as const,
      points: [
        { time: "2024-01-02", price: 180 },
        { time: "2024-01-08", price: 190 },
      ],
      label: "Trend line", color: "#38bdf8", alert_id: null,
    };
    vi.mocked(api.get).mockResolvedValue({ data: [original] });
    vi.mocked(api.patch).mockResolvedValue({ data: {
      ...original,
      points: [
        { time: "2024-01-04", price: 210 },
        { time: "2024-01-12", price: 230 },
      ],
    } });
    render(<CandlestickChart bars={makeBars(40)} market="US" symbol="AAPL" height={300} />);
    fireEvent.click(await screen.findByRole("button", { name: "Reposition trend endpoints" }));
    const callback = lastChart().subscribeClick.mock.calls[0][0];
    callback({ point: { y: 105 }, time: "2024-01-04" });
    expect(await screen.findByRole("button", { name: "Click second point" })).toBeInTheDocument();
    callback({ point: { y: 115 }, time: "2024-01-12" });

    await waitFor(() => expect(api.patch).toHaveBeenCalledWith("/charts/drawings/t2", {
      points: [
        { time: "2024-01-04", price: 210 },
        { time: "2024-01-12", price: 230 },
      ],
    }));
  });

  it("creates a dynamic alert in the next crossing direction for a trend line", async () => {
    vi.mocked(api.get).mockResolvedValue({ data: [{
      id: "t3", market: "US", symbol: "AAPL", kind: "trend",
      points: [
        { time: "2024-01-01", price: 100 },
        { time: "2024-01-11", price: 100 },
      ],
      label: "Trend line", color: "#38bdf8", alert_id: null,
    }] });
    vi.mocked(api.post).mockResolvedValue({ data: { id: "a3" } });
    render(<CandlestickChart bars={makeBars(40)} market="US" symbol="AAPL" currentPrice={150} currentTime="2024-01-28" height={300} />);
    fireEvent.click(await screen.findByRole("button", { name: "Create dynamic trend crossing alert" }));
    await waitFor(() => expect(api.post).toHaveBeenCalledWith(
      "/charts/drawings/t3/alert",
      { condition: "below", repeat: false, cooldown_seconds: 0 },
    ));
    expect(await screen.findByRole("button", { name: "Alert created" })).toBeDisabled();
  });
});
