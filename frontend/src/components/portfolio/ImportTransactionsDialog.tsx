import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { errorDetail } from "@/lib/api";
import { useImportTransactions, type TransactionImportResult } from "@/hooks/usePortfolio";
import { parseTransactionCSV, type ImportedTransactionRow } from "./_shared";
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";

export function ImportTransactionsDialog({
  portfolioId, onClose,
}: { portfolioId: string; onClose: () => void }) {
  const { t } = useTranslation();
  const inputRef = useRef<HTMLInputElement>(null);
  const mutation = useImportTransactions(portfolioId);
  const [rows, setRows] = useState<ImportedTransactionRow[]>([]);
  const [fileName, setFileName] = useState("");
  const [preview, setPreview] = useState<TransactionImportResult | null>(null);
  const [parseError, setParseError] = useState("");

  async function selectFile(file?: File) {
    if (!file) return;
    setParseError("");
    setPreview(null);
    try {
      const parsed = parseTransactionCSV(await file.text());
      if (parsed.length > 500) throw new Error(t("portfolio.transactions.import.too_many"));
      setRows(parsed);
      setFileName(file.name);
      setPreview(await mutation.mutateAsync({ rows: parsed, dry_run: true }));
    } catch (error) {
      setRows([]);
      setFileName(file.name);
      setParseError(errorDetail(error));
    }
  }

  async function commit() {
    const result = await mutation.mutateAsync({ rows, dry_run: false });
    setPreview(result);
    if (result.imported_count > 0) onClose();
  }

  return (
    <Dialog open onOpenChange={(next) => { if (!next) onClose(); }}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>{t("portfolio.transactions.import.title")}</DialogTitle>
          <DialogDescription>{t("portfolio.transactions.import.description")}</DialogDescription>
        </DialogHeader>
        <input
          ref={inputRef}
          className="hidden"
          type="file"
          accept=".csv,text/csv"
          onChange={(event) => void selectFile(event.target.files?.[0])}
        />
        <button
          type="button"
          onClick={() => inputRef.current?.click()}
          disabled={mutation.isPending}
          className="rounded-md border border-dashed border-border px-4 py-8 text-sm text-muted-foreground hover:border-primary hover:text-foreground disabled:opacity-50"
        >
          {fileName || t("portfolio.transactions.import.choose")}
        </button>
        <p className="text-xs text-muted-foreground">
          {t("portfolio.transactions.import.columns")}
        </p>
        {(parseError || mutation.isError) && (
          <p className="rounded bg-danger/10 p-3 text-xs text-danger">
            {parseError || errorDetail(mutation.error)}
          </p>
        )}
        {preview && (
          <div className={`rounded p-3 text-sm ${preview.duplicate ? "bg-warning/10 text-warning" : preview.valid ? "bg-positive/10 text-positive" : "bg-danger/10 text-danger"}`}>
            <p>{preview.duplicate
              ? t("portfolio.transactions.import.duplicate", { count: preview.valid_count })
              : preview.valid
                ? t("portfolio.transactions.import.ready", { count: preview.valid_count })
              : t("portfolio.transactions.import.invalid", { count: preview.errors.length })}
            </p>
            {!!preview.errors.length && (
              <ul className="mt-2 max-h-40 list-disc space-y-1 overflow-auto pl-5 text-xs">
                {preview.errors.map((error, index) => (
                  <li key={`${error.row}-${error.field}-${index}`}>
                    {t("portfolio.transactions.import.row", { row: error.row })}
                    {error.field ? ` · ${error.field}` : ""}: {error.message}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
        <DialogFooter className="gap-3 sm:gap-2">
          <button type="button" onClick={onClose} className="min-h-[36px] px-4 py-1.5 text-sm text-muted-foreground hover:text-foreground">
            {t("common.cancel")}
          </button>
          <button
            type="button"
            onClick={() => void commit()}
            disabled={!preview?.valid || preview.duplicate || mutation.isPending}
            className="min-h-[36px] rounded bg-primary px-4 py-1.5 text-sm text-primary-foreground disabled:opacity-50"
          >
            {mutation.isPending ? t("common.saving") : t("portfolio.transactions.import.confirm")}
          </button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
