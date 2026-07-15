import { useState } from "react";
import { useTranslation } from "react-i18next";
import { errorDetail } from "@/lib/api";
import {
  useRollbackTransactionImport,
  useTransactionImportTransactions,
  useTransactionImports,
} from "@/hooks/usePortfolio";
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";

export function TransactionImportHistoryDialog({
  portfolioId, onClose,
}: { portfolioId: string; onClose: () => void }) {
  const { t, i18n } = useTranslation();
  const imports = useTransactionImports(portfolioId);
  const rollback = useRollbackTransactionImport(portfolioId);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [viewing, setViewing] = useState<string | null>(null);
  const details = useTransactionImportTransactions(portfolioId, viewing);

  function formatTradeDate(value: string) {
    return new Intl.DateTimeFormat(i18n.language, {
      dateStyle: "medium", timeZone: "UTC",
    }).format(new Date(`${value}T00:00:00Z`));
  }

  async function confirmRollback(importId: string) {
    try {
      await rollback.mutateAsync(importId);
      setConfirming(null);
    } catch {
      // The mutation retains the API error so the dialog can explain why
      // the batch cannot be rolled back (for example, a dependent sell).
    }
  }

  return (
    <Dialog open onOpenChange={(next) => { if (!next) onClose(); }}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>{t("portfolio.transactions.import_history.title")}</DialogTitle>
          <DialogDescription>
            {t("portfolio.transactions.import_history.description")}
          </DialogDescription>
        </DialogHeader>

        {imports.isLoading && (
          <p className="py-6 text-center text-sm text-muted-foreground animate-pulse">
            {t("common.loading")}
          </p>
        )}
        {imports.isError && (
          <p className="rounded bg-danger/10 p-3 text-sm text-danger">
            {errorDetail(imports.error)}
          </p>
        )}
        {!imports.isLoading && !imports.isError && !imports.data?.length && (
          <p className="py-6 text-center text-sm text-muted-foreground">
            {t("portfolio.transactions.import_history.empty")}
          </p>
        )}
        {!!imports.data?.length && (
          <div className="max-h-[55vh] space-y-3 overflow-y-auto pr-1">
            {imports.data.map((batch) => (
              <div key={batch.id} className="rounded-md border border-border p-3">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <p className="text-sm font-medium text-foreground">
                      {t("portfolio.transactions.import_history.rows", {
                        count: batch.row_count,
                      })}
                    </p>
                    <p className="mt-1 text-xs text-muted-foreground">
                      {new Intl.DateTimeFormat(i18n.language, {
                        dateStyle: "medium", timeStyle: "short",
                      }).format(new Date(batch.imported_at))}
                    </p>
                    {batch.first_tx_date && batch.last_tx_date && (
                      <p className="mt-2 text-xs text-muted-foreground">
                        {t("portfolio.transactions.import_history.trade_dates", {
                          start: formatTradeDate(batch.first_tx_date),
                          end: formatTradeDate(batch.last_tx_date),
                        })}
                      </p>
                    )}
                    {!!batch.instruments.length && (
                      <div className="mt-2 flex flex-wrap items-center gap-1.5">
                        {batch.instruments.slice(0, 3).map((instrument) => (
                          <span
                            key={`${instrument.market}:${instrument.symbol}`}
                            className="rounded bg-muted px-1.5 py-0.5 text-meta text-foreground"
                          >
                            {instrument.symbol} · {instrument.market}
                          </span>
                        ))}
                        {batch.instruments.length > 3 && (
                          <span className="text-meta text-muted-foreground">
                            {t("portfolio.transactions.import_history.more_instruments", {
                              count: batch.instruments.length - 3,
                            })}
                          </span>
                        )}
                      </div>
                    )}
                    <p className={`mt-2 text-xs ${
                      batch.provenance_complete ? "text-positive" : "text-warning"
                    }`}>
                      {batch.provenance_complete
                        ? t("portfolio.transactions.import_history.complete", {
                          linked: batch.linked_count, total: batch.row_count,
                        })
                        : t("portfolio.transactions.import_history.incomplete", {
                          linked: batch.linked_count, total: batch.row_count,
                        })}
                    </p>
                  </div>
                  <div className="flex items-center gap-3">
                    <button
                      type="button"
                      onClick={() => setViewing(viewing === batch.id ? null : batch.id)}
                      className="min-h-[32px] text-xs text-primary hover:underline"
                    >
                      {viewing === batch.id
                        ? t("portfolio.transactions.import_history.hide_details")
                        : t("portfolio.transactions.import_history.view_details")}
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        rollback.reset();
                        setConfirming(batch.id);
                      }}
                      disabled={!batch.provenance_complete || rollback.isPending}
                      className="min-h-[32px] rounded border border-danger/40 px-3 py-1 text-xs text-danger hover:bg-danger/10 disabled:cursor-not-allowed disabled:opacity-40"
                    >
                      {t("portfolio.transactions.import_history.rollback")}
                    </button>
                  </div>
                </div>

                {viewing === batch.id && (
                  <div className="mt-3 overflow-x-auto rounded border border-border">
                    {details.isLoading && (
                      <p className="p-3 text-xs text-muted-foreground">{t("common.loading")}</p>
                    )}
                    {details.isError && (
                      <p className="p-3 text-xs text-danger">{errorDetail(details.error)}</p>
                    )}
                    {!!details.data?.length && (
                      <table className="w-full min-w-[480px] text-xs">
                        <thead className="bg-muted text-muted-foreground">
                          <tr>
                            <th className="px-2 py-1.5 text-left">{t("portfolio.transactions.executed_at")}</th>
                            <th className="px-2 py-1.5 text-left">{t("portfolio.holdings.symbol")}</th>
                            <th className="px-2 py-1.5 text-left">{t("portfolio.transactions.type")}</th>
                            <th className="px-2 py-1.5 text-right">{t("portfolio.transactions.qty")}</th>
                            <th className="px-2 py-1.5 text-right">{t("portfolio.transactions.price")}</th>
                          </tr>
                        </thead>
                        <tbody>
                          {details.data.map((transaction) => (
                            <tr key={transaction.id} className="border-t border-border">
                              <td className="px-2 py-1.5 text-muted-foreground">{transaction.tx_date}</td>
                              <td className="px-2 py-1.5 font-medium">{transaction.symbol} · {transaction.market}</td>
                              <td className="px-2 py-1.5 capitalize">{transaction.tx_type}</td>
                              <td className="px-2 py-1.5 text-right">{transaction.quantity.toLocaleString()}</td>
                              <td className="px-2 py-1.5 text-right">{transaction.price.toLocaleString()}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    )}
                    {!details.isLoading && !details.isError && !details.data?.length && (
                      <p className="p-3 text-xs text-muted-foreground">
                        {t("portfolio.transactions.import_history.no_details")}
                      </p>
                    )}
                  </div>
                )}

                {confirming === batch.id && (
                  <div className="mt-3 rounded bg-warning/10 p-3 text-xs text-foreground">
                    <p>{t("portfolio.transactions.import_history.confirm", {
                      count: batch.row_count,
                    })}</p>
                    <div className="mt-3 flex justify-end gap-3">
                      <button
                        type="button"
                        onClick={() => setConfirming(null)}
                        className="text-muted-foreground hover:text-foreground"
                      >
                        {t("common.cancel")}
                      </button>
                      <button
                        type="button"
                        onClick={() => void confirmRollback(batch.id)}
                        disabled={rollback.isPending}
                        className="rounded bg-danger px-3 py-1.5 text-white disabled:opacity-50"
                      >
                        {rollback.isPending
                          ? t("common.saving")
                          : t("portfolio.transactions.import_history.confirm_action")}
                      </button>
                    </div>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}

        {rollback.isError && (
          <p className="rounded bg-danger/10 p-3 text-sm text-danger">
            {errorDetail(rollback.error)}
          </p>
        )}
        <DialogFooter>
          <button
            type="button"
            onClick={onClose}
            className="min-h-[36px] px-4 py-1.5 text-sm text-muted-foreground hover:text-foreground"
          >
            {t("common.close")}
          </button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
