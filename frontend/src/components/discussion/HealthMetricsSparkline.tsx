/**
 * PR-4b: rolling-30 health metrics sparkline.
 *
 * One chart, two y-axes — brier (lower-better) on the left, hit
 * rate (higher-better) on the right. Color-codes any point whose
 * `status_flags` is non-empty with a red dot so the operator sees
 * the day(s) the cron flagged drift / collapse / low-sample
 * inline with the trend.
 */
import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import {
  CartesianGrid,
  Dot,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import api from "@/lib/api";
import type { StrategyHealthSnapshot } from "./_helpers";


function StatusFlagDot(props: any) {
  const { cx, cy, payload } = props;
  if (!payload || !Array.isArray(payload.status_flags)) {
    return null;
  }
  if (payload.status_flags.length === 0) return null;
  return (
    <Dot
      cx={cx}
      cy={cy}
      r={4}
      fill="hsl(var(--destructive))"
      stroke="hsl(var(--destructive))"
    />
  );
}


export function HealthMetricsSparkline({
  strategyId,
  windowDays = 30,
}: {
  strategyId: string;
  windowDays?: number;
}) {
  const { t } = useTranslation();
  const { data, isLoading } = useQuery({
    queryKey: ["strategy-health", strategyId, windowDays],
    queryFn: async () => {
      const { data } = await api.get<{
        strategy_id: string;
        days: number;
        snapshots: StrategyHealthSnapshot[];
      }>(`/discussion/strategies/${strategyId}/health?days=${windowDays}`);
      return data;
    },
    staleTime: 60_000,
  });

  const series = useMemo(() => {
    const rows = (data?.snapshots ?? []).slice();
    // API returns newest-first; chart wants oldest-left → reverse.
    rows.reverse();
    return rows.map((s) => ({
      date: s.snapshot_date,
      brier: s.brier_30d,
      hitRate: s.hit_rate_30d,
      status_flags: s.status_flags,
    }));
  }, [data]);

  if (isLoading) {
    return (
      <div className="text-[11px] text-muted-foreground italic mt-2">
        {t("common.loading")}
      </div>
    );
  }
  if (series.length === 0) {
    return (
      <div className="text-[11px] text-muted-foreground italic mt-2">
        {t("discussion.health.empty")}
      </div>
    );
  }

  return (
    <div className="mt-2">
      <div className="text-[11px] text-muted-foreground mb-1">
        {t("discussion.health.title", { days: windowDays })}
      </div>
      <ResponsiveContainer width="100%" height={120}>
        <LineChart data={series} margin={{ top: 4, right: 8, bottom: 4, left: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
          <XAxis
            dataKey="date"
            tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
            tickFormatter={(d: string) => d.slice(5)}
          />
          <YAxis
            yAxisId="brier"
            domain={[0, 1]}
            tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
            tickFormatter={(v: number) => v.toFixed(2)}
            width={32}
          />
          <YAxis
            yAxisId="hit"
            orientation="right"
            domain={[0, 1]}
            tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
            tickFormatter={(v: number) => `${Math.round(v * 100)}%`}
            width={32}
          />
          <Tooltip
            wrapperStyle={{ fontSize: 11 }}
            formatter={(value: number, name: string) => {
              if (name === "hitRate") {
                return [`${(value * 100).toFixed(1)}%`, t("discussion.health.hit_rate")];
              }
              return [value?.toFixed(3), t("discussion.health.brier")];
            }}
            labelFormatter={(label) => label}
            contentStyle={{
              backgroundColor: "hsl(var(--popover))",
              borderColor: "hsl(var(--border))",
            }}
          />
          <Line
            yAxisId="brier"
            type="monotone"
            dataKey="brier"
            stroke="hsl(var(--chart-2))"
            strokeWidth={2}
            dot={<StatusFlagDot />}
            activeDot={{ r: 4 }}
            connectNulls={false}
            name="brier"
          />
          <Line
            yAxisId="hit"
            type="monotone"
            dataKey="hitRate"
            stroke="hsl(var(--chart-4))"
            strokeWidth={2}
            dot={false}
            activeDot={{ r: 4 }}
            connectNulls={false}
            name="hitRate"
          />
        </LineChart>
      </ResponsiveContainer>
      <div className="flex items-center justify-end gap-3 text-[10px] text-muted-foreground mt-0.5">
        <span className="flex items-center gap-1">
          <span className="inline-block w-2 h-2 rounded-full" style={{ background: "hsl(var(--chart-2))" }} />
          {t("discussion.health.brier")}
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block w-2 h-2 rounded-full" style={{ background: "hsl(var(--chart-4))" }} />
          {t("discussion.health.hit_rate")}
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block w-2 h-2 rounded-full" style={{ background: "hsl(var(--destructive))" }} />
          {t("discussion.health.flagged")}
        </span>
      </div>
    </div>
  );
}
