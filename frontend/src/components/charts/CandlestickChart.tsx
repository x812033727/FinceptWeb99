import { useEffect, useRef, useState } from "react";
import {
  createChart,
  ColorType,
  LineStyle,
  type IChartApi,
  type ISeriesApi,
  type CandlestickData,
  type HistogramData,
  type LineData,
  type Time,
} from "lightweight-charts";
import type { OHLCVBar } from "@/types/market";
import { useThemeStore } from "@/store/themeStore";
import { getChartTheme } from "@/lib/chartTheme";
import { sma, ema, bollinger, rsi, macd, stochastic } from "@/lib/indicators";
import IndicatorToolbar from "./IndicatorToolbar";
import {
  loadIndicatorPrefs,
  saveIndicatorPrefs,
  type IndicatorPrefs,
} from "./indicatorPrefs";

interface Props {
  bars: OHLCVBar[];
  /** Fixed pixel height. Omit to fill the parent container (the
   *  ResizeObserver keeps the canvas in sync) — used by the
   *  StockDetailPage fullscreen mode. */
  height?: number;
}

/** Main-pane overlay periods. Colors map to getChartTheme().series by the
 *  slot order below (stable regardless of which overlays are enabled):
 *  MA5→0, MA10→1, MA20→2, EMA12→3, EMA26→4, BOLL→5. */
const MA_PERIODS = [5, 10, 20];
const EMA_PERIODS = [12, 26];
const BOLL_PERIOD = 20;
const BOLL_MULT = 2;

function toTime(t: string | number): Time {
  if (typeof t === "number") return (t / 1000) as Time;
  return t as Time;
}

/** "rgb(r, g, b)" → "rgba(r, g, b, a)" — lightweight-charts rejects hsl()
 *  but accepts rgba(); used for the translucent volume histogram. */
function withAlpha(rgb: string, alpha: number): string {
  return rgb.replace(/^rgb\(/, "rgba(").replace(/\)$/, `, ${alpha})`);
}

/** Drop warm-up nulls: lightweight-charts accepts sparse series, so points
 *  inside an indicator's warm-up window are simply omitted. */
function toLineData(times: Time[], values: (number | null)[]): LineData[] {
  const out: LineData[] = [];
  for (let i = 0; i < values.length; i++) {
    const v = values[i];
    if (v != null) out.push({ time: times[i], value: v });
  }
  return out;
}

export default function CandlestickChart({ bars, height }: Props) {
  const theme = useThemeStore((s) => s.theme);
  const marketColorMode = useThemeStore((s) => s.marketColorMode);
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  // Indicator series are torn down and recreated wholesale whenever the
  // selection / data / theme changes — daily series are small, so this is
  // cheap and avoids tracking per-indicator series identity.
  const indicatorSeriesRef = useRef<ISeriesApi<"Line" | "Histogram">[]>([]);

  const [prefs, setPrefs] = useState<IndicatorPrefs>(loadIndicatorPrefs);
  useEffect(() => {
    saveIndicatorPrefs(prefs);
  }, [prefs]);

  useEffect(() => {
    if (!containerRef.current) return;

    const t = getChartTheme();

    const chart = createChart(containerRef.current, {
      width: containerRef.current.clientWidth,
      height: containerRef.current.clientHeight,
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: t.text,
        fontSize: 11,
      },
      grid: {
        vertLines: { color: t.grid },
        horzLines: { color: t.grid },
      },
      crosshair: { mode: 1 },
      rightPriceScale: { borderColor: t.grid },
      timeScale: {
        borderColor: t.grid,
        timeVisible: true,
        secondsVisible: false,
      },
    });
    chartRef.current = chart;

    // Candlestick series — up/down come from the --up/--down CSS vars so
    // candles follow the market colour convention (台股紅漲綠跌 vs intl).
    const candle = chart.addCandlestickSeries({
      upColor: t.up,
      downColor: t.down,
      borderUpColor: t.up,
      borderDownColor: t.down,
      wickUpColor: t.up,
      wickDownColor: t.down,
    });
    candleRef.current = candle;

    // Volume histogram (overlay in lower 20% of price scale)
    const vol = chart.addHistogramSeries({
      color: t.grid,
      priceFormat: { type: "volume" },
      priceScaleId: "vol",
    });
    chart.priceScale("vol").applyOptions({
      scaleMargins: { top: 0.82, bottom: 0 },
    });
    volRef.current = vol;

    const ro = new ResizeObserver(() => {
      if (!containerRef.current) return;
      chart.applyOptions({
        width: containerRef.current.clientWidth,
        height: containerRef.current.clientHeight,
      });
    });
    ro.observe(containerRef.current);

    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
      candleRef.current = null;
      volRef.current = null;
      indicatorSeriesRef.current = [];
    };
  }, []);

  // Re-apply theme colours without recreating the chart — runs on both the
  // light/dark toggle and the market colour convention (up/down) switch.
  // (Indicator series colours are handled by the indicator effect below,
  // which also depends on theme/marketColorMode and rebuilds its series.)
  useEffect(() => {
    if (!chartRef.current) return;
    const t = getChartTheme();
    chartRef.current.applyOptions({
      layout: { textColor: t.text },
      grid: {
        vertLines: { color: t.grid },
        horzLines: { color: t.grid },
      },
      rightPriceScale: { borderColor: t.grid },
      timeScale: { borderColor: t.grid },
    });
    candleRef.current?.applyOptions({
      upColor: t.up,
      downColor: t.down,
      borderUpColor: t.up,
      borderDownColor: t.down,
      wickUpColor: t.up,
      wickDownColor: t.down,
    });
  }, [theme, marketColorMode]);

  useEffect(() => {
    if (!candleRef.current || !volRef.current || !bars.length) return;

    const t = getChartTheme();
    const volUp = withAlpha(t.up, 0.33);
    const volDown = withAlpha(t.down, 0.33);

    const sorted = [...bars].sort((a, b) =>
      String(a.time) < String(b.time) ? -1 : 1
    );

    const candleData: CandlestickData[] = sorted.map((b) => ({
      time: toTime(b.time),
      open: b.open,
      high: b.high,
      low: b.low,
      close: b.close,
    }));

    const volData: HistogramData[] = sorted.map((b) => ({
      time: toTime(b.time),
      value: b.volume,
      color: b.close >= b.open ? volUp : volDown,
    }));

    candleRef.current.setData(candleData);
    volRef.current.setData(volData);
    chartRef.current?.timeScale().fitContent();
  }, [bars, theme, marketColorMode]);

  // Indicator overlays + sub-pane. lightweight-charts v4 has no true
  // multi-pane support, so the sub-indicator reuses the volume-histogram
  // trick: a dedicated overlay priceScaleId whose scaleMargins carve a band
  // at the bottom of the canvas. When a sub-indicator is active the layout
  // is candles 4–56% / volume 60–72% / oscillator 76–100%; otherwise the
  // original candles-with-volume-strip layout is restored.
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;

    for (const s of indicatorSeriesRef.current) chart.removeSeries(s);
    indicatorSeriesRef.current = [];

    const subActive = prefs.sub !== null && bars.length > 0;
    chart.priceScale("right").applyOptions({
      // Off-state = lightweight-charts defaults, so the original layout is
      // restored exactly when the sub-pane closes.
      scaleMargins: subActive ? { top: 0.04, bottom: 0.44 } : { top: 0.2, bottom: 0.1 },
    });
    chart.priceScale("vol").applyOptions({
      scaleMargins: subActive ? { top: 0.6, bottom: 0.28 } : { top: 0.82, bottom: 0 },
    });

    if (!bars.length) return;

    const t = getChartTheme();
    const sorted = [...bars].sort((a, b) =>
      String(a.time) < String(b.time) ? -1 : 1
    );
    const times = sorted.map((b) => toTime(b.time));
    const closes = sorted.map((b) => b.close);

    const addLine = (
      color: string,
      priceScaleId: "right" | "sub",
      lineStyle: LineStyle = LineStyle.Solid
    ): ISeriesApi<"Line"> => {
      const s = chart.addLineSeries({
        color,
        lineWidth: 1,
        lineStyle,
        priceScaleId,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerRadius: 3,
      });
      indicatorSeriesRef.current.push(s);
      return s;
    };

    if (prefs.overlays.includes("ma")) {
      MA_PERIODS.forEach((p, i) => {
        addLine(t.series[i % t.series.length], "right").setData(
          toLineData(times, sma(closes, p))
        );
      });
    }
    if (prefs.overlays.includes("ema")) {
      EMA_PERIODS.forEach((p, i) => {
        addLine(t.series[(MA_PERIODS.length + i) % t.series.length], "right").setData(
          toLineData(times, ema(closes, p))
        );
      });
    }
    if (prefs.overlays.includes("boll")) {
      const bandColor = t.series[(MA_PERIODS.length + EMA_PERIODS.length) % t.series.length];
      const { middle, upper, lower } = bollinger(closes, BOLL_PERIOD, BOLL_MULT);
      addLine(bandColor, "right").setData(toLineData(times, middle));
      addLine(withAlpha(bandColor, 0.6), "right", LineStyle.Dashed).setData(
        toLineData(times, upper)
      );
      addLine(withAlpha(bandColor, 0.6), "right", LineStyle.Dashed).setData(
        toLineData(times, lower)
      );
    }

    if (prefs.sub === "rsi") {
      addLine(t.series[0], "sub").setData(toLineData(times, rsi(closes, 14)));
    } else if (prefs.sub === "macd") {
      const { macd: line, signal, histogram } = macd(closes, 12, 26, 9);
      // Histogram first so the MACD / signal lines draw on top of it.
      const hist = chart.addHistogramSeries({
        priceScaleId: "sub",
        priceLineVisible: false,
        lastValueVisible: false,
        base: 0,
      });
      indicatorSeriesRef.current.push(hist);
      const histUp = withAlpha(t.up, 0.45);
      const histDown = withAlpha(t.down, 0.45);
      const histData: HistogramData[] = [];
      histogram.forEach((v, i) => {
        if (v != null)
          histData.push({ time: times[i], value: v, color: v >= 0 ? histUp : histDown });
      });
      hist.setData(histData);
      addLine(t.series[0], "sub").setData(toLineData(times, line));
      addLine(t.series[1], "sub").setData(toLineData(times, signal));
    } else if (prefs.sub === "kd") {
      const { k, d } = stochastic(sorted, 9, 3, 3);
      addLine(t.series[0], "sub").setData(toLineData(times, k));
      addLine(t.series[1], "sub").setData(toLineData(times, d));
    }

    if (subActive) {
      chart.priceScale("sub").applyOptions({
        scaleMargins: { top: 0.76, bottom: 0 },
      });
    }
  }, [bars, prefs, theme, marketColorMode]);

  return (
    <div className="flex flex-col w-full" style={{ height: height ?? "100%" }}>
      <IndicatorToolbar prefs={prefs} onChange={setPrefs} />
      <div className="relative flex-1 min-h-0">
        <div ref={containerRef} className="absolute inset-0" />
        {!bars.length && (
          <div className="absolute inset-0 flex items-center justify-center text-sm text-muted-foreground pointer-events-none">
            No price data available
          </div>
        )}
      </div>
    </div>
  );
}
