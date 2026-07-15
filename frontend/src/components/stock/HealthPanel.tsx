import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import { Bar, BarChart, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { ChartTooltip } from "@/components/ui/ChartTooltip";
import { Loading } from "./_atoms";
import { fetchHealth, fmtPct1 } from "./_shared";
import type { HealthPeriod, Light } from "./_shared";
import { DataTable, type DataTableColumn } from "../ui/table";

const LIGHT_CLASS: Record<Light, string> = {
  green:  "bg-success",
  yellow: "bg-warning",
  red:    "bg-danger",
  gray:   "bg-muted-foreground/40",
};

function LightDot({ light }: { light: Light }) {
  return <span className={`inline-block w-2.5 h-2.5 rounded-full ${LIGHT_CLASS[light]}`} />;
}

function HealthSection({
  title, light, children,
}: { title: string; light: Light; children: ReactNode }) {
  return (
    <div className="bg-card border border-border rounded-lg p-4">
      <div className="flex items-center gap-2 mb-3">
        <LightDot light={light} />
        <h4 className="text-sm font-semibold text-foreground">{title}</h4>
      </div>
      {children}
    </div>
  );
}

function MetricSparkRow({
  label, periods, accessor, suffix = "%",
}: {
  label: string;
  periods: HealthPeriod[];
  accessor: (p: HealthPeriod) => number | null;
  suffix?: string;
}) {
  const values = periods.map(accessor);
  const latest = values[values.length - 1];
  const data = periods.map((p, i) => ({
    date: p.date.slice(0, 7),
    value: values[i],
  }));
  return (
    <div>
      <div className="flex items-baseline justify-between mb-1">
        <span className="text-xs text-muted-foreground">{label}</span>
        <span className="text-sm font-medium text-foreground">
          {latest == null ? "—" : `${latest.toFixed(2)}${suffix}`}
        </span>
      </div>
      <ResponsiveContainer width="100%" height={42}>
        <BarChart data={data} margin={{ left: 0, right: 0, top: 4, bottom: 0 }}>
          <XAxis dataKey="date" hide />
          <YAxis hide />
          <Tooltip content={<ChartTooltip valueFormatter={(v) => `${v == null ? "—" : v.toFixed(2)}${suffix}`} />} />
          <Bar dataKey="value" radius={[2, 2, 0, 0]}>
            {data.map((d, i) => (
              <Cell
                key={i}
                fill={d.value == null
                  ? "hsl(var(--muted-foreground) / 0.2)"
                  : d.value >= 0 ? "hsl(var(--up))" : "hsl(var(--down))"}
              />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

export function HealthPanel({ symbol }: { symbol: string }) {
  const { t } = useTranslation();
  const { data, isLoading } = useQuery({
    queryKey: ["health", symbol],
    queryFn: () => fetchHealth(symbol),
    staleTime: 6 * 3_600_000,    // 6 hours; quarterlies don't change often
    gcTime: 24 * 3_600_000,      // fundamentals tier — cache for a day
  });

  if (isLoading) return <Loading />;
  if (!data || !data.periods.length) {
    return <div className="p-6 text-muted-foreground text-sm">{t("stock.health.no_data")}</div>;
  }

  const { periods, summary, lights } = data;
  const quality = data.quality;

  const periodRows = [...periods].reverse();
  const columns: DataTableColumn<HealthPeriod>[] = [
    {
      key: "date",
      header: t("stock.health.period"),
      render: (p) => <span className="text-muted-foreground">{p.date}</span>,
    },
    {
      key: "revenue_yoy",
      header: t("stock.health.quarter_revenue_yoy"),
      numeric: true,
      cellClassName: "text-foreground",
      render: (p) => fmtPct1(p.revenue_yoy),
    },
    {
      key: "gross_margin",
      header: t("stock.health.gross_margin"),
      numeric: true,
      cellClassName: "text-foreground",
      render: (p) => fmtPct1(p.gross_margin),
    },
    {
      key: "operating_margin",
      header: t("stock.health.operating_margin"),
      numeric: true,
      cellClassName: "text-foreground",
      render: (p) => fmtPct1(p.operating_margin),
    },
    {
      key: "net_margin",
      header: t("stock.health.net_margin"),
      numeric: true,
      cellClassName: "text-foreground",
      render: (p) => fmtPct1(p.net_margin),
    },
    {
      key: "debt_ratio",
      header: t("stock.health.debt_ratio"),
      numeric: true,
      cellClassName: "text-foreground",
      render: (p) => fmtPct1(p.debt_ratio),
    },
    {
      key: "eps",
      header: "EPS",
      numeric: true,
      cellClassName: "text-foreground",
      render: (p) => (p.eps == null ? "—" : p.eps.toFixed(2)),
    },
  ];

  return (
    <div className="p-4 space-y-4">
      {quality && (
        <div className={`rounded-lg border px-4 py-3 text-sm ${
          quality.status === "good"
            ? "border-success/30 bg-success/5"
            : quality.status === "degraded"
              ? "border-warning/30 bg-warning/5"
              : "border-border bg-muted/20"
        }`}>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="font-medium text-foreground">
              {t(`stock.health.quality_${quality.status}`)}
            </span>
            <span className="text-xs text-muted-foreground">
              {t("stock.health.coverage", { value: quality.latest_core_coverage_pct })}
              {quality.sources.length > 0 && ` · ${quality.sources.join(", ")}`}
            </span>
          </div>
          {quality.flags.length > 0 && (
            <div className="mt-1 text-xs text-muted-foreground">
              {quality.flags.map((flag) => t(`stock.health.quality_flag_${flag}`)).join(" · ")}
            </div>
          )}
        </div>
      )}

      {/* summary strip */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <div className="bg-background border border-border rounded p-3">
          <div className="text-xs text-muted-foreground">ROE (TTM)</div>
          <div className="text-lg font-semibold text-foreground">{fmtPct1(summary.latest_roe)}</div>
        </div>
        <div className="bg-background border border-border rounded p-3">
          <div className="text-xs text-muted-foreground">ROA (TTM)</div>
          <div className="text-lg font-semibold text-foreground">{fmtPct1(summary.latest_roa)}</div>
        </div>
        <div className="bg-background border border-border rounded p-3">
          <div className="text-xs text-muted-foreground">{t("stock.health.ttm_net_margin")}</div>
          <div className="text-lg font-semibold text-foreground">{fmtPct1(summary.ttm_net_margin)}</div>
        </div>
        <div className="bg-background border border-border rounded p-3">
          <div className="text-xs text-muted-foreground">{t("stock.health.cash_conversion")}</div>
          <div className="text-lg font-semibold text-foreground">
            {summary.cash_conversion_ttm == null ? "—" : `${summary.cash_conversion_ttm.toFixed(2)}x`}
          </div>
        </div>
      </div>

      <div className="bg-background border border-border rounded-lg p-4">
        <h4 className="text-sm font-semibold text-foreground mb-3">{t("stock.health.dupont_title")}</h4>
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
          <div><div className="text-xs text-muted-foreground">{t("stock.health.ttm_net_margin")}</div><div className="font-medium">{fmtPct1(summary.ttm_net_margin)}</div></div>
          <div><div className="text-xs text-muted-foreground">{t("stock.health.asset_turnover")}</div><div className="font-medium">{summary.asset_turnover == null ? "—" : `${summary.asset_turnover.toFixed(2)}x`}</div></div>
          <div><div className="text-xs text-muted-foreground">{t("stock.health.equity_multiplier")}</div><div className="font-medium">{summary.equity_multiplier == null ? "—" : `${summary.equity_multiplier.toFixed(2)}x`}</div></div>
          <div><div className="text-xs text-muted-foreground">{t("stock.health.dupont_roe")}</div><div className="font-medium">{fmtPct1(summary.dupont_roe)}</div></div>
        </div>
        <p className="mt-3 text-xs text-muted-foreground">{t("stock.health.ttm_method_note")}</p>
      </div>

      {data.signals?.length > 0 && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {data.signals.map((signal) => (
            <div key={signal.code} className={`rounded border px-4 py-3 text-sm ${
              signal.direction === "positive"
                ? "border-success/30 bg-success/5"
                : "border-danger/30 bg-danger/5"
            }`}>
              <div className="font-medium text-foreground">{t(`stock.health.signal_${signal.code}`)}</div>
              <div className="mt-1 text-xs text-muted-foreground">
                {signal.unit === "percentage_points"
                  ? t("stock.health.signal_value_pp", { value: signal.value.toFixed(2) })
                  : signal.unit === "ratio"
                    ? `${signal.value.toFixed(2)}x`
                    : signal.value.toLocaleString()}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* four StatementDog sections */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <HealthSection title={t("stock.health.profitability")} light={lights.profitability}>
          <div className="space-y-3">
            <MetricSparkRow label={t("stock.health.gross_margin")} periods={periods} accessor={(p) => p.gross_margin} />
            <MetricSparkRow label={t("stock.health.operating_margin")} periods={periods} accessor={(p) => p.operating_margin} />
            <MetricSparkRow label={t("stock.health.net_margin")} periods={periods} accessor={(p) => p.net_margin} />
          </div>
        </HealthSection>

        <HealthSection title={t("stock.health.safety")} light={lights.safety}>
          <div className="space-y-3">
            <MetricSparkRow label={t("stock.health.debt_ratio")} periods={periods} accessor={(p) => p.debt_ratio} />
            <MetricSparkRow
              label={t("stock.health.current_ratio")}
              periods={periods}
              accessor={(p) => p.current_ratio}
              suffix="x"
            />
          </div>
        </HealthSection>

        <HealthSection title={t("stock.health.growth")} light={lights.growth}>
          <MetricSparkRow
            label={t("stock.health.eps")}
            periods={periods}
            accessor={(p) => p.eps}
            suffix=""
          />
          <div className="mt-3 text-xs text-muted-foreground">
            {t("stock.health.revenue_yoy")}:{" "}
            <span className={`font-medium ${
              summary.revenue_yoy == null ? ""
              : summary.revenue_yoy >= 0 ? "text-up" : "text-down"
            }`}>
              {fmtPct1(summary.revenue_yoy)}
            </span>
          </div>
        </HealthSection>

        <HealthSection title={t("stock.health.cash_flow")} light={lights.cash_flow}>
          <MetricSparkRow
            label={t("stock.health.operating_cf")}
            periods={periods}
            accessor={(p) => p.operating_cf}
            suffix=""
          />
          <MetricSparkRow
            label={t("stock.health.free_cf")}
            periods={periods}
            accessor={(p) => p.free_cf}
            suffix=""
          />
          <div className="mt-2 text-xs text-muted-foreground">
            {t("stock.health.cf_streak", { count: summary.cf_positive_streak_4q })}
          </div>
        </HealthSection>
      </div>

      {/* per-period table */}
      <DataTable
        columns={columns}
        rows={periodRows}
        rowKey={(p) => p.date}
        mobileMode="scroll"
        aria-label={t("stock.health.period")}
      />
    </div>
  );
}
