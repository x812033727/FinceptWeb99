import { FormEvent, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useUpdateTransaction } from "@/hooks/usePortfolio";
import { fetchSuggestedFxRate } from "./_shared";
import type { TransactionRow } from "./_shared";

export function EditTransactionModal({
  portfolioId, tx, onClose,
}: { portfolioId: string; tx: TransactionRow; onClose: () => void }) {
  const { t } = useTranslation();
  const update = useUpdateTransaction(portfolioId);
  const [form, setForm] = useState({
    symbol: tx.symbol,
    market: tx.market,
    tx_type: tx.tx_type,
    quantity: String(tx.quantity),
    price: String(tx.price),
    fx_rate: String(tx.fx_rate),
    tx_date: tx.tx_date,
    notes: tx.notes ?? "",
  });
  // Treat the existing tx.fx_rate as user-pinned — don't silently rewrite
  // it with the suggested rate unless the user explicitly changes the
  // market or tx_date (handled below).
  const [userPinnedFx, setUserPinnedFx] = useState(true);
  const set = (k: string) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => {
    if (k === "fx_rate") setUserPinnedFx(true);
    if (k === "market" || k === "tx_date") setUserPinnedFx(false);
    setForm((f) => ({ ...f, [k]: e.target.value }));
  };

  useEffect(() => {
    if (userPinnedFx) return;
    let cancelled = false;
    (async () => {
      const rate = await fetchSuggestedFxRate(portfolioId, form.market, form.tx_date);
      if (cancelled || rate == null) return;
      setForm((f) => ({ ...f, fx_rate: String(rate) }));
    })();
    return () => { cancelled = true; };
  }, [portfolioId, form.market, form.tx_date, userPinnedFx]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    await update.mutateAsync({
      txId: tx.id,
      patch: {
        symbol: form.symbol.toUpperCase(),
        market: form.market,
        tx_type: form.tx_type,
        quantity: parseFloat(form.quantity),
        price: parseFloat(form.price),
        fx_rate: parseFloat(form.fx_rate),
        tx_date: form.tx_date,
        notes: form.notes || undefined,
      },
    });
    onClose();
  }

  const input = "w-full px-3 py-1.5 rounded bg-input border border-border text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring";
  const label = "block text-xs text-muted-foreground mb-1";

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <form onSubmit={submit} className="bg-card border border-border rounded-lg p-6 w-full max-w-md space-y-4">
        <h3 className="text-foreground font-semibold">{t("portfolio.transactions.edit")}</h3>
        <div className="grid grid-cols-2 gap-3">
          <div><label className={label}>{t("alerts.symbol")}</label><input required className={input} value={form.symbol} onChange={set("symbol")} /></div>
          <div><label className={label}>{t("alerts.market")}</label>
            <select className={input} value={form.market} onChange={set("market")}>
              <option value="US">US</option><option value="TW">TW</option><option value="CRYPTO">CRYPTO</option>
            </select>
          </div>
          <div><label className={label}>{t("portfolio.transactions.type")}</label>
            <select className={input} value={form.tx_type} onChange={set("tx_type")}>
              <option value="buy">{t("portfolio.transactions.buy")}</option>
              <option value="sell">{t("portfolio.transactions.sell")}</option>
              <option value="dividend">Dividend</option>
            </select>
          </div>
          <div><label className={label}>{t("portfolio.transactions.executed_at")}</label><input type="date" required className={input} value={form.tx_date} onChange={set("tx_date")} /></div>
          <div><label className={label}>{t("portfolio.transactions.qty")}</label><input type="number" required min="0" step="any" className={input} value={form.quantity} onChange={set("quantity")} /></div>
          <div><label className={label}>{t("portfolio.transactions.price")}</label><input type="number" required min="0" step="any" className={input} value={form.price} onChange={set("price")} /></div>
          <div><label className={label}>FX Rate</label><input type="number" min="0" step="any" className={input} value={form.fx_rate} onChange={set("fx_rate")} /></div>
          <div><label className={label}>Notes</label><input className={input} value={form.notes} onChange={set("notes")} /></div>
        </div>
        <div className="flex gap-3 justify-end">
          <button type="button" onClick={onClose} className="px-4 py-1.5 text-sm text-muted-foreground hover:text-foreground">{t("common.cancel")}</button>
          <button type="submit" disabled={update.isPending} className="px-4 py-1.5 text-sm bg-primary text-primary-foreground rounded hover:opacity-90 disabled:opacity-50">
            {update.isPending ? t("common.saving") : t("common.save")}
          </button>
        </div>
      </form>
    </div>
  );
}
