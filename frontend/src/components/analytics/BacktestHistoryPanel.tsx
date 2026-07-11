/**
 * C3 — 歷史回測 list + side-by-side comparison.
 *
 * Lists the caller's saved backtest runs (GET /analytics/backtest-runs),
 * lets them tick 2–4 runs and open a comparison view backed by
 * GET /analytics/backtest-runs/compare — an overlay chart of equity
 * curves normalised to 100 at each run's own start, plus a metrics
 * table. Series colors follow the --chart-1..4 tokens in run order.
 */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid,
  Tooltip, Legend, ResponsiveContainer, ReferenceLine,
} from "recharts";
import { useTranslation } from "react-i18next";
import api from "@/lib/api";
import { Num, DeltaText } from "@/components/Num";
import {
  CompareResponse, MAX_COMPARE, RunListResponse, toChartRows,
} from "./compare";

const SERIES_COLORS = [1, 2, 3, 4].map(n => `hsl(var(--chart-${n}))`);

export default function BacktestHistoryPanel() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [selected, setSelected] = useState<string[]>([]);
  const [compareIds, setCompareIds] = useState<string | null>(null);

  const runsQuery = useQuery({
    queryKey: ["backtest-runs"],
    queryFn: () => api.get("/analytics/backtest-runs?limit=50").then(r => r.data as RunListResponse),
  });

  const compareQuery = useQuery({
    queryKey: ["backtest-runs-compare", compareIds],
    queryFn: () =>
      api.get(`/analytics/backtest-runs/compare?ids=${compareIds}`).then(r => r.data as CompareResponse),
    enabled: compareIds != null,
  });

  const deleteRun = useMutation({
    mutationFn: (id: string) => api.delete(`/analytics/backtest-runs/${id}`),
    onSuccess: (_res, id) => {
      setSelected(sel => sel.filter(s => s !== id));
      setCompareIds(null);
      qc.invalidateQueries({ queryKey: ["backtest-runs"] });
    },
  });

  function toggle(id: string) {
    setSelected(sel =>
      sel.includes(id)
        ? sel.filter(s => s !== id)
        : sel.length >= MAX_COMPARE ? sel : [...sel, id],
    );
  }

  function strategyLabel(strategy: string): string {
    return t(`analytics.backtest.strategy_${strategy}`, strategy);
  }
  function runLabel(run: { name: string | null; strategy: string }): string {
    return run.name || t("analytics.backtest.unnamed");
  }

  const items = runsQuery.data?.items ?? [];
  const compare = compareIds != null ? compareQuery.data : undefined;
  const chartRows = compare ? toChartRows(compare) : [];

  const metricRows: { key: string; label: string; render: (m: Record<string, number | undefined>) => React.ReactNode }[] = [
    {
      key: "total_return_pct", label: t("analytics.backtest.total_return"),
      render: m => <DeltaText value={m.total_return_pct} percent />,
    },
    {
      key: "annualised_return_pct", label: t("analytics.backtest.annualized_return"),
      render: m => <DeltaText value={m.annualised_return_pct} percent />,
    },
    {
      key: "sharpe_ratio", label: t("analytics.backtest.sharpe"),
      render: m => <Num value={m.sharpe_ratio} decimals={2} />,
    },
    {
      key: "max_drawdown_pct", label: t("analytics.backtest.max_drawdown"),
      render: m => <Num value={m.max_drawdown_pct} format="percent" className="text-negative" />,
    },
    {
      key: "win_rate", label: t("analytics.backtest.win_rate"),
      render: m => <Num value={m.win_rate != null ? m.win_rate * 100 : null} format="percent" decimals={1} />,
    },
    {
      key: "total_trades", label: t("analytics.backtest.trades"),
      render: m => <Num value={m.total_trades} decimals={0} />,
    },
    {
      key: "final_value", label: t("portfolio.summary.total_value"),
      render: m => <Num value={m.final_value} decimals={0} />,
    },
  ];

  return (
    <div className="bg-card border border-border rounded-lg p-5 space-y-4">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <h2 className="text-foreground font-medium">{t("analytics.backtest.history")}</h2>
        <div className="flex items-center gap-3">
          <span className="text-xs text-muted-foreground">{t("analytics.backtest.compare_hint")}</span>
          <button
            type="button"
            disabled={selected.length < 2}
            onClick={() => setCompareIds(selected.join(","))}
            className="px-3 py-1.5 bg-primary text-primary-foreground text-xs rounded hover:opacity-90 disabled:opacity-50"
          >
            {t("analytics.backtest.compare")} ({selected.length})
          </button>
        </div>
      </div>

      {runsQuery.isLoading && <p className="text-xs text-muted-foreground">{t("common.loading")}</p>}
      {!runsQuery.isLoading && items.length === 0 && (
        <p className="text-sm text-muted-foreground">{t("analytics.backtest.history_empty")}</p>
      )}

      {items.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-xs text-muted-foreground">
                <th className="w-8"></th>
                <th className="text-left pb-2 font-normal">{t("analytics.backtest.run_name")}</th>
                <th className="text-left pb-2 font-normal">{t("analytics.backtest.strategy")}</th>
                <th className="text-left pb-2 font-normal">{t("analytics.backtest.run_date")}</th>
                <th className="text-right pb-2 font-normal">{t("analytics.backtest.total_return")}</th>
                <th className="w-16"></th>
              </tr>
            </thead>
            <tbody>
              {items.map(run => (
                <tr key={run.id} className="border-t border-border/30">
                  <td className="py-2">
                    <input
                      type="checkbox"
                      aria-label={`select-${run.id}`}
                      checked={selected.includes(run.id)}
                      onChange={() => toggle(run.id)}
                    />
                  </td>
                  <td className="py-2 text-foreground">{runLabel(run)}</td>
                  <td className="py-2 text-muted-foreground">{strategyLabel(run.strategy)}</td>
                  <td className="py-2 text-muted-foreground font-mono tabular-nums">
                    {run.created_at.slice(0, 10)}
                  </td>
                  <td className="py-2 text-right">
                    <DeltaText value={run.metrics.total_return_pct} percent />
                  </td>
                  <td className="py-2 text-right">
                    <button
                      type="button"
                      onClick={() => {
                        if (window.confirm(t("analytics.backtest.delete_confirm"))) {
                          deleteRun.mutate(run.id);
                        }
                      }}
                      className="text-xs text-negative hover:underline"
                    >
                      {t("common.delete")}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {compareIds != null && compareQuery.isLoading && (
        <p className="text-xs text-muted-foreground">{t("common.loading")}</p>
      )}
      {compareQuery.isError && (
        <p className="text-negative text-xs">
          {(compareQuery.error as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? String(compareQuery.error)}
        </p>
      )}

      {compare && compare.runs.length > 0 && (
        <div className="space-y-4" data-testid="backtest-compare">
          <div>
            <p className="text-xs text-muted-foreground mb-2">
              {t("analytics.backtest.normalized_equity")}
            </p>
            <ResponsiveContainer width="100%" height={280}>
              <LineChart data={chartRows} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
                <XAxis
                  dataKey="date"
                  tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
                  tickFormatter={d => String(d).slice(0, 7)}
                  interval={Math.max(0, Math.floor(chartRows.length / 8))}
                />
                <YAxis
                  tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
                  domain={["auto", "auto"]}
                  width={48}
                />
                <Tooltip
                  contentStyle={{ background: "hsl(var(--card))", border: "1px solid hsl(var(--border))", borderRadius: 6 }}
                  labelStyle={{ color: "hsl(var(--muted-foreground))", fontSize: 11 }}
                  formatter={(v: number) => v?.toFixed(2)}
                />
                <Legend wrapperStyle={{ fontSize: 11 }} />
                <ReferenceLine y={100} stroke="hsl(var(--muted-foreground))" strokeDasharray="4 2" />
                {compare.runs.map((run, i) => (
                  <Line
                    key={run.id}
                    dataKey={`r${i}`}
                    name={runLabel(run)}
                    type="monotone"
                    stroke={SERIES_COLORS[i % SERIES_COLORS.length]}
                    strokeWidth={1.5}
                    dot={false}
                    connectNulls={false}
                  />
                ))}
              </LineChart>
            </ResponsiveContainer>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-xs text-muted-foreground">
                  <th className="text-left pb-2 font-normal">{t("analytics.backtest.metric")}</th>
                  {compare.runs.map((run, i) => (
                    <th key={run.id} className="text-right pb-2 font-normal">
                      <span style={{ color: SERIES_COLORS[i % SERIES_COLORS.length] }}>●</span>{" "}
                      {runLabel(run)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {metricRows.map(row => (
                  <tr key={row.key} className="border-t border-border/30">
                    <td className="py-1.5 text-muted-foreground">{row.label}</td>
                    {compare.runs.map(run => (
                      <td key={run.id} className="py-1.5 text-right">
                        {row.render(run.metrics)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
