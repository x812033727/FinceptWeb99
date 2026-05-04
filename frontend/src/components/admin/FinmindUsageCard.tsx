import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { CollapsibleHeader, useCollapsible } from "@/components/Collapsible";
import api, { errorDetail } from "@/lib/api";

/**
 * AdminPage usage chart for the FinMind clone subsystem.
 *
 * Reads `/api/admin/finmind/usage?days=N` and renders two views:
 *   - Calls + rows per UTC day (BarChart, last 7 / 30 / 90 days
 *     selectable)
 *   - Top 10 datasets by call count over the same window (table)
 *
 * Sits next to FinmindAdminCard. Kept as a separate card because:
 *   1. Operators check usage on a different cadence than catalog
 *      configuration — they don't need both expanded at once.
 *   2. The chart has its own range selector + auto-refresh that
 *      shouldn't reset every time someone toggles a dataset.
 */

interface UsageByDay {
  day: string;
  calls: number;
  rows: number;
}

interface UsageByDataset {
  dataset_code: string;
  calls: number;
  rows: number;
}

interface UsagePayload {
  window_days: number;
  by_day: UsageByDay[];
  by_dataset: UsageByDataset[];
}

const RANGE_OPTIONS = [7, 30, 90] as const;
type Range = (typeof RANGE_OPTIONS)[number];

function formatNumber(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

export default function FinmindUsageCard() {
  const { open, toggle } = useCollapsible("admin-finmind-usage", false);
  const [days, setDays] = useState<Range>(7);

  const usageQuery = useQuery<UsagePayload>({
    queryKey: ["admin", "finmind", "usage", days],
    queryFn: async () => {
      const r = await api.get<UsagePayload>(
        `/admin/finmind/usage?days=${days}`,
      );
      return r.data;
    },
    enabled: open,
    refetchInterval: 60_000,
  });

  const totalCalls = (usageQuery.data?.by_day ?? []).reduce(
    (s, d) => s + d.calls,
    0,
  );
  const totalRows = (usageQuery.data?.by_day ?? []).reduce(
    (s, d) => s + d.rows,
    0,
  );

  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <CollapsibleHeader
        title="FinMind Usage"
        subtitle={
          usageQuery.data
            ? `${formatNumber(totalCalls)} calls · ${formatNumber(totalRows)} rows · last ${days}d`
            : "click to expand"
        }
        open={open}
        toggle={toggle}
      />

      {open && (
        <div className="mt-4 space-y-6">
          {/* Range selector ─────────────────── */}
          <div className="flex items-center gap-2 text-sm">
            <span className="text-muted-foreground">Window</span>
            {RANGE_OPTIONS.map((d) => (
              <button
                key={d}
                type="button"
                onClick={() => setDays(d)}
                className={`rounded px-2 py-1 text-xs ${
                  days === d
                    ? "bg-primary text-primary-foreground"
                    : "border border-border text-muted-foreground hover:bg-muted"
                }`}
              >
                {d}d
              </button>
            ))}
          </div>

          {usageQuery.isError && (
            <div className="rounded border border-destructive bg-destructive/10 p-3 text-sm">
              Failed to load usage:{" "}
              {errorDetail(usageQuery.error)}
            </div>
          )}

          {usageQuery.data && (
            <>
              {/* Calls + rows per day chart ───── */}
              {usageQuery.data.by_day.length === 0 ? (
                <div className="rounded border border-border bg-muted/30 p-6 text-center text-sm text-muted-foreground">
                  No requests in the last {days} days yet. Issue an API
                  key and hit{" "}
                  <code className="rounded bg-muted px-1">
                    /api/finmind/data/...
                  </code>{" "}
                  to populate this chart.
                </div>
              ) : (
                <div>
                  <h3 className="mb-2 text-sm font-semibold">
                    Calls + rows per UTC day
                  </h3>
                  <ResponsiveContainer width="100%" height={220}>
                    <BarChart data={usageQuery.data.by_day}>
                      <CartesianGrid
                        stroke="hsl(var(--border))"
                        strokeDasharray="3 3"
                      />
                      <XAxis
                        dataKey="day"
                        tick={{
                          fill: "hsl(var(--muted-foreground))",
                          fontSize: 11,
                        }}
                      />
                      <YAxis
                        yAxisId="left"
                        orientation="left"
                        tickFormatter={formatNumber}
                        tick={{
                          fill: "hsl(var(--muted-foreground))",
                          fontSize: 11,
                        }}
                      />
                      <YAxis
                        yAxisId="right"
                        orientation="right"
                        tickFormatter={formatNumber}
                        tick={{
                          fill: "hsl(var(--muted-foreground))",
                          fontSize: 11,
                        }}
                      />
                      <Tooltip
                        formatter={(value: number) => formatNumber(value)}
                        labelStyle={{ color: "#000" }}
                      />
                      <Legend wrapperStyle={{ fontSize: 12 }} />
                      <Bar
                        yAxisId="left"
                        dataKey="calls"
                        fill="#3b82f6"
                        name="Calls"
                      />
                      <Bar
                        yAxisId="right"
                        dataKey="rows"
                        fill="#22c55e"
                        name="Rows"
                      />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              )}

              {/* Top datasets table ───── */}
              {usageQuery.data.by_dataset.length > 0 && (
                <div>
                  <h3 className="mb-2 text-sm font-semibold">
                    Top datasets ({days}d)
                  </h3>
                  <table className="w-full text-xs">
                    <thead className="border-b border-border text-left text-muted-foreground">
                      <tr>
                        <th className="py-1.5 pr-2">Dataset</th>
                        <th className="py-1.5 pr-2 text-right">Calls</th>
                        <th className="py-1.5 pr-2 text-right">Rows</th>
                        <th className="py-1.5 pr-2 text-right">
                          Rows/call
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {usageQuery.data.by_dataset
                        .slice(0, 10)
                        .map((d) => (
                          <tr
                            key={d.dataset_code}
                            className="border-b border-border last:border-b-0"
                          >
                            <td className="py-1 pr-2 font-mono">
                              {d.dataset_code}
                            </td>
                            <td className="py-1 pr-2 text-right">
                              {formatNumber(d.calls)}
                            </td>
                            <td className="py-1 pr-2 text-right">
                              {formatNumber(d.rows)}
                            </td>
                            <td className="py-1 pr-2 text-right text-muted-foreground">
                              {d.calls
                                ? Math.round(d.rows / d.calls).toLocaleString()
                                : "—"}
                            </td>
                          </tr>
                        ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}
