/**
 * PR-5c: side-by-side strategy comparison.
 *
 * Multi-select up to 4 of the operator's strategies, see their
 * brier + hit-rate trajectories overlay-plotted, and a KPI table
 * underneath ranking by latest brier.
 */
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { ChartTooltip } from "@/components/ui/ChartTooltip";
import api from "@/lib/api";
import type {
  StrategyCompareEntry,
  StrategyCompareResponse,
} from "@/components/discussion/_helpers";
import { fetchStrategies, type StrategyTemplate } from "@/components/discussion/_helpers";
import { DataTable, type DataTableColumn } from "@/components/ui/table";


// Stable line colours per slot — operator can rely on "the green
// one is strategy A" while skimming.
const LINE_COLORS = [
  "hsl(var(--chart-1))", "hsl(var(--chart-2))",
  "hsl(var(--chart-3))", "hsl(var(--chart-4))",
];


export default function StrategyComparisonPage() {
  const { t } = useTranslation();
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [days, setDays] = useState<number>(90);

  const { data: strategies = [] } = useQuery({
    queryKey: ["strategies"],
    queryFn: () => fetchStrategies(),
  });

  const compare = useMutation({
    mutationFn: async (
      payload: { strategy_ids: string[]; days: number },
    ) => {
      const { data } = await api.post<StrategyCompareResponse>(
        "/discussion/strategies/compare", payload,
      );
      return data;
    },
  });

  function toggleStrategy(id: string) {
    setSelectedIds((prev) => {
      if (prev.includes(id)) return prev.filter((x) => x !== id);
      if (prev.length >= 4) return prev;   // hard cap
      return [...prev, id];
    });
  }

  function runCompare() {
    if (selectedIds.length === 0) return;
    compare.mutate({ strategy_ids: selectedIds, days });
  }

  const result = compare.data;
  const seriesByStrategy = useMemo(() => {
    if (!result) return [];
    return result.strategies.map((s, i) => ({
      ...s,
      color: LINE_COLORS[i % LINE_COLORS.length],
    }));
  }, [result]);

  // Build a unified date axis for recharts — recharts wants one
  // dataset with each strategy's value on the same row keyed by
  // date. Iterate every metric date across all strategies, then
  // fill each strategy's brier value (or null when missing).
  const chartData = useMemo(() => {
    if (seriesByStrategy.length === 0) return [];
    const allDates = new Set<string>();
    for (const s of seriesByStrategy) {
      for (const m of s.metrics) {
        allDates.add(m.date);
      }
    }
    const sortedDates = [...allDates].sort();
    return sortedDates.map((date) => {
      const row: Record<string, number | string | null> = { date };
      for (const s of seriesByStrategy) {
        const m = s.metrics.find((x) => x.date === date);
        row[`brier_${s.strategy_id}`] = m?.brier_30d ?? null;
        row[`hit_${s.strategy_id}`] = m?.hit_rate_30d ?? null;
      }
      return row;
    });
  }, [seriesByStrategy]);

  return (
    <div className="p-gutter sm:p-page space-y-stack max-w-6xl">
      <h1 className="text-title font-semibold">
        {t("strategy_compare.title")}
      </h1>

      {/* Strategy picker chips */}
      <div className="space-y-2">
        <div className="text-xs text-muted-foreground">
          {t("strategy_compare.pick", { count: selectedIds.length })}
        </div>
        <div className="flex flex-wrap gap-1.5">
          {strategies.map((s: StrategyTemplate) => {
            const on = selectedIds.includes(s.id);
            const slot = selectedIds.indexOf(s.id);
            const tone = on
              ? "border-2 text-foreground"
              : "border border-border text-muted-foreground hover:text-foreground";
            const style = on && slot >= 0
              ? { borderColor: LINE_COLORS[slot] }
              : {};
            return (
              <button
                key={s.id}
                type="button"
                onClick={() => toggleStrategy(s.id)}
                className={`px-2 py-0.5 text-[11px] rounded ${tone}`}
                style={style}
                disabled={!on && selectedIds.length >= 4}
              >
                {s.name}{" "}
                <span className="text-[9px] text-muted-foreground">
                  · {s.market}
                </span>
              </button>
            );
          })}
        </div>
        <div className="flex items-center gap-2">
          <label className="text-[11px] text-muted-foreground flex items-center gap-1">
            {t("strategy_compare.days")}:
            <select
              value={days}
              onChange={(e) => setDays(Number(e.target.value))}
              className="bg-background border border-border rounded px-1 py-0.5 text-[11px]"
            >
              <option value={30}>30</option>
              <option value={60}>60</option>
              <option value={90}>90</option>
              <option value={180}>180</option>
            </select>
          </label>
          <button
            type="button"
            onClick={runCompare}
            disabled={selectedIds.length === 0 || compare.isPending}
            className="px-2 py-0.5 text-[11px] bg-success text-white rounded
                       hover:bg-success/90 disabled:opacity-50"
          >
            {compare.isPending
              ? t("common.computing")
              : t("strategy_compare.run")}
          </button>
        </div>
      </div>

      {/* Chart */}
      {result && chartData.length > 0 && (
        <>
          <div>
            <div className="text-xs text-muted-foreground mb-1">
              {t("strategy_compare.brier_chart")}
            </div>
            <ResponsiveContainer width="100%" height={220}>
              <LineChart data={chartData}>
                <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
                <XAxis
                  dataKey="date"
                  tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
                />
                <YAxis
                  domain={[0, 1]}
                  tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
                  tickFormatter={(v: number) => v.toFixed(2)}
                  width={40}
                />
                <Tooltip content={<ChartTooltip />} />
                <Legend wrapperStyle={{ fontSize: 11 }} />
                {seriesByStrategy.map((s) => (
                  <Line
                    key={s.strategy_id}
                    type="monotone"
                    dataKey={`brier_${s.strategy_id}`}
                    stroke={s.color}
                    strokeWidth={2}
                    dot={false}
                    connectNulls={false}
                    name={s.name}
                  />
                ))}
              </LineChart>
            </ResponsiveContainer>
          </div>

          {/* KPI table */}
          <KpiTable strategies={seriesByStrategy} />
        </>
      )}
      {!result && selectedIds.length === 0 && (
        <div className="text-xs text-muted-foreground italic">
          {t("strategy_compare.empty_pick_hint")}
        </div>
      )}
    </div>
  );
}


function KpiTable({
  strategies,
}: {
  strategies: (StrategyCompareEntry & { color: string })[];
}) {
  const { t } = useTranslation();
  type KpiRow = StrategyCompareEntry & { color: string };
  const columns: DataTableColumn<KpiRow>[] = [
    {
      key: "name",
      header: t("strategy_compare.col.name"),
      mobile: "primary",
      render: (s) => (
        <>
          <span
            className="inline-block w-2 h-2 rounded-full mr-1"
            style={{ backgroundColor: s.color }}
          />
          {s.name}
        </>
      ),
    },
    {
      key: "tier",
      header: t("strategy_compare.col.tier"),
      render: (s) => (
        <span className="text-muted-foreground text-[10px]">
          {s.maturity_tier ?? "—"}
        </span>
      ),
    },
    {
      key: "brier_avg",
      header: t("strategy_compare.col.brier_avg"),
      numeric: true,
      render: (s) =>
        s.summary.brier_avg !== null ? s.summary.brier_avg.toFixed(3) : "—",
    },
    {
      key: "brier_latest",
      header: t("strategy_compare.col.brier_latest"),
      numeric: true,
      render: (s) =>
        s.summary.brier_latest !== null
          ? s.summary.brier_latest.toFixed(3)
          : "—",
    },
    {
      key: "hit_rate_latest",
      header: t("strategy_compare.col.hit_rate_latest"),
      numeric: true,
      render: (s) =>
        s.summary.hit_rate_latest !== null
          ? `${(s.summary.hit_rate_latest * 100).toFixed(0)}%`
          : "—",
    },
    {
      key: "samples",
      header: t("strategy_compare.col.samples"),
      numeric: true,
      render: (s) => s.summary.sample_total,
    },
  ];
  return (
    <DataTable
      columns={columns}
      rows={strategies}
      rowKey={(s) => s.strategy_id}
      mobileMode="cards"
      aria-label={t("strategy_compare.title")}
    />
  );
}
