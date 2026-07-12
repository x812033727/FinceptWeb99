import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import { Cell, Legend, Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts";
import { Loading } from "./_atoms";
import { fetchETFHoldings } from "./_shared";
import { DataTable, type DataTableColumn } from "../ui/table";

const PIE_COLORS = [
  "hsl(var(--chart-1))", "hsl(var(--chart-2))", "hsl(var(--chart-3))",
  "hsl(var(--chart-4))", "hsl(var(--chart-5))", "hsl(var(--chart-6))",
];

export function HoldingsPanel({ symbol }: { symbol: string }) {
  const { t } = useTranslation();
  const { data, isLoading } = useQuery({
    queryKey: ["etf-holdings", symbol],
    queryFn: () => fetchETFHoldings(symbol),
    staleTime: 6 * 3_600_000,
    gcTime: 24 * 3_600_000, // fundamentals tier — ETF constituents, cache for a day
  });

  if (isLoading) return <Loading />;
  if (!data || !data.holdings.length) {
    return (
      <div className="p-6 text-muted-foreground text-sm">
        {t("stock.etf.no_holdings")}
      </div>
    );
  }

  const top10 = data.holdings.slice(0, 10);
  const otherWeight = data.holdings.slice(10).reduce((s, h) => s + h.weight, 0);
  const pieData = otherWeight > 0
    ? [...top10.map((h) => ({ name: h.symbol, value: h.weight })),
       { name: t("stock.etf.others"), value: Number(otherWeight.toFixed(2)) }]
    : top10.map((h) => ({ name: h.symbol, value: h.weight }));

  const top10Weight = top10.reduce((s, h) => s + h.weight, 0);

  const rows = data.holdings.slice(0, 30).map((h, i) => ({ ...h, rank: i + 1 }));
  type HoldingRow = (typeof rows)[number];
  const columns: DataTableColumn<HoldingRow>[] = [
    {
      key: "rank",
      header: "#",
      mobile: "secondary",
      render: (h) => <span className="text-muted-foreground">{h.rank}</span>,
    },
    {
      key: "symbol",
      header: t("stock.etf.constituent"),
      mobile: "primary",
      render: (h) => <span className="text-primary font-medium">{h.symbol}</span>,
    },
    {
      key: "name",
      header: t("market.table.name"),
      mobile: "secondary",
      render: (h) => <span className="text-foreground">{h.name_zh || "—"}</span>,
    },
    {
      key: "weight",
      header: t("stock.etf.weight"),
      numeric: true,
      render: (h) => (
        <span className="text-foreground font-medium">{h.weight.toFixed(2)}%</span>
      ),
    },
  ];

  return (
    <div className="p-4 space-y-4">
      <div className="flex items-center justify-between text-xs text-muted-foreground">
        <span>{t("stock.etf.as_of", { date: data.as_of ?? "—" })}</span>
        <span>{t("stock.etf.top10_concentration")}: <span className="text-foreground font-medium">{top10Weight.toFixed(2)}%</span></span>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-5 gap-4">
        <div className="lg:col-span-2">
          <ResponsiveContainer width="100%" height={280}>
            <PieChart>
              <Pie
                data={pieData}
                dataKey="value"
                nameKey="name"
                cx="50%"
                cy="50%"
                outerRadius={100}
                innerRadius={45}
                paddingAngle={1}
              >
                {pieData.map((_, i) => (
                  <Cell key={i} fill={PIE_COLORS[i % PIE_COLORS.length]} />
                ))}
              </Pie>
              <Tooltip
                contentStyle={{ background: "hsl(var(--card))", border: "1px solid hsl(var(--border))", fontSize: 11 }}
                formatter={(v: number) => [`${v.toFixed(2)}%`, "weight"]}
              />
              <Legend wrapperStyle={{ fontSize: 11 }} iconSize={8} />
            </PieChart>
          </ResponsiveContainer>
        </div>

        <div className="lg:col-span-3">
          <DataTable
            columns={columns}
            rows={rows}
            rowKey={(h) => h.symbol}
            mobileMode="cards"
            aria-label={t("stock.etf.weight")}
          />
        </div>
      </div>
    </div>
  );
}
