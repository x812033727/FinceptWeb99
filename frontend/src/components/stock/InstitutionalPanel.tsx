import { useQuery } from "@tanstack/react-query";
import {
  Bar, BarChart, Cell, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { Loading } from "./_atoms";
import { fetchInstitutional, fmtK } from "./_shared";

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
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-border text-muted-foreground">
                <th className="text-left py-2 pr-4 font-medium">日期</th>
                <th className="text-right py-2 px-2 font-medium">外資買</th>
                <th className="text-right py-2 px-2 font-medium">外資賣</th>
                <th className="text-right py-2 px-2 font-medium text-up">外資淨</th>
                <th className="text-right py-2 px-2 font-medium">投信買</th>
                <th className="text-right py-2 px-2 font-medium">投信賣</th>
                <th className="text-right py-2 px-2 font-medium text-blue-400">投信淨</th>
                <th className="text-right py-2 px-2 font-medium text-purple-400">自營淨</th>
              </tr>
            </thead>
            <tbody>
              {[...data].reverse().slice(0, 30).map((r) => {
                const finiNet = r.fini_buy - r.fini_sell;
                const sitcNet = r.sitc_buy - r.sitc_sell;
                const dealerNet = r.dealer_buy - r.dealer_sell;
                return (
                  <tr key={r.date} className="border-b border-border/30 hover:bg-accent/5">
                    <td className="py-1.5 pr-4 text-muted-foreground">{r.date}</td>
                    <td className="text-right py-1.5 px-2">{fmtK(r.fini_buy)}</td>
                    <td className="text-right py-1.5 px-2">{fmtK(r.fini_sell)}</td>
                    <td className={`text-right py-1.5 px-2 font-medium ${finiNet >= 0 ? "text-up" : "text-down"}`}>
                      {finiNet >= 0 ? "+" : ""}{fmtK(Math.abs(finiNet))}
                    </td>
                    <td className="text-right py-1.5 px-2">{fmtK(r.sitc_buy)}</td>
                    <td className="text-right py-1.5 px-2">{fmtK(r.sitc_sell)}</td>
                    <td className={`text-right py-1.5 px-2 font-medium ${sitcNet >= 0 ? "text-up" : "text-down"}`}>
                      {sitcNet >= 0 ? "+" : ""}{fmtK(Math.abs(sitcNet))}
                    </td>
                    <td className={`text-right py-1.5 px-2 font-medium ${dealerNet >= 0 ? "text-up" : "text-down"}`}>
                      {dealerNet >= 0 ? "+" : ""}{fmtK(Math.abs(dealerNet))}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
