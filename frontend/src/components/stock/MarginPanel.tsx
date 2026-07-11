import { useQuery } from "@tanstack/react-query";
import { Bar, BarChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Loading } from "./_atoms";
import { fetchMargin, fmtK } from "./_shared";

export function MarginPanel({ symbol }: { symbol: string }) {
  const { data = [], isLoading } = useQuery({
    queryKey: ["margin", symbol],
    queryFn: () => fetchMargin(symbol),
    staleTime: 300_000,
  });

  if (isLoading) return <Loading />;
  if (!data.length) return <div className="p-6 text-muted-foreground text-sm">No margin data.</div>;

  const chartData = [...data].reverse().slice(-30).map((r) => ({
    date: r.date.slice(5),
    margin_balance: Math.round(r.margin_balance / 1000),
    short_balance: Math.round(r.short_balance / 1000),
  }));

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

      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-border text-muted-foreground">
              <th className="text-left py-2 pr-4 font-medium">日期</th>
              <th className="text-right py-2 px-2 font-medium text-blue-400">融資買入</th>
              <th className="text-right py-2 px-2 font-medium text-blue-400">融資餘額</th>
              <th className="text-right py-2 px-2 font-medium text-yellow-400">融券賣出</th>
              <th className="text-right py-2 px-2 font-medium text-yellow-400">融券餘額</th>
              <th className="text-right py-2 px-2 font-medium">券資比</th>
            </tr>
          </thead>
          <tbody>
            {[...data].reverse().slice(0, 30).map((r) => {
              const ratio = r.margin_balance > 0 ? (r.short_balance / r.margin_balance) * 100 : null;
              return (
                <tr key={r.date} className="border-b border-border/30 hover:bg-accent/5">
                  <td className="py-1.5 pr-4 text-muted-foreground">{r.date}</td>
                  <td className="text-right py-1.5 px-2 text-blue-400">{fmtK(r.margin_purchase)}</td>
                  <td className="text-right py-1.5 px-2 text-blue-400 font-medium">{fmtK(r.margin_balance)}</td>
                  <td className="text-right py-1.5 px-2 text-yellow-400">{fmtK(r.short_sale)}</td>
                  <td className="text-right py-1.5 px-2 text-yellow-400 font-medium">{fmtK(r.short_balance)}</td>
                  <td className="text-right py-1.5 px-2 text-muted-foreground">
                    {ratio != null ? `${ratio.toFixed(1)}%` : "—"}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
