import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import { Bar, BarChart, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Loading } from "./_atoms";
import { fetchHealth, fmtPct1 } from "./_shared";
import type { HealthPeriod, Light } from "./_shared";

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
          <Tooltip
            contentStyle={{ background: "hsl(var(--card))", border: "1px solid hsl(var(--border))", fontSize: 11 }}
            formatter={(v: number) => [`${v == null ? "—" : v.toFixed(2)}${suffix}`, label]}
          />
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
  });

  if (isLoading) return <Loading />;
  if (!data || !data.periods.length) {
    return <div className="p-6 text-muted-foreground text-sm">{t("stock.health.no_data")}</div>;
  }

  const { periods, summary, lights } = data;

  return (
    <div className="p-4 space-y-4">
      {/* summary strip */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <div className="bg-background border border-border rounded p-3">
          <div className="text-xs text-muted-foreground">ROE (TTM)</div>
          <div className="text-lg font-semibold text-foreground">{fmtPct1(summary.latest_roe)}</div>
        </div>
        <div className="bg-background border border-border rounded p-3">
          <div className="text-xs text-muted-foreground">{t("stock.health.debt_ratio")}</div>
          <div className="text-lg font-semibold text-foreground">{fmtPct1(summary.latest_debt_ratio)}</div>
        </div>
        <div className="bg-background border border-border rounded p-3">
          <div className="text-xs text-muted-foreground">{t("stock.health.gross_margin")}</div>
          <div className="text-lg font-semibold text-foreground">{fmtPct1(summary.latest_gross_margin)}</div>
        </div>
        <div className="bg-background border border-border rounded p-3">
          <div className="text-xs text-muted-foreground">{t("stock.health.revenue_yoy")}</div>
          <div className={`text-lg font-semibold ${
            summary.revenue_yoy == null ? "text-muted-foreground"
            : summary.revenue_yoy >= 0 ? "text-up" : "text-down"
          }`}>
            {fmtPct1(summary.revenue_yoy)}
          </div>
        </div>
      </div>

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
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-border text-muted-foreground">
              <th className="text-left py-2 pr-4 font-medium">{t("stock.health.period")}</th>
              <th className="text-right py-2 px-2 font-medium">{t("stock.health.gross_margin")}</th>
              <th className="text-right py-2 px-2 font-medium">{t("stock.health.operating_margin")}</th>
              <th className="text-right py-2 px-2 font-medium">{t("stock.health.net_margin")}</th>
              <th className="text-right py-2 px-2 font-medium">{t("stock.health.debt_ratio")}</th>
              <th className="text-right py-2 px-2 font-medium">EPS</th>
            </tr>
          </thead>
          <tbody>
            {[...periods].reverse().map((p) => (
              <tr key={p.date} className="border-b border-border/30 hover:bg-accent/5">
                <td className="py-1.5 pr-4 text-muted-foreground">{p.date}</td>
                <td className="text-right py-1.5 px-2 text-foreground">{fmtPct1(p.gross_margin)}</td>
                <td className="text-right py-1.5 px-2 text-foreground">{fmtPct1(p.operating_margin)}</td>
                <td className="text-right py-1.5 px-2 text-foreground">{fmtPct1(p.net_margin)}</td>
                <td className="text-right py-1.5 px-2 text-foreground">{fmtPct1(p.debt_ratio)}</td>
                <td className="text-right py-1.5 px-2 text-foreground">{p.eps == null ? "—" : p.eps.toFixed(2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
