import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import api from "@/lib/api";
import { useDeleteTransaction } from "@/hooks/usePortfolio";
import { EditTransactionModal } from "./EditTransactionModal";
import { exportCSV } from "./_shared";
import type { TransactionRow } from "./_shared";

export function TransactionHistory({ portfolioId }: { portfolioId: string }) {
  const { t } = useTranslation();
  const [editing, setEditing] = useState<TransactionRow | null>(null);
  const deleteTx = useDeleteTransaction(portfolioId);
  const { data: txns = [], isLoading } = useQuery<TransactionRow[]>({
    queryKey: ["portfolio-transactions", portfolioId],
    queryFn: () =>
      api.get(`/portfolio/${portfolioId}/transactions?limit=200`).then((r) => r.data),
    staleTime: 30_000,
  });

  function handleExport() {
    exportCSV(
      txns.map((tx) => ({
        date: tx.tx_date,
        symbol: tx.symbol,
        market: tx.market,
        type: tx.tx_type,
        quantity: tx.quantity,
        price: tx.price,
        fx_rate: tx.fx_rate,
        value: tx.quantity * tx.price,
        notes: tx.notes ?? "",
      })),
      `transactions-${portfolioId}.csv`
    );
  }

  return (
    <div className="bg-card border border-border rounded-lg p-5 space-y-3">
      <div className="flex items-center justify-between">
        <h2 className="text-foreground font-medium">{t("portfolio.transactions.title")}</h2>
        <button
          onClick={handleExport}
          disabled={!txns.length}
          className="text-xs text-primary hover:underline disabled:opacity-40"
        >
          CSV
        </button>
      </div>

      {isLoading && <p className="text-xs text-muted-foreground animate-pulse">{t("common.loading")}</p>}
      {!isLoading && txns.length === 0 && (
        <p className="text-xs text-muted-foreground py-4 text-center">{t("portfolio.transactions.no_transactions")}</p>
      )}

      {txns.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-border text-muted-foreground">
                <th className="text-left py-2 pr-4 font-medium">{t("portfolio.transactions.executed_at")}</th>
                <th className="text-left py-2 px-2 font-medium">{t("portfolio.holdings.symbol")}</th>
                <th className="text-left py-2 px-2 font-medium">{t("portfolio.holdings.market")}</th>
                <th className="text-left py-2 px-2 font-medium">{t("portfolio.transactions.type")}</th>
                <th className="text-right py-2 px-2 font-medium">{t("portfolio.transactions.qty")}</th>
                <th className="text-right py-2 px-2 font-medium">{t("portfolio.transactions.price")}</th>
                <th className="text-right py-2 px-2 font-medium">{t("portfolio.transactions.fx_rate")}</th>
                <th className="text-right py-2 px-2 font-medium">{t("portfolio.holdings.value")}</th>
                <th className="text-left py-2 pl-4 font-medium">Notes</th>
                <th className="px-2"></th>
              </tr>
            </thead>
            <tbody>
              {txns.map((tx) => (
                <tr key={tx.id} className="border-b border-border/30 hover:bg-accent/5 group">
                  <td className="py-1.5 pr-4 text-muted-foreground">{tx.tx_date}</td>
                  <td className="py-1.5 px-2 font-medium text-primary">{tx.symbol}</td>
                  <td className="py-1.5 px-2 text-muted-foreground">{tx.market}</td>
                  <td className={`py-1.5 px-2 font-medium capitalize ${
                    tx.tx_type === "buy"      ? "text-green-400"
                    : tx.tx_type === "sell"   ? "text-red-400"
                    : "text-amber-400"
                  }`}>{tx.tx_type}</td>
                  <td className="py-1.5 px-2 text-right text-foreground">
                    {tx.quantity.toLocaleString()}
                  </td>
                  <td className="py-1.5 px-2 text-right text-foreground">
                    {tx.price.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                  </td>
                  <td className="py-1.5 px-2 text-right text-muted-foreground">
                    {tx.fx_rate === 1 ? "—" : tx.fx_rate.toLocaleString(undefined, { minimumFractionDigits: 4, maximumFractionDigits: 4 })}
                  </td>
                  <td className="py-1.5 px-2 text-right text-muted-foreground whitespace-nowrap">
                    {(tx.quantity * tx.price).toLocaleString(undefined, { maximumFractionDigits: 0 })}
                    <span className="ml-1 text-[10px] opacity-60">{tx.market === "TW" ? "TWD" : "USD"}</span>
                  </td>
                  <td className="py-1.5 pl-4 text-muted-foreground max-w-[160px] truncate">
                    {tx.notes ?? ""}
                  </td>
                  <td className="py-1.5 px-2 text-right whitespace-nowrap opacity-0 group-hover:opacity-100 transition-opacity">
                    <button
                      onClick={() => setEditing(tx)}
                      className="text-muted-foreground hover:text-foreground mr-2"
                      title={t("common.edit")}
                    >✎</button>
                    <button
                      onClick={() => {
                        if (confirm(t("portfolio.confirm_delete_tx"))) deleteTx.mutate(tx.id);
                      }}
                      className="text-muted-foreground hover:text-red-400"
                      title={t("common.delete")}
                    >×</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {editing && (
        <EditTransactionModal
          portfolioId={portfolioId}
          tx={editing}
          onClose={() => setEditing(null)}
        />
      )}
    </div>
  );
}
