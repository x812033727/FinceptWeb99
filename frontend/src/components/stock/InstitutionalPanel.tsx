import { useQuery } from "@tanstack/react-query";
import {
  Bar, BarChart, Cell, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { Loading } from "./_atoms";
import { fetchInstitutional, fmtK } from "./_shared";
import { DataTable, type DataTableColumn } from "../ui/table";

export function InstitutionalPanel({ symbol }: { symbol: string }) {
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
      header: "日期",
      render: (r) => <span className="text-muted-foreground">{r.date}</span>,
    },
    {
      key: "fini_buy",
      header: "外資買",
      numeric: true,
      render: (r) => fmtK(r.fini_buy),
    },
    {
      key: "fini_sell",
      header: "外資賣",
      numeric: true,
      render: (r) => fmtK(r.fini_sell),
    },
    {
      key: "fini_net",
      header: "外資淨",
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
      header: "投信買",
      numeric: true,
      render: (r) => fmtK(r.sitc_buy),
    },
    {
      key: "sitc_sell",
      header: "投信賣",
      numeric: true,
      render: (r) => fmtK(r.sitc_sell),
    },
    {
      key: "sitc_net",
      header: "投信淨",
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
      header: "自營淨",
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
          <h4 className="text-xs font-semibold text-foreground mb-2">外資淨買超（千股，近 30 日）</h4>
          <ResponsiveContainer width="100%" height={120}>
            <BarChart data={chartData} margin={{ left: 0, right: 0 }}>
              <XAxis dataKey="date" tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }} interval="preserveStartEnd" />
              <YAxis tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }} width={40} />
              <Tooltip contentStyle={{ background: "hsl(var(--card))", border: "1px solid hsl(var(--border))", fontSize: 11 }} />
              <ReferenceLine y={0} stroke="hsl(var(--border))" />
              <Bar dataKey="fini" name="外資" radius={[2, 2, 0, 0]}>
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
          aria-label="法人買賣超"
        />
      </div>
    </div>
  );
}
