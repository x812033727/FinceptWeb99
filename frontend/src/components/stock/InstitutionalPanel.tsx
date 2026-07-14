import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import {
  Bar, BarChart, Cell, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { Loading } from "./_atoms";
import { fetchInstitutional, fmtK } from "./_shared";
import { DataTable, type DataTableColumn } from "../ui/table";
import { ChartTooltip, chartAxisTick } from "../ui/ChartTooltip";

export function InstitutionalPanel({ symbol }: { symbol: string }) {
  const { t } = useTranslation();
  const { data = [], isLoading } = useQuery({
    queryKey: ["institutional", symbol],
    queryFn: () => fetchInstitutional(symbol),
    staleTime: 300_000,
  });

  if (isLoading) return <Loading />;
  if (!data.length) return <div className="p-6 text-muted-foreground text-sm">No institutional data.</div>;

  const chartData = [...data].reverse().slice(-30).map((r) => ({
    date: r.date.slice(5),
    fini: Math.round((r.fini_buy - r.fini_sell) / 1000),
    sitc: Math.round((r.sitc_buy - r.sitc_sell) / 1000),
    dealer: Math.round((r.dealer_buy - r.dealer_sell) / 1000),
  }));

  type InstRow = (typeof data)[number];
  const rows = [...data].reverse().slice(0, 30);
  const columns: DataTableColumn<InstRow>[] = [
    {
      key: "date",
      header: t("stock.institutional.col_date"),
      render: (r) => <span className="text-muted-foreground">{r.date}</span>,
    },
    {
      key: "fini_buy",
      header: t("stock.institutional.col_fini_buy"),
      numeric: true,
      render: (r) => fmtK(r.fini_buy),
    },
    {
      key: "fini_sell",
      header: t("stock.institutional.col_fini_sell"),
      numeric: true,
      render: (r) => fmtK(r.fini_sell),
    },
    {
      key: "fini_net",
      header: t("stock.institutional.col_fini_net"),
      numeric: true,
      headerClassName: "text-up",
      render: (r) => {
        const net = r.fini_buy - r.fini_sell;
        return (
          <span className={`font-medium ${net >= 0 ? "text-up" : "text-down"}`}>
            {net >= 0 ? "+" : ""}{fmtK(Math.abs(net))}
          </span>
        );
      },
    },
    {
      key: "sitc_buy",
      header: t("stock.institutional.col_sitc_buy"),
      numeric: true,
      render: (r) => fmtK(r.sitc_buy),
    },
    {
      key: "sitc_sell",
      header: t("stock.institutional.col_sitc_sell"),
      numeric: true,
      render: (r) => fmtK(r.sitc_sell),
    },
    {
      key: "sitc_net",
      header: t("stock.institutional.col_sitc_net"),
      numeric: true,
      headerClassName: "text-blue-400",
      render: (r) => {
        const net = r.sitc_buy - r.sitc_sell;
        return (
          <span className={`font-medium ${net >= 0 ? "text-up" : "text-down"}`}>
            {net >= 0 ? "+" : ""}{fmtK(Math.abs(net))}
          </span>
        );
      },
    },
    {
      key: "dealer_net",
      header: t("stock.institutional.col_dealer_net"),
      numeric: true,
      headerClassName: "text-purple-400",
      render: (r) => {
        const net = r.dealer_buy - r.dealer_sell;
        return (
          <span className={`font-medium ${net >= 0 ? "text-up" : "text-down"}`}>
            {net >= 0 ? "+" : ""}{fmtK(Math.abs(net))}
          </span>
        );
      },
    },
  ];

  return (
    <div className="p-4 space-y-4">
      <div className="space-y-4">
        {/* net buy bar chart */}
        <div>
          <h4 className="text-xs font-semibold text-foreground mb-2">{t("stock.institutional.chart_title")}</h4>
          <ResponsiveContainer width="100%" height={120}>
            <BarChart data={chartData} margin={{ left: 0, right: 0 }}>
              <XAxis dataKey="date" tick={chartAxisTick} interval="preserveStartEnd" />
              <YAxis tick={chartAxisTick} width={40} />
              <Tooltip content={<ChartTooltip />} />
              <ReferenceLine y={0} stroke="hsl(var(--border))" />
              <Bar dataKey="fini" name={t("stock.institutional.bar_fini")} radius={[2, 2, 0, 0]}>
                {chartData.map((d, i) => (
                  <Cell key={i} fill={d.fini >= 0 ? "hsl(var(--up))" : "hsl(var(--down))"} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>

        {/* table */}
        <DataTable
          columns={columns}
          rows={rows}
          rowKey={(r) => r.date}
          mobileMode="scroll"
          aria-label={t("stock.institutional.aria")}
        />
      </div>
    </div>
  );
}
