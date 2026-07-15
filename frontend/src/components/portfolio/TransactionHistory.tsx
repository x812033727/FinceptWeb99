import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useInfiniteQuery } from "@tanstack/react-query";
import api, { errorDetail } from "@/lib/api";
import {
  useDeleteTransaction,
  useExportPortfolioTransactions,
} from "@/hooks/usePortfolio";
import { EditTransactionModal } from "./EditTransactionModal";
import { exportCSV } from "./_shared";
import type { TransactionRow } from "./_shared";
import { DataTable, type DataTableColumn } from "../ui/table";
import { ImportTransactionsDialog } from "./ImportTransactionsDialog";
import { TransactionImportHistoryDialog } from "./TransactionImportHistoryDialog";

const PAGE_SIZE = 100;

export function TransactionHistory({ portfolioId }: { portfolioId: string }) {
  const { t } = useTranslation();
  const [editing, setEditing] = useState<TransactionRow | null>(null);
  const [importing, setImporting] = useState(false);
  const [viewingImports, setViewingImports] = useState(false);
  const deleteTx = useDeleteTransaction(portfolioId);
  const exportTransactions = useExportPortfolioTransactions(portfolioId);
  const {
    data, isLoading, isError, error, hasNextPage, fetchNextPage, isFetchingNextPage,
  } = useInfiniteQuery({
    queryKey: ["portfolio-transactions", portfolioId],
    initialPageParam: 0,
    queryFn: ({ pageParam }) => api.get<TransactionRow[]>(
      `/portfolio/${portfolioId}/transactions`,
      { params: { limit: PAGE_SIZE + 1, offset: pageParam } },
    ).then((response) => ({
      rows: response.data.slice(0, PAGE_SIZE),
      nextOffset: response.data.length > PAGE_SIZE ? pageParam + PAGE_SIZE : undefined,
    })),
    getNextPageParam: (lastPage) => lastPage.nextOffset,
    staleTime: 30_000,
  });
  const txns = data?.pages.flatMap((page) => page.rows) ?? [];

  const columns: DataTableColumn<TransactionRow>[] = [
    {
      key: "tx_date",
      header: t("portfolio.transactions.executed_at"),
      render: (tx) => <span className="text-muted-foreground">{tx.tx_date}</span>,
    },
    {
      key: "symbol",
      header: t("portfolio.holdings.symbol"),
      render: (tx) => <span className="font-medium text-primary">{tx.symbol}</span>,
    },
    {
      key: "market",
      header: t("portfolio.holdings.market"),
      render: (tx) => <span className="text-muted-foreground">{tx.market}</span>,
    },
    {
      key: "tx_type",
      header: t("portfolio.transactions.type"),
      render: (tx) => (
        <span
          className={`font-medium capitalize ${
            tx.tx_type === "buy"      ? "text-up"
            : tx.tx_type === "sell"   ? "text-down"
            : "text-amber-400"
          }`}
        >
          {tx.tx_type}
        </span>
      ),
    },
    {
      key: "quantity",
      header: t("portfolio.transactions.qty"),
      numeric: true,
      cellClassName: "text-foreground",
      render: (tx) => tx.quantity.toLocaleString(),
    },
    {
      key: "price",
      header: t("portfolio.transactions.price"),
      numeric: true,
      cellClassName: "text-foreground",
      render: (tx) =>
        tx.price.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }),
    },
    {
      key: "fx_rate",
      header: t("portfolio.transactions.fx_rate"),
      numeric: true,
      cellClassName: "text-muted-foreground",
      render: (tx) =>
        tx.fx_rate === 1
          ? "—"
          : tx.fx_rate.toLocaleString(undefined, { minimumFractionDigits: 4, maximumFractionDigits: 4 }),
    },
    {
      key: "value",
      header: t("portfolio.holdings.value"),
      numeric: true,
      cellClassName: "text-muted-foreground whitespace-nowrap",
      render: (tx) => (
        <>
          {(tx.quantity * tx.price).toLocaleString(undefined, { maximumFractionDigits: 0 })}
          <span className="ml-1 text-micro opacity-60">{tx.market === "TW" ? "TWD" : "USD"}</span>
        </>
      ),
    },
    {
      key: "notes",
      header: "Notes",
      cellClassName: "text-muted-foreground max-w-[160px] truncate",
      render: (tx) => tx.notes ?? "",
    },
    {
      key: "actions",
      header: "",
      align: "right",
      // Actions stay visible (the original row-level `group-hover` reveal
      // never fired on touch, so edit/delete were unreachable on mobile).
      cellClassName: "whitespace-nowrap",
      render: (tx) => (
        <>
          <button
            onClick={() => setEditing(tx)}
            className="text-muted-foreground hover:text-foreground mr-2"
            title={t("common.edit")}
          >✎</button>
          <button
            onClick={() => {
              if (confirm(t("portfolio.confirm_delete_tx"))) deleteTx.mutate(tx.id);
            }}
            className="text-muted-foreground hover:text-danger"
            title={t("common.delete")}
          >×</button>
        </>
      ),
    },
  ];

  async function handleExport() {
    try {
      const allTransactions = await exportTransactions.mutateAsync();
      exportCSV(
        allTransactions.map((tx) => ({
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
        `transactions-${portfolioId}.csv`,
      );
    } catch {
      // The mutation error is rendered below with the API's explanation.
    }
  }

  return (
    <div className="bg-card border border-border rounded-lg p-5 space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-foreground font-medium">{t("portfolio.transactions.title")}</h2>
        <div className="flex flex-wrap justify-end gap-3">
          <button onClick={() => setImporting(true)} className="text-xs text-primary hover:underline">
            {t("portfolio.transactions.import.action")}
          </button>
          <button onClick={() => setViewingImports(true)} className="text-xs text-primary hover:underline">
            {t("portfolio.transactions.import_history.action")}
          </button>
          <button
            onClick={() => void handleExport()}
            disabled={!txns.length || exportTransactions.isPending}
            className="text-xs text-primary hover:underline disabled:opacity-40"
          >
            {exportTransactions.isPending
              ? t("portfolio.transactions.exporting")
              : t("portfolio.transactions.export")}
          </button>
        </div>
      </div>

      {isLoading && <p className="text-xs text-muted-foreground animate-pulse">{t("common.loading")}</p>}
      {isError && <p className="text-xs text-danger">{errorDetail(error)}</p>}
      {!isLoading && !isError && txns.length === 0 && (
        <p className="text-xs text-muted-foreground py-4 text-center">{t("portfolio.transactions.no_transactions")}</p>
      )}

      {txns.length > 0 && (
        <>
          <DataTable
            columns={columns}
            rows={txns}
            rowKey={(tx) => tx.id}
            mobileMode="scroll"
            aria-label={t("portfolio.transactions.title")}
          />
          <div className="flex items-center justify-between gap-3 text-xs text-muted-foreground">
            <span>{t("portfolio.transactions.loaded_count", { count: txns.length })}</span>
            {hasNextPage && (
              <button
                type="button"
                onClick={() => void fetchNextPage()}
                disabled={isFetchingNextPage}
                className="min-h-[36px] text-primary hover:underline disabled:opacity-50"
              >
                {isFetchingNextPage
                  ? t("portfolio.transactions.loading_more")
                  : t("portfolio.transactions.load_more")}
              </button>
            )}
          </div>
        </>
      )}
      {exportTransactions.isError && (
        <p className="text-xs text-danger">{errorDetail(exportTransactions.error)}</p>
      )}

      {editing && (
        <EditTransactionModal
          portfolioId={portfolioId}
          tx={editing}
          onClose={() => setEditing(null)}
        />
      )}
      {importing && (
        <ImportTransactionsDialog
          portfolioId={portfolioId}
          onClose={() => setImporting(false)}
        />
      )}
      {viewingImports && (
        <TransactionImportHistoryDialog
          portfolioId={portfolioId}
          onClose={() => setViewingImports(false)}
        />
      )}
    </div>
  );
}
