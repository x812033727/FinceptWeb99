import { useTranslation } from "react-i18next";
import type { Holding } from "@/types/portfolio";

interface Props {
  holdings: Holding[];
  currency: string;
}

export default function HoldingsTable({ holdings, currency }: Props) {
  const { t } = useTranslation();

  if (!holdings.length) {
    return <p className="text-sm text-muted-foreground py-4">{t("portfolio.holdings.no_holdings")}</p>;
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-border text-muted-foreground text-left">
            <th className="pb-2 pr-4">{t("portfolio.holdings.symbol")}</th>
            <th className="pb-2 pr-4">{t("portfolio.holdings.market")}</th>
            <th className="pb-2 pr-4 text-right">{t("portfolio.holdings.qty")}</th>
            <th className="pb-2 pr-4 text-right">{t("portfolio.holdings.avg_cost")}</th>
            <th className="pb-2 pr-4 text-right">{t("portfolio.holdings.price")}</th>
            <th className="pb-2 pr-4 text-right">{t("portfolio.holdings.value")} ({currency})</th>
            <th className="pb-2 pr-4 text-right">{t("portfolio.holdings.pnl")}</th>
            <th className="pb-2 text-right">{t("portfolio.holdings.pnl_pct")}</th>
            <th className="pb-2 text-right">{t("portfolio.holdings.weight")}</th>
          </tr>
        </thead>
        <tbody>
          {holdings.map((h) => (
            <tr key={h.id} className="border-b border-border/50 hover:bg-secondary/30 transition-colors">
              <td className="py-2 pr-4 font-medium text-foreground">{h.symbol}</td>
              <td className="py-2 pr-4 text-muted-foreground">{h.market}</td>
              <td className="py-2 pr-4 text-right">{h.quantity.toLocaleString()}</td>
              <td className="py-2 pr-4 text-right">{h.avg_cost.toFixed(2)}</td>
              <td className="py-2 pr-4 text-right">{(h.current_price ?? 0).toFixed(2)}</td>
              <td className="py-2 pr-4 text-right">{(h.current_value ?? 0).toLocaleString(undefined, { maximumFractionDigits: 0 })}</td>
              <td className={`py-2 pr-4 text-right font-medium ${(h.unrealized_pnl ?? 0) >= 0 ? "text-positive" : "text-negative"}`}>
                {(h.unrealized_pnl ?? 0) >= 0 ? "+" : ""}
                {(h.unrealized_pnl ?? 0).toLocaleString(undefined, { maximumFractionDigits: 0 })}
              </td>
              <td className={`py-2 pr-4 text-right ${(h.unrealized_pnl_pct ?? 0) >= 0 ? "text-positive" : "text-negative"}`}>
                {(h.unrealized_pnl_pct ?? 0) >= 0 ? "+" : ""}
                {(h.unrealized_pnl_pct ?? 0).toFixed(2)}%
              </td>
              <td className="py-2 text-right text-muted-foreground">{h.weight_pct?.toFixed(1)}%</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
