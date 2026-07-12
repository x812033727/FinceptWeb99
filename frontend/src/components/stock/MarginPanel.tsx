import { useQuery } from "@tanstack/react-query";
import { Bar, BarChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
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

// Column colors mirror the bars above (chart-1 = 融資, chart-2 = 融券)
// so the table doubles as the chart's legend.
const COLUMNS: DataTableColumn<MarginRow>[] = [
  {
    key: "date",
    header: "日期",
    cellClassName: "text-muted-foreground",
    mobile: "primary",
  },
  {
    key: "margin_purchase",
    header: <span className="text-chart-1">融資買入</span>,
    numeric: true,
    render: (r) => <span className="text-chart-1">{fmtK(r.margin_purchase)}</span>,
  },
  {
    key: "margin_balance",
    header: <span className="text-chart-1">融資餘額</span>,
    numeric: true,
    render: (r) => <span className="font-medium text-chart-1">{fmtK(r.margin_balance)}</span>,
    mobile: "primary",
  },
  {
    key: "short_sale",
    header: <span className="text-chart-2">融券賣出</span>,
    numeric: true,
    render: (r) => <span className="text-chart-2">{fmtK(r.short_sale)}</span>,
  },
  {
    key: "short_balance",
    header: <span className="text-chart-2">融券餘額</span>,
    numeric: true,
    render: (r) => <span className="font-medium text-chart-2">{fmtK(r.short_balance)}</span>,
  },
  {
    key: "ratio",
    header: "券資比",
    numeric: true,
    cellClassName: "text-muted-foreground",
    render: (r) => {
      const ratio = r.margin_balance > 0 ? (r.short_balance / r.margin_balance) * 100 : null;
      return ratio != null ? `${ratio.toFixed(1)}%` : "—";
    },
  },
];

export function MarginPanel({ symbol }: { symbol: string }) {
  const { data = [], isLoading } = useQuery({
    queryKey: ["margin", symbol],
    queryFn: () => fetchMargin(symbol),
    staleTime: 300_000,
  });

  if (isLoading) return <Loading />;
  if (!data.length) {
    return <EmptyState icon={Inbox} title="無融資融券資料" />;
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
        <h4 className="text-xs font-semibold text-foreground mb-2">融資融券餘額（千股，近 30 日）</h4>
        <ResponsiveContainer width="100%" height={130}>
          <BarChart data={chartData} margin={{ left: 0, right: 0 }}>
            <XAxis dataKey="date" tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }} interval="preserveStartEnd" />
            <YAxis tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }} width={45} />
            <Tooltip contentStyle={{ background: "hsl(var(--card))", border: "1px solid hsl(var(--border))", fontSize: 11 }} />
            <Bar dataKey="margin_balance" name="融資餘額" fill="hsl(var(--chart-1))" radius={[2, 2, 0, 0]} />
            <Bar dataKey="short_balance" name="融券餘額" fill="hsl(var(--chart-2))" radius={[2, 2, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>
      </div>

      <DataTable
        aria-label="融資融券明細"
        columns={COLUMNS}
        rows={tableRows}
        rowKey={(r) => r.date}
      />
    </div>
  );
}
