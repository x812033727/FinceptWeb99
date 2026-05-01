import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import {
  CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import api from "@/lib/api";

const PERIOD_DAYS: Record<string, number> = { "1M": 30, "3M": 90, "6M": 180, "1Y": 365 };
const PERIOD_TO_YFINANCE: Record<string, string> = { "1M": "1mo", "3M": "3mo", "6M": "6mo", "1Y": "1y" };

function normalize(points: { date: string; value: number }[]): { date: string; indexed: number }[] {
  if (!points.length) return [];
  const base = points[0].value;
  if (base === 0) return [];
  return points.map((p) => ({ date: p.date.slice(5), indexed: parseFloat(((p.value / base) * 100).toFixed(2)) }));
}

export function PerformanceChart({ portfolioId }: { portfolioId: string }) {
  const { t } = useTranslation();
  const [period, setPeriod] = useState("3M");
  const days = PERIOD_DAYS[period];
  const yfinancePeriod = PERIOD_TO_YFINANCE[period];

  const { data: perfData = [], isLoading: perfLoading } = useQuery<{ date: string; value: number }[]>({
    queryKey: ["portfolio-performance", portfolioId, days],
    queryFn: () =>
      api.get(`/portfolio/${portfolioId}/performance?days=${days}`).then((r) => r.data),
    staleTime: 3_600_000,
  });

  const { data: spyHistory = [] } = useQuery<{ date: string; close: number }[]>({
    queryKey: ["spy-history", yfinancePeriod],
    queryFn: () =>
      api.get(`/us/history/SPY?period=${yfinancePeriod}&interval=1d`).then((r) => r.data),
    staleTime: 3_600_000,
  });

  const portfolioIndexed = normalize(perfData);

  const spyIndexed = (() => {
    if (!spyHistory.length) return [];
    const base = spyHistory[0].close;
    if (base === 0) return [];
    return spyHistory.map((p) => ({ date: p.date?.slice(0, 10).slice(5), spy: parseFloat(((p.close / base) * 100).toFixed(2)) }));
  })();

  // Merge on date key
  const merged: Record<string, { date: string; portfolio?: number; spy?: number }> = {};
  for (const p of portfolioIndexed) merged[p.date] = { date: p.date, portfolio: p.indexed };
  for (const s of spyIndexed) {
    if (merged[s.date]) merged[s.date].spy = s.spy;
    else merged[s.date] = { date: s.date, spy: s.spy };
  }
  const chartData = Object.values(merged).sort((a, b) => a.date.localeCompare(b.date));

  const hasPerfData = portfolioIndexed.length > 0;

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
            <Tooltip
              contentStyle={{
                background: "hsl(var(--card))",
                border: "1px solid hsl(var(--border))",
                fontSize: 11,
              }}
              formatter={(v: number, name: string) => [`${v.toFixed(2)}`, name]}
            />
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
            <Line
              type="monotone"
              dataKey="spy"
              name="SPY (benchmark)"
              stroke="hsl(var(--muted-foreground))"
              strokeWidth={1.5}
              strokeDasharray="4 2"
              dot={false}
              connectNulls
            />
          </LineChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}
