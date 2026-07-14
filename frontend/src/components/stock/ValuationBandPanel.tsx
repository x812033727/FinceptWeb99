import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import {
  Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { ChartTooltip } from "@/components/ui/ChartTooltip";
import { Loading } from "./_atoms";
import { fetchValuationBand } from "./_shared";

export function ValuationBandPanel({ symbol }: { symbol: string }) {
  const { t } = useTranslation();
  const [metric, setMetric] = useState<"pe" | "pb">("pe");
  const { data, isLoading } = useQuery({
    queryKey: ["valuation-band", symbol, metric],
    queryFn: () => fetchValuationBand(symbol, metric),
    staleTime: 6 * 3_600_000,
    gcTime: 24 * 3_600_000, // valuation tier — cache for a day
  });

  if (isLoading) return <Loading />;
  if (!data || !data.series.length) {
    return <div className="p-6 text-muted-foreground text-sm">{t("stock.valuation.no_data")}</div>;
  }

  const { series, stats } = data;
  const filtered = series.filter((s) => s.value != null);
  if (!filtered.length) {
    return <div className="p-6 text-muted-foreground text-sm">{t("stock.valuation.no_data")}</div>;
  }

  // Sample to ~250 points so Recharts stays smooth on 5y of daily data.
  const stride = Math.max(1, Math.floor(filtered.length / 250));
  const chartData = filtered.filter((_, i) => i % stride === 0);

  const mean = stats.mean ?? 0;
  const std = stats.std ?? 0;
  // Band colours are valuation semantics (expensive→cheap), not market
  // direction — --danger/--warning/--success, not --up/--down.
  const bands = std > 0 ? [
    { v: mean + 2 * std, label: "+2σ", color: "hsl(var(--danger))" },
    { v: mean + std,     label: "+1σ", color: "hsl(var(--warning))" },
    { v: mean,           label: "μ",   color: "hsl(var(--chart-3))" },
    { v: mean - std,     label: "-1σ", color: "hsl(var(--success))" },
    { v: mean - 2 * std, label: "-2σ", color: "hsl(var(--chart-4))" },
  ] : [];

  // Percentile of `current` against history.
  let percentile: number | null = null;
  if (stats.current != null) {
    const sorted = [...filtered.map((s) => s.value as number)].sort((a, b) => a - b);
    const lessEq = sorted.filter((v) => v <= (stats.current as number)).length;
    percentile = (lessEq / sorted.length) * 100;
  }

  return (
    <div className="p-4 space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex rounded border border-border overflow-hidden">
          {(["pe", "pb"] as const).map((m) => (
            <button
              key={m}
              onClick={() => setMetric(m)}
              className={`px-4 py-1.5 text-sm transition-colors ${
                metric === m
                  ? "bg-primary text-primary-foreground"
                  : "text-muted-foreground hover:text-foreground"
              }`}
            >
              {m === "pe" ? t("stock.valuation.pe_band") : t("stock.valuation.pb_band")}
            </button>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <div className="bg-background border border-border rounded p-3">
          <div className="text-xs text-muted-foreground">{t("stock.valuation.current")}</div>
          <div className="text-lg font-semibold text-foreground">
            {stats.current == null ? "—" : stats.current.toFixed(2)}
          </div>
        </div>
        <div className="bg-background border border-border rounded p-3">
          <div className="text-xs text-muted-foreground">{t("stock.valuation.mean")}</div>
          <div className="text-lg font-semibold text-foreground">
            {stats.mean == null ? "—" : stats.mean.toFixed(2)}
          </div>
        </div>
        <div className="bg-background border border-border rounded p-3">
          <div className="text-xs text-muted-foreground">{t("stock.valuation.z_score")}</div>
          <div className={`text-lg font-semibold ${
            stats.current_z == null ? "text-muted-foreground"
            : stats.current_z > 1 ? "text-danger"
            : stats.current_z < -1 ? "text-success"
            : "text-foreground"
          }`}>
            {stats.current_z == null ? "—" : stats.current_z.toFixed(2)}
          </div>
        </div>
        <div className="bg-background border border-border rounded p-3">
          <div className="text-xs text-muted-foreground">{t("stock.valuation.percentile")}</div>
          <div className="text-lg font-semibold text-foreground">
            {percentile == null ? "—" : `${percentile.toFixed(0)}%`}
          </div>
        </div>
      </div>

      <ResponsiveContainer width="100%" height={320}>
        <LineChart data={chartData} margin={{ left: 0, right: 8, top: 8, bottom: 0 }}>
          <XAxis
            dataKey="date"
            tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
            interval="preserveStartEnd"
            minTickGap={40}
          />
          <YAxis
            tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
            width={45}
            domain={["auto", "auto"]}
          />
          <Tooltip content={<ChartTooltip valueFormatter={(v) => v?.toFixed(2) ?? "—"} />} />
          {bands.map((b) => (
            <ReferenceLine
              key={b.label}
              y={b.v}
              stroke={b.color}
              strokeDasharray="4 4"
              label={{ value: `${b.label} ${b.v.toFixed(1)}`, position: "right",
                       fill: b.color, fontSize: 10 }}
            />
          ))}
          <Line
            type="monotone"
            dataKey="value"
            stroke="hsl(var(--chart-1))"
            strokeWidth={1.5}
            dot={false}
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>

      <div className="grid grid-cols-2 sm:grid-cols-5 gap-2 text-xs">
        {([
          ["min", t("stock.valuation.min")],
          ["p25", "P25"],
          ["p50", t("stock.valuation.median")],
          ["p75", "P75"],
          ["max", t("stock.valuation.max")],
        ] as const).map(([k, label]) => (
          <div key={k} className="bg-background border border-border rounded px-2 py-1.5 flex justify-between">
            <span className="text-muted-foreground">{label}</span>
            <span className="text-foreground font-medium">
              {stats[k] == null ? "—" : (stats[k] as number).toFixed(2)}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
