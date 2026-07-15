import { useState } from "react";
import { useTranslation } from "react-i18next";

import {
  useAddCashEntry,
  useCashBalance,
  useCashEntries,
  useReverseCashEntry,
} from "@/hooks/usePortfolio";

const today = () => new Date().toISOString().slice(0, 10);

export default function CashLedgerPanel({
  portfolioId, defaultCurrency,
}: { portfolioId: string; defaultCurrency: string }) {
  const { t } = useTranslation();
  const balance = useCashBalance(portfolioId);
  const entries = useCashEntries(portfolioId);
  const addEntry = useAddCashEntry(portfolioId);
  const reverseEntry = useReverseCashEntry(portfolioId);
  const [currency, setCurrency] = useState(defaultCurrency);
  const [entryType, setEntryType] = useState("deposit");
  const [amount, setAmount] = useState(0);
  const [occurredOn, setOccurredOn] = useState(today());
  const [notes, setNotes] = useState("");

  const submit = async () => {
    if (!Number.isFinite(amount) || amount <= 0) return;
    await addEntry.mutateAsync({
      currency, amount, entry_type: entryType, occurred_on: occurredOn,
      notes: notes || undefined,
    });
    setAmount(0);
    setNotes("");
  };

  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-border bg-card p-4">
        <h2 className="font-medium text-foreground">{t("portfolio.cash.title")}</h2>
        <p className="mt-1 text-xs text-muted-foreground">{t("portfolio.cash.subtitle")}</p>
        <div className="mt-3 flex flex-wrap gap-2">
          {Object.entries(balance.data?.balances ?? {}).map(([code, value]) => (
            <div key={code} className={`rounded border px-3 py-2 ${value < 0 ? "border-danger/40 text-danger" : "border-border text-foreground"}`}>
              <div className="text-xs text-muted-foreground">{code}</div>
              <div className="font-semibold">{value.toLocaleString()}</div>
            </div>
          ))}
          <div className="rounded border border-primary/30 bg-primary/5 px-3 py-2">
            <div className="text-xs text-muted-foreground">{t("portfolio.cash.base_total", { currency: balance.data?.base_currency ?? defaultCurrency })}</div>
            <div className="font-semibold text-primary">{(balance.data?.total_cash_base ?? 0).toLocaleString()}</div>
          </div>
        </div>
        {!!balance.data?.negative_currencies.length && (
          <p className="mt-2 text-xs text-danger">{t("portfolio.cash.negative_warning", { currencies: balance.data.negative_currencies.join(", ") })}</p>
        )}
      </div>

      <div className="rounded-lg border border-border bg-card p-4">
        <h3 className="text-sm font-medium text-foreground">{t("portfolio.cash.add")}</h3>
        <div className="mt-3 flex flex-wrap items-end gap-2">
          <label className="text-xs text-muted-foreground">{t("portfolio.cash.type")}
            <select value={entryType} onChange={(event) => setEntryType(event.target.value)} className="mt-1 block rounded border border-border bg-background px-3 py-2 text-sm text-foreground">
              {(["deposit", "withdrawal", "fee", "tax", "interest", "dividend", "refund", "adjustment_credit", "adjustment_debit"]).map((type) => <option key={type} value={type}>{t(`portfolio.cash.type_${type}`)}</option>)}
            </select>
          </label>
          <label className="text-xs text-muted-foreground">{t("portfolio.currency")}
            <input value={currency} maxLength={3} onChange={(event) => setCurrency(event.target.value.toUpperCase())} className="mt-1 block w-20 rounded border border-border bg-background px-3 py-2 text-sm text-foreground" />
          </label>
          <label className="text-xs text-muted-foreground">{t("portfolio.cash.amount")}
            <input aria-label={t("portfolio.cash.amount")} type="number" min={0.01} value={amount || ""} onChange={(event) => setAmount(Number(event.target.value))} className="mt-1 block w-36 rounded border border-border bg-background px-3 py-2 text-sm text-foreground" />
          </label>
          <label className="text-xs text-muted-foreground">{t("portfolio.cash.date")}
            <input type="date" value={occurredOn} onChange={(event) => setOccurredOn(event.target.value)} className="mt-1 block rounded border border-border bg-background px-3 py-2 text-sm text-foreground" />
          </label>
          <label className="min-w-48 flex-1 text-xs text-muted-foreground">{t("portfolio.cash.notes")}
            <input value={notes} onChange={(event) => setNotes(event.target.value)} className="mt-1 block w-full rounded border border-border bg-background px-3 py-2 text-sm text-foreground" />
          </label>
          <button type="button" onClick={submit} disabled={addEntry.isPending || amount <= 0 || currency.length !== 3} className="rounded bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50">{addEntry.isPending ? t("common.loading") : t("portfolio.cash.add")}</button>
        </div>
        {addEntry.isError && <p className="mt-2 text-xs text-danger">{t("portfolio.cash.failed")}</p>}
      </div>

      <div className="overflow-x-auto rounded-lg border border-border bg-card">
        <table className="w-full text-sm">
          <thead><tr className="bg-secondary/20 text-muted-foreground"><th className="p-3 text-left">{t("portfolio.cash.date")}</th><th className="p-3 text-left">{t("portfolio.cash.type")}</th><th className="p-3 text-left">{t("portfolio.currency")}</th><th className="p-3 text-right">{t("portfolio.cash.amount")}</th><th className="p-3 text-left">{t("portfolio.cash.source")}</th><th className="p-3 text-right">{t("portfolio.holdings.actions")}</th></tr></thead>
          <tbody>{(entries.data ?? []).map((entry) => <tr key={entry.id} className="border-t border-border/60"><td className="p-3">{entry.occurred_on}</td><td className="p-3">{t(`portfolio.cash.type_${entry.entry_type}`, { defaultValue: entry.entry_type })}</td><td className="p-3">{entry.currency}</td><td className={`p-3 text-right font-medium ${entry.amount >= 0 ? "text-up" : "text-down"}`}>{entry.amount >= 0 ? "+" : ""}{entry.amount.toLocaleString()}</td><td className="p-3 text-muted-foreground">{entry.source}</td><td className="p-3 text-right">{entry.is_reversed ? <span className="text-xs text-muted-foreground">{t("portfolio.cash.reversed")}</span> : entry.entry_type !== "reversal" && !entry.reversal_of && <button type="button" onClick={() => reverseEntry.mutate(entry.id)} disabled={reverseEntry.isPending} className="text-xs text-danger hover:underline">{t("portfolio.cash.reverse")}</button>}</td></tr>)}</tbody>
        </table>
        {!entries.isLoading && !entries.data?.length && <p className="p-6 text-center text-sm text-muted-foreground">{t("portfolio.cash.empty")}</p>}
      </div>
      <p className="text-xs text-muted-foreground">{t("portfolio.cash.append_only")}</p>
    </div>
  );
}
