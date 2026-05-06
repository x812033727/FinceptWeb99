/**
 * Per-strategy historical Brier line chart (audit Workflow Win #1).
 *
 * The single most operationally meaningful answer to "did the
 * learning loop actually work?" — not point-in-time Brier numbers
 * (those live in SweepAggregateCard's BrierRow) but the trajectory
 * across every completed sweep that referenced this strategy.
 *
 * Two lines:
 *   - amber: raw Brier (synthesizer's emitted confidence)
 *   - emerald: calibrated Brier (after isotonic curve, PR-C2)
 *
 * Reading the chart:
 *   - Both lines descending = the strategy is genuinely getting
 *     better at predicting its own market (lessons + persona
 *     weight learning are kicking in)
 *   - Emerald consistently below amber = the calibration curve
 *     is reducing residual error from raw confidence
 *   - Emerald rising above amber = the curve is fitted to a
 *     stale regime; consider triggering a manual re-fit via
 *     POST /strategies/{id}/learn (re-runs both weight learner
 *     and isotonic fit)
 *   - Both lines flat = either the strategy doesn't have enough
 *     data yet or learning has plateaued — both are diagnostic
 *     signals worth knowing about
 */
import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  fetchStrategyBrierHistory,
  type BrierHistoryPoint,
} from "./_helpers";


export function BrierTrendChart({
  strategyId,
  windowDays = 90,
}: {
  strategyId: string;
  windowDays?: number;
}) {
  const { t } = useTranslation();
  const { data, isLoading, error } = useQuery({
    queryKey: ["strategy-brier-history", strategyId, windowDays],
    queryFn: () => fetchStrategyBrierHistory(strategyId, windowDays),
  });

  // Memoize so recharts doesn't redraw on every render. Sort by
  // completed_at for stable ordering (the API already returns
  // ascending but we double-check defensively).
  const points = useMemo(
    () => sortPoints(data ?? []).map(toChartPoint),
    [data],
  );

  if (isLoading) {
    return (
      <p className="text-[11px] text-muted-foreground animate-pulse px-1 py-0.5">
        {t("common.loading")}
      </p>
    );
  }
  if (error) {
    return (
      <p className="text-[11px] text-red-300 px-1 py-0.5">
        {(error as Error).message}
      </p>
    );
  }
  if (points.length === 0) {
    return (
      <p className="text-[11px] text-muted-foreground px-1 py-0.5">
        {t(
          "strategy.brier_trend_empty",
          "尚未有已 resolve 的 sweep — 訓練資料還在累積中",
        )}
      </p>
    );
  }

  return (
    <div className="bg-secondary/20 border border-border rounded p-2 space-y-1">
      <p
        className="text-[10px] text-muted-foreground uppercase tracking-wider"
        title={t(
          "strategy.brier_trend_tip",
          "每個 sweep 一個點。下降 = 整體預測誤差變小;" +
          "Calibrated 持續低於 Raw = isotonic 曲線在發揮作用。",
        )}
      >
        {t(
          "strategy.brier_trend_label",
          "Brier 趨勢({{n}}d 內 sweep)",
          { n: windowDays },
        )}
      </p>
      <div style={{ width: "100%", height: 180 }}>
        <ResponsiveContainer>
          <LineChart data={points}>
            <CartesianGrid
              stroke="hsl(var(--border))"
              strokeOpacity={0.3}
              strokeDasharray="3 3"
            />
            <XAxis
              dataKey="label"
              stroke="hsl(var(--muted-foreground))"
              fontSize={10}
              tickMargin={4}
            />
            <YAxis
              domain={[0, 0.5]}
              ticks={[0, 0.1, 0.2, 0.25, 0.3, 0.4, 0.5]}
              stroke="hsl(var(--muted-foreground))"
              fontSize={10}
              width={28}
            />
            <Tooltip
              contentStyle={{
                background: "hsl(var(--background))",
                border: "1px solid hsl(var(--border))",
                fontSize: "11px",
              }}
              labelStyle={{ color: "hsl(var(--foreground))" }}
              formatter={(value) =>
                typeof value === "number" ? value.toFixed(3) : "—"
              }
            />
            <Legend
              wrapperStyle={{ fontSize: "10px" }}
            />
            {/* Reference line at 0.25 — Brier of a coin flip
                with uniform 0.5 confidence. Anything above means
                the synthesizer is worse than guessing. */}
            <ReferenceLine
              y={0.25}
              stroke="hsl(var(--muted-foreground))"
              strokeDasharray="2 2"
              label={{
                value: t("strategy.brier_baseline", "亂猜基線"),
                position: "right",
                fontSize: 9,
                fill: "hsl(var(--muted-foreground))",
              }}
            />
            <Line
              type="monotone"
              dataKey="raw_brier"
              name={t("strategy.brier_raw_legend", "Raw")}
              stroke="#f59e0b"
              strokeWidth={2}
              dot={{ r: 2 }}
              connectNulls
            />
            <Line
              type="monotone"
              dataKey="calibrated_brier"
              name={t("strategy.brier_calibrated_legend", "Calibrated")}
              stroke="#10b981"
              strokeWidth={2}
              dot={{ r: 2 }}
              connectNulls
            />
          </LineChart>
        </ResponsiveContainer>
      </div>
      <p className="text-[9px] text-muted-foreground">
        {t(
          "strategy.brier_trend_legend",
          "🟠 raw confidence  🟢 isotonic-calibrated  ┄ 0.5 uniform baseline",
        )}
      </p>
    </div>
  );
}


function sortPoints(rows: BrierHistoryPoint[]): BrierHistoryPoint[] {
  return [...rows].sort((a, b) => {
    const ax = a.completed_at ?? "";
    const bx = b.completed_at ?? "";
    return ax.localeCompare(bx);
  });
}


interface ChartPoint {
  label: string;
  raw_brier: number | null;
  calibrated_brier: number | null;
  fold_kind: string | null;
}


function toChartPoint(p: BrierHistoryPoint): ChartPoint {
  // Use anchor_date for the X-axis label — operator thinks in
  // terms of "the sweep that anchored 2026-04-15" not "the
  // sweep that completed at 14:32:08". Fall back to ISO date
  // from completed_at when anchor is missing.
  let label = p.anchor_date ?? "";
  if (!label && p.completed_at) {
    label = p.completed_at.slice(0, 10);
  }
  return {
    label: label.slice(5) || "?",   // MM-DD trims YYYY for chart space
    raw_brier: p.raw_brier,
    calibrated_brier: p.calibrated_brier,
    fold_kind: p.fold_kind,
  };
}
