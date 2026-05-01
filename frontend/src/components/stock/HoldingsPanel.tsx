import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import { Cell, Legend, Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts";
import { Loading } from "./_atoms";
import { fetchETFHoldings } from "./_shared";

const PIE_COLORS = [
  "#3b82f6", "#8b5cf6", "#ec4899", "#f59e0b", "#10b981",
  "#06b6d4", "#6366f1", "#f43f5e", "#84cc16", "#eab308",
];

export function HoldingsPanel({ symbol }: { symbol: string }) {
  const { t } = useTranslation();
  const { data, isLoading } = useQuery({
    queryKey: ["etf-holdings", symbol],
    queryFn: () => fetchETFHoldings(symbol),
    staleTime: 6 * 3_600_000,
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

        <div className="lg:col-span-3 overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-border text-muted-foreground">
                <th className="text-left py-2 pr-4 font-medium">#</th>
                <th className="text-left py-2 pr-4 font-medium">{t("stock.etf.constituent")}</th>
                <th className="text-left py-2 pr-4 font-medium">{t("market.table.name")}</th>
                <th className="text-right py-2 px-2 font-medium">{t("stock.etf.weight")}</th>
              </tr>
            </thead>
            <tbody>
              {data.holdings.slice(0, 30).map((h, i) => (
                <tr key={h.symbol} className="border-b border-border/30 hover:bg-accent/5">
                  <td className="py-1.5 pr-4 text-muted-foreground">{i + 1}</td>
                  <td className="py-1.5 pr-4 text-primary font-medium">{h.symbol}</td>
                  <td className="py-1.5 pr-4 text-foreground">{h.name_zh || "—"}</td>
                  <td className="text-right py-1.5 px-2 text-foreground font-medium">
                    {h.weight.toFixed(2)}%
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
