import type { Holding } from "@/types/portfolio";

interface Props {
  holdings: Holding[];
  currency: string;
}

export default function HoldingsTable({ holdings, currency }: Props) {
  if (!holdings.length) {
    return <p className="text-sm text-muted-foreground py-4">No holdings yet.</p>;
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-border text-muted-foreground text-left">
            <th className="pb-2 pr-4">Symbol</th>
            <th className="pb-2 pr-4">Market</th>
            <th className="pb-2 pr-4 text-right">Qty</th>
            <th className="pb-2 pr-4 text-right">Avg Cost</th>
            <th className="pb-2 pr-4 text-right">Price</th>
            <th className="pb-2 pr-4 text-right">Value ({currency})</th>
            <th className="pb-2 pr-4 text-right">P&L</th>
            <th className="pb-2 text-right">P&L %</th>
            <th className="pb-2 text-right">Weight</th>
          </tr>
        </thead>
        <tbody>
          {holdings.map((h) => (
            <tr key={h.id} className="border-b border-border/50 hover:bg-secondary/30 transition-colors">
              <td className="py-2 pr-4 font-medium text-foreground">{h.symbol}</td>
              <td className="py-2 pr-4 text-muted-foreground">{h.market}</td>
              <td className="py-2 pr-4 text-right">{h.quantity.toLocaleString()}</td>
              <td className="py-2 pr-4 text-right">{h.avg_cost.toFixed(2)}</td>
              <td className="py-2 pr-4 text-right">{h.current_price.toFixed(2)}</td>
              <td className="py-2 pr-4 text-right">{h.current_value.toLocaleString(undefined, { maximumFractionDigits: 0 })}</td>
              <td className={`py-2 pr-4 text-right font-medium ${h.unrealized_pnl >= 0 ? "text-positive" : "text-negative"}`}>
                {h.unrealized_pnl >= 0 ? "+" : ""}
                {h.unrealized_pnl.toLocaleString(undefined, { maximumFractionDigits: 0 })}
              </td>
              <td className={`py-2 pr-4 text-right ${h.unrealized_pnl_pct >= 0 ? "text-positive" : "text-negative"}`}>
                {h.unrealized_pnl_pct >= 0 ? "+" : ""}
                {h.unrealized_pnl_pct.toFixed(2)}%
              </td>
              <td className="py-2 text-right text-muted-foreground">{h.weight_pct?.toFixed(1)}%</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
