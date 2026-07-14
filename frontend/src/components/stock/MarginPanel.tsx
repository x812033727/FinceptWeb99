import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Bar, BarChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { ChartTooltip } from "@/components/ui/ChartTooltip";
import { Inbox } from "lucide-react";
import { DataTable, type DataTableColumn } from "@/components/ui/table";
import { EmptyState } from "@/components/ui/EmptyState";
import { Loading } from "./_atoms";
import { fetchMargin, fmtK } from "./_shared";

interface MarginRow {
  date: string;
  margin_purchase: number;
  margin_balance: number;
  short_sale: number;
  short_balance: number;
}

export function MarginPanel({ symbol }: { symbol: string }) {
  const { t } = useTranslation();
  const { data = [], isLoading } = useQuery({
    queryKey: ["margin", symbol],
    queryFn: () => fetchMargin(symbol),
    staleTime: 300_000,
  });

  // Column colors mirror the bars above (chart-1 = margin, chart-2 = short)
  // so the table doubles as the chart's legend.
  const columns: DataTableColumn<MarginRow>[] = [
    {
      key: "date",
      header: t("stock.margin.col_date"),
      cellClassName: "text-muted-foreground",
      mobile: "primary",
    },
    {
      key: "margin_purchase",
      header: <span className="text-chart-1">{t("stock.margin.col_margin_purchase")}</span>,
      numeric: true,
      render: (r) => <span className="text-chart-1">{fmtK(r.margin_purchase)}</span>,
    },
    {
      key: "margin_balance",
      header: <span className="text-chart-1">{t("stock.margin.col_margin_balance")}</span>,
      numeric: true,
      render: (r) => <span className="font-medium text-chart-1">{fmtK(r.margin_balance)}</span>,
      mobile: "primary",
    },
    {
      key: "short_sale",
      header: <span className="text-chart-2">{t("stock.margin.col_short_sale")}</span>,
      numeric: true,
      render: (r) => <span className="text-chart-2">{fmtK(r.short_sale)}</span>,
    },
    {
      key: "short_balance",
      header: <span className="text-chart-2">{t("stock.margin.col_short_balance")}</span>,
      numeric: true,
      render: (r) => <span className="font-medium text-chart-2">{fmtK(r.short_balance)}</span>,
    },
    {
      key: "ratio",
      header: t("stock.margin.col_ratio"),
      numeric: true,
      cellClassName: "text-muted-foreground",
      render: (r) => {
        const ratio = r.margin_balance > 0 ? (r.short_balance / r.margin_balance) * 100 : null;
        return ratio != null ? `${ratio.toFixed(1)}%` : "—";
      },
    },
  ];

  if (isLoading) return <Loading />;
  if (!data.length) {
    return <EmptyState icon={Inbox} title={t("stock.margin.empty")} />;
  }

  const chartData = [...data].reverse().slice(-30).map((r) => ({
    date: r.date.slice(5),
    margin_balance: Math.round(r.margin_balance / 1000),
    short_balance: Math.round(r.short_balance / 1000),
  }));

  const tableRows = [...data].reverse().slice(0, 30) as MarginRow[];

  return (
    <div className="p-4 space-y-4">
      <div>
        <h4 className="text-xs font-semibold text-foreground mb-2">{t("stock.margin.chart_title")}</h4>
        <ResponsiveContainer width="100%" height={130}>
          <BarChart data={chartData} margin={{ left: 0, right: 0 }}>
            <XAxis dataKey="date" tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }} interval="preserveStartEnd" />
            <YAxis tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }} width={45} />
            <Tooltip content={<ChartTooltip />} />
            <Bar dataKey="margin_balance" name={t("stock.margin.bar_margin")} fill="hsl(var(--chart-1))" radius={[2, 2, 0, 0]} />
            <Bar dataKey="short_balance" name={t("stock.margin.bar_short")} fill="hsl(var(--chart-2))" radius={[2, 2, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>
      </div>

      <DataTable
        aria-label={t("stock.margin.aria_table")}
        columns={columns}
        rows={tableRows}
        rowKey={(r) => r.date}
      />
    </div>
  );
}
