import { useEffect, useRef } from "react";
import {
  createChart,
  ColorType,
  type IChartApi,
  type ISeriesApi,
  type CandlestickData,
  type HistogramData,
  type Time,
} from "lightweight-charts";
import type { OHLCVBar } from "@/types/market";
import { useThemeStore } from "@/store/themeStore";
import { getChartTheme } from "@/lib/chartTheme";

interface Props {
  bars: OHLCVBar[];
  /** Fixed pixel height. Omit to fill the parent container (the
   *  ResizeObserver keeps the canvas in sync) — used by the
   *  StockDetailPage fullscreen mode. */
  height?: number;
}

function toTime(t: string | number): Time {
  if (typeof t === "number") return (t / 1000) as Time;
  return t as Time;
}

/** "rgb(r, g, b)" → "rgba(r, g, b, a)" — lightweight-charts rejects hsl()
 *  but accepts rgba(); used for the translucent volume histogram. */
function withAlpha(rgb: string, alpha: number): string {
  return rgb.replace(/^rgb\(/, "rgba(").replace(/\)$/, `, ${alpha})`);
}

export default function CandlestickChart({ bars, height }: Props) {
  const theme = useThemeStore((s) => s.theme);
  const marketColorMode = useThemeStore((s) => s.marketColorMode);
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volRef = useRef<ISeriesApi<"Histogram"> | null>(null);

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
    };
  }, []);

  // Re-apply theme colours without recreating the chart — runs on both the
  // light/dark toggle and the market colour convention (up/down) switch.
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

  return (
    <div className="relative w-full" style={{ height: height ?? "100%" }}>
      <div ref={containerRef} className="absolute inset-0" />
      {!bars.length && (
        <div className="absolute inset-0 flex items-center justify-center text-sm text-muted-foreground pointer-events-none">
          No price data available
        </div>
      )}
    </div>
  );
}
