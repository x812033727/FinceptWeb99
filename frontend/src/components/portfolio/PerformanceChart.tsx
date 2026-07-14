import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import {
  CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { ChartTooltip } from "@/components/ui/ChartTooltip";
import api from "@/lib/api";

const PERIOD_DAYS: Record<string, number> = { "1M": 30, "3M": 90, "6M": 180, "1Y": 365 };
const PERIOD_TO_YFINANCE: Record<string, string> = { "1M": "1mo", "3M": "3mo", "6M": "6mo", "1Y": "1y" };
// Period → months for the TW history endpoint, which takes `months`
// (the TWSE / OHLCV archive cron stores daily bars and downsamples
// later; 1M ≈ 1 month, 1Y ≈ 12 months).
const PERIOD_TO_TW_MONTHS: Record<string, number> = { "1M": 1, "3M": 3, "6M": 6, "1Y": 12 };

function normalize(points: { date: string; value: number }[]): { date: string; indexed: number }[] {
  if (!points.length) return [];
  const base = points[0].value;
  if (base === 0) return [];
  return points.map((p) => ({ date: p.date.slice(5), indexed: parseFloat(((p.value / base) * 100).toFixed(2)) }));
}

/**
 * Dynamic benchmark choice:
 *
 *  - **TWD** portfolios → `_TAIEX_TR` (TAIEX 含息報酬指數, populated
 *    by `tasks/ingest_taiex_tr_history` from FinMind sponsor-tier).
 *    Includes dividend reinvestment, so it's apples-to-apples vs a
 *    portfolio's mark-to-market value (which also reinvests divs
 *    mechanically through ex-div price drops).
 *  - **USD** (and anything else) → SPY via yfinance, the long-
 *    standing default.
 *
 * Pre-PR #191 the chart hardcoded SPY for every portfolio currency,
 * so a TWD portfolio comparing against the S&P 500 looked artificially
 * good or bad depending on the period — silent benchmark drift.
 */
type BenchmarkInfo = {
  symbol: string;       // for queryKey / query stability
  label: string;        // shown in the chart legend
  fetcher: () => Promise<{ date: string; close: number }[]>;
};

function chooseBenchmark(
  currency: string | undefined,
  period: string,
): BenchmarkInfo {
  const yfinancePeriod = PERIOD_TO_YFINANCE[period];
  const twMonths = PERIOD_TO_TW_MONTHS[period];

  if ((currency || "").toUpperCase() === "TWD") {
    return {
      symbol: "_TAIEX_TR",
      label: "TAIEX TR (benchmark)",
      fetcher: async () => {
        // `/tw/history/{symbol}` short-circuits for synthetic-prefix
        // symbols and returns whatever's in `ohlcv_daily` for
        // `_TAIEX_TR`. Empty array if the cron hasn't populated yet
        // — the chart renders without the benchmark line in that
        // case, which is the right degraded UX.
        const res = await api.get<{ time: string; close: number }[]>(
          `/tw/history/_TAIEX_TR?months=${twMonths}`,
        );
        return res.data.map((b) => ({ date: b.time, close: b.close }));
      },
    };
  }

  return {
    symbol: "SPY",
    label: "SPY (benchmark)",
    fetcher: async () => {
      const res = await api.get<{ date: string; close: number }[]>(
        `/us/history/SPY?period=${yfinancePeriod}&interval=1d`,
      );
      return res.data;
    },
  };
}

export function PerformanceChart({
  portfolioId, currency,
}: { portfolioId: string; currency?: string }) {
  const { t } = useTranslation();
  const [period, setPeriod] = useState("3M");
  const days = PERIOD_DAYS[period];

  const { data: perfData = [], isLoading: perfLoading } = useQuery<{ date: string; value: number }[]>({
    queryKey: ["portfolio-performance", portfolioId, days],
    queryFn: () =>
      api.get(`/portfolio/${portfolioId}/performance?days=${days}`).then((r) => r.data),
    staleTime: 3_600_000,
  });

  const benchmark = chooseBenchmark(currency, period);

  const { data: benchmarkHistory = [] } = useQuery<{ date: string; close: number }[]>({
    queryKey: ["benchmark-history", benchmark.symbol, period],
    queryFn: benchmark.fetcher,
    staleTime: 3_600_000,
  });

  const portfolioIndexed = normalize(perfData);

  const benchmarkIndexed = (() => {
    if (!benchmarkHistory.length) return [];
    const base = benchmarkHistory[0].close;
    if (base === 0) return [];
    return benchmarkHistory.map((p) => ({
      date: p.date?.slice(0, 10).slice(5),
      benchmark: parseFloat(((p.close / base) * 100).toFixed(2)),
    }));
  })();

  // Merge on date key. Benchmark + portfolio rarely share every
  // trading day exactly (TW vs US calendars, weekend gaps), so
  // recharts gets a sparse series and `connectNulls` fills the gaps.
  const merged: Record<string, { date: string; portfolio?: number; benchmark?: number }> = {};
  for (const p of portfolioIndexed) merged[p.date] = { date: p.date, portfolio: p.indexed };
  for (const s of benchmarkIndexed) {
    if (merged[s.date]) merged[s.date].benchmark = s.benchmark;
    else merged[s.date] = { date: s.date, benchmark: s.benchmark };
  }
  const chartData = Object.values(merged).sort((a, b) => a.date.localeCompare(b.date));

  const hasPerfData = portfolioIndexed.length > 0;
  const hasBenchmark = benchmarkIndexed.length > 0;

  return (
    <div className="bg-card border border-border rounded-lg p-5 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-foreground font-medium">{t("portfolio.summary.performance")}</h2>
        </div>
        <div className="flex gap-1">
          {Object.keys(PERIOD_DAYS).map((p) => (
            <button
              key={p}
              onClick={() => setPeriod(p)}
              className={`px-2.5 py-1 text-xs rounded transition-colors ${
                period === p
                  ? "bg-primary text-primary-foreground"
                  : "text-muted-foreground hover:text-foreground"
              }`}
            >
              {p}
            </button>
          ))}
        </div>
      </div>

      {perfLoading ? (
        <div className="h-[200px] flex items-center justify-center text-muted-foreground text-sm animate-pulse">
          {t("common.loading")}
        </div>
      ) : !hasPerfData ? (
        <div className="h-[200px] flex items-center justify-center text-muted-foreground text-sm">
          {t("common.no_data")}
        </div>
      ) : (
        <ResponsiveContainer width="100%" height={200}>
          <LineChart data={chartData} margin={{ left: 0, right: 8, top: 4, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
            <XAxis
              dataKey="date"
              tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
              interval="preserveStartEnd"
            />
            <YAxis
              tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
              width={44}
              tickFormatter={(v) => v.toFixed(0)}
              domain={["auto", "auto"]}
            />
            <Tooltip content={<ChartTooltip valueFormatter={(v) => v.toFixed(2)} />} />
            <Legend wrapperStyle={{ fontSize: 11 }} />
            <Line
              type="monotone"
              dataKey="portfolio"
              name="Portfolio"
              stroke="hsl(var(--primary))"
              strokeWidth={2}
              dot={false}
              connectNulls
            />
            {hasBenchmark && (
              <Line
                type="monotone"
                dataKey="benchmark"
                name={benchmark.label}
                stroke="hsl(var(--muted-foreground))"
                strokeWidth={1.5}
                strokeDasharray="4 2"
                dot={false}
                connectNulls
              />
            )}
          </LineChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}
