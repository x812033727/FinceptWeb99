import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Bar, BarChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Loading } from "./_atoms";
import { fetchRevenue, fmtPct } from "./_shared";
import { DataTable, type DataTableColumn } from "../ui/table";

export function RevenuePanel({ symbol }: { symbol: string }) {
  const { t } = useTranslation();
  const { data = [], isLoading } = useQuery({
    queryKey: ["revenue", symbol],
    queryFn: () => fetchRevenue(symbol),
    staleTime: 3_600_000,
    gcTime: 24 * 3_600_000, // fundamentals tier — monthly revenue, cache for a day
  });

  if (isLoading) return <Loading />;
  if (!data.length) return <div className="p-6 text-muted-foreground text-sm">No revenue data.</div>;

  const chartData = [...data].reverse().slice(-24).map((r) => ({
    date: r.date.slice(0, 7),
    revenue: Math.round(r.revenue / 1000),   // millions NTD
    yoy: r.revenue_yoy,
  }));

  type RevRow = (typeof data)[number];
  const rows = [...data].reverse().slice(0, 24);
  const columns: DataTableColumn<RevRow>[] = [
    {
      key: "date",
      header: t("stock.revenue.col_month"),
      mobile: "primary",
      render: (r) => <span className="text-muted-foreground">{r.date.slice(0, 7)}</span>,
    },
    {
      key: "revenue",
      header: t("stock.revenue.col_revenue"),
      numeric: true,
      render: (r) => (
        <span className="text-foreground font-medium">{r.revenue.toLocaleString()}</span>
      ),
    },
    {
      key: "mom",
      header: t("stock.revenue.col_mom"),
      numeric: true,
      render: (r) => (
        <span
          className={`font-medium ${
            r.revenue_mom == null ? "text-muted-foreground"
            : r.revenue_mom >= 0 ? "text-up" : "text-down"
          }`}
        >
          {r.revenue_mom == null ? "—" : fmtPct(r.revenue_mom, true)}
        </span>
      ),
    },
    {
      key: "yoy",
      header: t("stock.revenue.col_yoy"),
      numeric: true,
      render: (r) => (
        <span
          className={`font-medium ${
            r.revenue_yoy == null ? "text-muted-foreground"
            : r.revenue_yoy >= 0 ? "text-up" : "text-down"
          }`}
        >
          {r.revenue_yoy == null ? "—" : fmtPct(r.revenue_yoy, true)}
        </span>
      ),
    },
  ];

  return (
    <div className="p-4 space-y-4">
      <div>
        <h4 className="text-xs font-semibold text-foreground mb-2">{t("stock.revenue.chart_title")}</h4>
        <ResponsiveContainer width="100%" height={130}>
          <BarChart data={chartData} margin={{ left: 0, right: 0 }}>
            <XAxis dataKey="date" tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }} interval="preserveStartEnd" />
            <YAxis tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }} width={50} />
            <Tooltip
              contentStyle={{ background: "hsl(var(--card))", border: "1px solid hsl(var(--border))", fontSize: 11 }}
              formatter={(v: number) => [`${v}M NTD`, "Revenue"]}
            />
            <Bar dataKey="revenue" name={t("stock.revenue.bar_name")} fill="hsl(var(--chart-1))" radius={[2, 2, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>
      </div>

      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.date}
        mobileMode="cards"
        aria-label={t("stock.revenue.aria")}
      />
    </div>
  );
}
