import { PieChart, Pie, Cell, Tooltip, Legend, ResponsiveContainer } from "recharts";
import type { Holding } from "@/types/portfolio";

const COLORS = ["#38bdf8","#818cf8","#34d399","#fb923c","#f472b6","#a78bfa","#22d3ee","#4ade80"];

interface Props {
  holdings: Holding[];
}

export default function AllocationPie({ holdings }: Props) {
  const data = holdings
    .filter((h) => (h.weight_pct ?? 0) > 0)
    .map((h) => ({ name: `${h.symbol} (${h.market})`, value: h.weight_pct ?? 0 }));

  if (!data.length) return null;

  return (
    <ResponsiveContainer width="100%" height={260}>
      <PieChart>
        <Pie
          data={data}
          cx="50%"
          cy="50%"
          innerRadius={60}
          outerRadius={100}
          paddingAngle={2}
          dataKey="value"
        >
          {data.map((_, i) => (
            <Cell key={i} fill={COLORS[i % COLORS.length]} />
          ))}
        </Pie>
        <Tooltip
          formatter={(v: number) => [`${v.toFixed(1)}%`, "Weight"]}
          contentStyle={{ background: "hsl(var(--card))", border: "1px solid hsl(var(--border))", borderRadius: 6 }}
          labelStyle={{ color: "hsl(var(--foreground))" }}
        />
        <Legend
          wrapperStyle={{ fontSize: 11, color: "hsl(var(--muted-foreground))" }}
          iconType="circle"
          iconSize={8}
        />
      </PieChart>
    </ResponsiveContainer>
  );
}
