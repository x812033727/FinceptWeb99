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

interface Props {
  bars: OHLCVBar[];
  height?: number;
}

function toTime(t: string | number): Time {
  if (typeof t === "number") return (t / 1000) as Time;
  return t as Time;
}

function getChartColors() {
  const style = getComputedStyle(document.documentElement);
  const v = (name: string) => `hsl(${style.getPropertyValue(name).trim()})`;
  return {
    textColor: v("--muted-foreground"),
    gridColor: v("--border"),
    borderColor: v("--border"),
  };
}

export default function CandlestickChart({ bars, height = 360 }: Props) {
  const theme = useThemeStore((s) => s.theme);
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volRef = useRef<ISeriesApi<"Histogram"> | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;

    const { textColor, gridColor, borderColor } = getChartColors();

    const chart = createChart(containerRef.current, {
      width: containerRef.current.clientWidth,
      height,
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor,
        fontSize: 11,
      },
      grid: {
        vertLines: { color: gridColor },
        horzLines: { color: gridColor },
      },
      crosshair: { mode: 1 },
      rightPriceScale: { borderColor },
      timeScale: {
        borderColor,
        timeVisible: true,
        secondsVisible: false,
      },
    });
    chartRef.current = chart;

    // Candlestick series — up/down colors are semantic data colors, not theme-dependent
    const candle = chart.addCandlestickSeries({
      upColor: "#22c55e",
      downColor: "#ef4444",
      borderUpColor: "#22c55e",
      borderDownColor: "#ef4444",
      wickUpColor: "#22c55e",
      wickDownColor: "#ef4444",
    });
    candleRef.current = candle;

    // Volume histogram (overlay in lower 20% of price scale)
    const vol = chart.addHistogramSeries({
      color: borderColor,
      priceFormat: { type: "volume" },
      priceScaleId: "vol",
    });
    chart.priceScale("vol").applyOptions({
      scaleMargins: { top: 0.82, bottom: 0 },
    });
    volRef.current = vol;

    const ro = new ResizeObserver(() => {
      chart.applyOptions({ width: containerRef.current!.clientWidth });
    });
    ro.observe(containerRef.current);

    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
      candleRef.current = null;
      volRef.current = null;
    };
  }, [height]);

  // Re-apply theme colours without recreating the chart
  useEffect(() => {
    if (!chartRef.current) return;
    const { textColor, gridColor, borderColor } = getChartColors();
    chartRef.current.applyOptions({
      layout: { textColor },
      grid: {
        vertLines: { color: gridColor },
        horzLines: { color: gridColor },
      },
      rightPriceScale: { borderColor },
      timeScale: { borderColor },
    });
  }, [theme]);

  useEffect(() => {
    if (!candleRef.current || !volRef.current || !bars.length) return;

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
      color: b.close >= b.open ? "#16a34a55" : "#dc262655",
    }));

    candleRef.current.setData(candleData);
    volRef.current.setData(volData);
    chartRef.current?.timeScale().fitContent();
  }, [bars]);

  return <div ref={containerRef} className="w-full" style={{ height }} />;
}
