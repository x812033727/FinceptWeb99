import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Bell, Minus, Pencil, TrendingUp, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import {
  createChart,
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  ColorType,
  LineStyle,
  type IChartApi,
  type ISeriesApi,
  type CandlestickData,
  type HistogramData,
  type LineData,
  type IPriceLine,
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
import api from "@/lib/api";

interface ChartDrawing {
  id: string;
  market: string;
  symbol: string;
  kind: "horizontal" | "trend";
  points: { time?: string | null; price: number }[];
  label: string | null;
  color: string;
  alert_id: string | null;
}

type DrawingMode = "horizontal" | "trend" | null;
type DrawingPoint = ChartDrawing["points"][number];

interface Props {
  bars: OHLCVBar[];
  market?: "US" | "TW" | "CRYPTO";
  symbol?: string;
  currentPrice?: number | null;
  currentTime?: string | number | null;
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

function serializeDrawingTime(time: Time): string {
  if (typeof time === "string") return time;
  if (typeof time === "number") return String(time);
  return `${time.year}-${String(time.month).padStart(2, "0")}-${String(time.day).padStart(2, "0")}`;
}

function parseDrawingTime(time: string): Time {
  return /^\d+$/.test(time) ? Number(time) as Time : time as Time;
}

function drawingTimeOrder(time: Time): number {
  if (typeof time === "number") return time;
  if (typeof time === "string") return (Date.parse(time) || 0) / 1000;
  return Date.UTC(time.year, time.month - 1, time.day) / 1000;
}

function projectTrendPrice(points: DrawingPoint[], at: Time): number | null {
  if (points.length !== 2 || !points[0].time || !points[1].time) return null;
  const startTime = drawingTimeOrder(parseDrawingTime(points[0].time));
  const endTime = drawingTimeOrder(parseDrawingTime(points[1].time));
  const atTime = drawingTimeOrder(at);
  if (!Number.isFinite(startTime) || !Number.isFinite(endTime) || endTime === startTime) return null;
  const projected = points[0].price + (points[1].price - points[0].price)
    * ((atTime - startTime) / (endTime - startTime));
  return Number.isFinite(projected) && projected > 0 ? projected : null;
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

export default function CandlestickChart({ bars, height, market, symbol, currentPrice, currentTime }: Props) {
  const { t: tr } = useTranslation();
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
  const drawingSeriesRef = useRef<ISeriesApi<"Line">[]>([]);
  const priceLinesRef = useRef<IPriceLine[]>([]);
  const drawingModeRef = useRef<DrawingMode>(null);
  const placePointRef = useRef<(point: DrawingPoint) => void>(() => undefined);
  const [drawingMode, setDrawingMode] = useState<DrawingMode>(null);
  const [trendAnchor, setTrendAnchor] = useState<DrawingPoint | null>(null);
  const [editingDrawingId, setEditingDrawingId] = useState<string | null>(null);
  const [drawings, setDrawings] = useState<ChartDrawing[]>([]);
  const [loadedDrawingScope, setLoadedDrawingScope] = useState("");
  const [drawingBusy, setDrawingBusy] = useState(false);
  const [drawingError, setDrawingError] = useState<string | null>(null);
  const drawingScope = market && symbol ? `${market}:${symbol}` : "";
  const visibleDrawings = useMemo(
    () => loadedDrawingScope === drawingScope ? drawings : [],
    [drawingScope, drawings, loadedDrawingScope],
  );

  useEffect(() => { drawingModeRef.current = drawingMode; }, [drawingMode]);

  const stopDrawing = useCallback(() => {
    setDrawingMode(null);
    setTrendAnchor(null);
    setEditingDrawingId(null);
  }, []);

  const selectDrawingMode = useCallback((mode: Exclude<DrawingMode, null>) => {
    if (drawingMode === mode && !editingDrawingId) {
      stopDrawing();
      return;
    }
    setDrawingMode(mode);
    setTrendAnchor(null);
    setEditingDrawingId(null);
    setDrawingError(null);
  }, [drawingMode, editingDrawingId, stopDrawing]);

  useEffect(() => {
    if (!market || !symbol) return;
    let cancelled = false;
    api.get<ChartDrawing[]>(`/charts/drawings/${market}/${symbol}`)
      .then((response) => {
        if (cancelled) return;
        stopDrawing();
        setDrawings(response.data);
        setLoadedDrawingScope(`${market}:${symbol}`);
        setDrawingError(null);
      })
      .catch(() => {
        if (!cancelled) {
          setDrawings([]);
          setLoadedDrawingScope(`${market}:${symbol}`);
          setDrawingError(tr("stock.drawings.load_error"));
        }
      });
    return () => { cancelled = true; };
  }, [market, symbol, stopDrawing, tr]);

  const createHorizontal = useCallback(async (price: number) => {
    if (!market || !symbol || !Number.isFinite(price) || price <= 0 || drawingBusy) return;
    setDrawingBusy(true);
    try {
      const response = await api.post<ChartDrawing>("/charts/drawings", {
        market, symbol, kind: "horizontal",
        points: [{ price: Number(price.toFixed(6)) }],
        label: tr("stock.drawings.level"), color: "#f59e0b",
      });
      setDrawings((current) => [...current, response.data]);
      setLoadedDrawingScope(`${market}:${symbol}`);
      stopDrawing();
      setDrawingError(null);
    } catch {
      setDrawingError(tr("stock.drawings.save_error"));
    } finally {
      setDrawingBusy(false);
    }
  }, [drawingBusy, market, stopDrawing, symbol, tr]);

  const placeDrawingPoint = useCallback(async (point: DrawingPoint) => {
    if (!drawingMode || drawingBusy) return;
    if (drawingMode === "horizontal") {
      await createHorizontal(point.price);
      return;
    }
    if (!point.time) return;
    if (!trendAnchor) {
      setTrendAnchor(point);
      return;
    }
    if (trendAnchor.time === point.time) {
      setDrawingError(tr("stock.drawings.distinct_time"));
      return;
    }
    setDrawingBusy(true);
    try {
      const points = [trendAnchor, point];
      const response = editingDrawingId
        ? await api.patch<ChartDrawing>(`/charts/drawings/${editingDrawingId}`, { points })
        : await api.post<ChartDrawing>("/charts/drawings", {
          market, symbol, kind: "trend", points,
          label: tr("stock.drawings.trend_label"), color: "#38bdf8",
        });
      setDrawings((current) => editingDrawingId
        ? current.map((drawing) => drawing.id === editingDrawingId ? response.data : drawing)
        : [...current, response.data]);
      setLoadedDrawingScope(`${market}:${symbol}`);
      stopDrawing();
      setDrawingError(null);
    } catch {
      setDrawingError(tr(editingDrawingId ? "stock.drawings.update_error" : "stock.drawings.save_error"));
    } finally {
      setDrawingBusy(false);
    }
  }, [createHorizontal, drawingBusy, drawingMode, editingDrawingId, market, stopDrawing, symbol, tr, trendAnchor]);
  useEffect(() => {
    placePointRef.current = (point) => { void placeDrawingPoint(point); };
  }, [placeDrawingPoint]);

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
    const candle = chart.addSeries(CandlestickSeries, {
      upColor: t.up,
      downColor: t.down,
      borderUpColor: t.up,
      borderDownColor: t.down,
      wickUpColor: t.up,
      wickDownColor: t.down,
    });
    candleRef.current = candle;
    const onChartClick = (param: { point?: { y: number }; time?: Time }) => {
      if (!drawingModeRef.current || !param.point) return;
      const price = candle.coordinateToPrice(param.point.y);
      if (price == null) return;
      placePointRef.current({
        price: Number(Number(price).toFixed(6)),
        time: param.time == null ? null : serializeDrawingTime(param.time),
      });
    };
    chart.subscribeClick(onChartClick);

    // Volume histogram (overlay in lower 20% of price scale)
    const vol = chart.addSeries(HistogramSeries, {
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
      chart.unsubscribeClick(onChartClick);
      chart.remove();
      chartRef.current = null;
      candleRef.current = null;
      volRef.current = null;
      indicatorSeriesRef.current = [];
      drawingSeriesRef.current = [];
      priceLinesRef.current = [];
    };
  }, []);

  useEffect(() => {
    const chart = chartRef.current;
    const candle = candleRef.current;
    if (!chart || !candle) return;
    for (const line of priceLinesRef.current) candle.removePriceLine(line);
    for (const series of drawingSeriesRef.current) chart.removeSeries(series);
    drawingSeriesRef.current = [];
    priceLinesRef.current = visibleDrawings
      .filter((drawing) => drawing.kind === "horizontal" && drawing.points[0]?.price > 0)
      .map((drawing) => candle.createPriceLine({
        price: drawing.points[0].price,
        color: drawing.color,
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: drawing.label ?? tr("stock.drawings.level"),
      }));
    for (const drawing of visibleDrawings.filter((row) => row.kind === "trend")) {
      if (drawing.points.length !== 2 || drawing.points.some((point) => !point.time || point.price <= 0)) continue;
      const series = chart.addSeries(LineSeries, {
        color: drawing.color,
        lineWidth: 2,
        priceScaleId: "right",
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: true,
        crosshairMarkerRadius: 4,
      });
      const data = drawing.points.map((point) => ({
        time: parseDrawingTime(point.time!), value: point.price,
      })).sort((a, b) => drawingTimeOrder(a.time) - drawingTimeOrder(b.time));
      const latestBar = [...bars].sort((a, b) => String(a.time).localeCompare(String(b.time))).at(-1);
      if (latestBar) {
        const latestTime = toTime(latestBar.time);
        const lastAnchorTime = drawingTimeOrder(data[data.length - 1].time);
        const projected = projectTrendPrice(drawing.points, latestTime);
        if (projected != null && drawingTimeOrder(latestTime) > lastAnchorTime) {
          data.push({ time: latestTime, value: projected });
        }
      }
      series.setData(data);
      drawingSeriesRef.current.push(series);
    }
  }, [bars, visibleDrawings, theme, marketColorMode, tr]);

  async function removeDrawing(id: string) {
    if (drawingBusy) return;
    setDrawingBusy(true);
    try {
      await api.delete(`/charts/drawings/${id}`);
      setDrawings((current) => current.filter((drawing) => drawing.id !== id));
      setDrawingError(null);
    } catch {
      setDrawingError(tr("stock.drawings.delete_error"));
    } finally {
      setDrawingBusy(false);
    }
  }

  function editTrend(drawing: ChartDrawing) {
    setDrawingMode("trend");
    setTrendAnchor(null);
    setEditingDrawingId(drawing.id);
    setDrawingError(null);
  }

  async function createAlert(drawing: ChartDrawing) {
    if (drawingBusy || drawing.alert_id) return;
    const latestBar = [...bars].sort((a, b) => String(a.time).localeCompare(String(b.time))).at(-1);
    let condition: "above" | "below";
    if (drawing.kind === "trend") {
      const livePrice = currentPrice != null && Number.isFinite(currentPrice) && currentPrice > 0
        ? currentPrice : null;
      const liveTime = currentTime == null ? null : toTime(currentTime);
      const hasLiveReference = livePrice != null && liveTime != null;
      if (!latestBar && !hasLiveReference) {
        setDrawingError(tr("stock.drawings.alert_requires_price"));
        return;
      }
      const referenceTime = hasLiveReference
        ? liveTime
        : toTime(latestBar!.time);
      const referencePrice = hasLiveReference ? livePrice : latestBar!.close;
      const projected = projectTrendPrice(drawing.points, referenceTime);
      if (projected == null) {
        setDrawingError(tr("stock.drawings.alert_error"));
        return;
      }
      // Default to the next meaningful crossing from the current side:
      // below line → alert on upward break; above line → alert on breakdown.
      condition = referencePrice >= projected ? "below" : "above";
    } else {
      const price = drawing.points[0].price;
      condition = latestBar == null || price >= latestBar.close ? "above" : "below";
    }
    setDrawingBusy(true);
    try {
      const response = await api.post(`/charts/drawings/${drawing.id}/alert`, {
        condition, repeat: false, cooldown_seconds: 0,
      });
      setDrawings((current) => current.map((row) => row.id === drawing.id ? { ...row, alert_id: response.data.id } : row));
      setDrawingError(null);
    } catch {
      setDrawingError(tr("stock.drawings.alert_error"));
    } finally {
      setDrawingBusy(false);
    }
  }

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
      const s = chart.addSeries(LineSeries, {
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
      const hist = chart.addSeries(HistogramSeries, {
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
      {market && symbol && (
        <div className="flex min-h-9 flex-wrap items-center gap-1.5 border-b border-border px-2 py-1">
          <button
            type="button"
            aria-pressed={drawingMode === "horizontal"}
            onClick={() => selectDrawingMode("horizontal")}
            disabled={drawingBusy}
            className={`inline-flex items-center gap-1 rounded px-2 py-1 text-xs ${drawingMode === "horizontal" ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-accent/10"}`}
          >
            <Minus size={13} /> {drawingMode === "horizontal" ? tr("stock.drawings.click_chart") : tr("stock.drawings.horizontal")}
          </button>
          <button
            type="button"
            aria-pressed={drawingMode === "trend"}
            onClick={() => selectDrawingMode("trend")}
            disabled={drawingBusy}
            className={`inline-flex items-center gap-1 rounded px-2 py-1 text-xs ${drawingMode === "trend" ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-accent/10"}`}
          >
            <TrendingUp size={13} /> {drawingMode === "trend"
              ? tr(trendAnchor ? "stock.drawings.click_second" : "stock.drawings.click_first")
              : tr("stock.drawings.trend")}
          </button>
          {visibleDrawings.filter((drawing) => drawing.kind === "horizontal").map((drawing) => (
            <span key={drawing.id} className="inline-flex items-center gap-1 rounded border border-border px-2 py-0.5 text-xs" style={{ borderColor: drawing.color }}>
              {drawing.points[0].price.toLocaleString(undefined, { maximumFractionDigits: 4 })}
              <button type="button" onClick={() => void createAlert(drawing)} disabled={drawingBusy || !!drawing.alert_id} aria-label={drawing.alert_id ? tr("stock.drawings.alert_created") : tr("stock.drawings.create_alert")} title={drawing.alert_id ? tr("stock.drawings.alert_created") : tr("stock.drawings.create_alert")} className={drawing.alert_id ? "text-positive" : "text-muted-foreground hover:text-primary"}><Bell size={12} /></button>
              <button type="button" onClick={() => void removeDrawing(drawing.id)} disabled={drawingBusy} aria-label={tr("stock.drawings.delete")} className="text-muted-foreground hover:text-negative"><Trash2 size={12} /></button>
            </span>
          ))}
          {visibleDrawings.filter((drawing) => drawing.kind === "trend").map((drawing) => (
            <span key={drawing.id} className="inline-flex items-center gap-1 rounded border border-border px-2 py-0.5 text-xs" style={{ borderColor: drawing.color }}>
              <TrendingUp size={12} />
              {drawing.points.map((point) => point.price.toLocaleString(undefined, { maximumFractionDigits: 4 })).join(" → ")}
              <button type="button" onClick={() => void createAlert(drawing)} disabled={drawingBusy || !!drawing.alert_id} aria-label={drawing.alert_id ? tr("stock.drawings.alert_created") : tr("stock.drawings.create_trend_alert")} title={drawing.alert_id ? tr("stock.drawings.alert_created") : tr("stock.drawings.create_trend_alert")} className={drawing.alert_id ? "text-positive" : "text-muted-foreground hover:text-primary"}><Bell size={12} /></button>
              <button type="button" onClick={() => editTrend(drawing)} disabled={drawingBusy} aria-label={tr("stock.drawings.reposition")} title={tr("stock.drawings.reposition")} className="text-muted-foreground hover:text-primary"><Pencil size={12} /></button>
              <button type="button" onClick={() => void removeDrawing(drawing.id)} disabled={drawingBusy} aria-label={tr("stock.drawings.delete")} className="text-muted-foreground hover:text-negative"><Trash2 size={12} /></button>
            </span>
          ))}
          {drawingError && <span className="text-xs text-negative">{drawingError}</span>}
        </div>
      )}
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
