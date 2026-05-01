import { useQuery } from "@tanstack/react-query";
import { Bar, BarChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Loading } from "./_atoms";
import { fetchRevenue, fmtPct } from "./_shared";

export function RevenuePanel({ symbol }: { symbol: string }) {
  const { data = [], isLoading } = useQuery({
    queryKey: ["revenue", symbol],
    queryFn: () => fetchRevenue(symbol),
    staleTime: 3_600_000,
  });

  if (isLoading) return <Loading />;
  if (!data.length) return <div className="p-6 text-muted-foreground text-sm">No revenue data.</div>;

  const chartData = [...data].reverse().slice(-24).map((r) => ({
    date: r.date.slice(0, 7),
    revenue: Math.round(r.revenue / 1000),   // millions NTD
    yoy: r.revenue_yoy,
  }));

  return (
    <div className="p-4 space-y-4">
      <div>
        <h4 className="text-xs font-semibold text-foreground mb-2">月營收（百萬新台幣）</h4>
        <ResponsiveContainer width="100%" height={130}>
          <BarChart data={chartData} margin={{ left: 0, right: 0 }}>
            <XAxis dataKey="date" tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }} interval="preserveStartEnd" />
            <YAxis tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }} width={50} />
            <Tooltip
              contentStyle={{ background: "hsl(var(--card))", border: "1px solid hsl(var(--border))", fontSize: 11 }}
              formatter={(v: number) => [`${v}M NTD`, "Revenue"]}
            />
            <Bar dataKey="revenue" name="月營收" fill="#6366f1" radius={[2, 2, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-border text-muted-foreground">
              <th className="text-left py-2 pr-4 font-medium">月份</th>
              <th className="text-right py-2 px-2 font-medium">營收（千元）</th>
              <th className="text-right py-2 px-2 font-medium">月增率</th>
              <th className="text-right py-2 px-2 font-medium">年增率</th>
            </tr>
          </thead>
          <tbody>
            {[...data].reverse().slice(0, 24).map((r) => (
              <tr key={r.date} className="border-b border-border/30 hover:bg-accent/5">
                <td className="py-1.5 pr-4 text-muted-foreground">{r.date.slice(0, 7)}</td>
                <td className="text-right py-1.5 px-2 text-foreground font-medium">
                  {r.revenue.toLocaleString()}
                </td>
                <td className={`text-right py-1.5 px-2 font-medium ${
                  r.revenue_mom == null ? "text-muted-foreground"
                  : r.revenue_mom >= 0 ? "text-green-400" : "text-red-400"
                }`}>
                  {r.revenue_mom == null ? "—" : fmtPct(r.revenue_mom, true)}
                </td>
                <td className={`text-right py-1.5 px-2 font-medium ${
                  r.revenue_yoy == null ? "text-muted-foreground"
                  : r.revenue_yoy >= 0 ? "text-green-400" : "text-red-400"
                }`}>
                  {r.revenue_yoy == null ? "—" : fmtPct(r.revenue_yoy, true)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
