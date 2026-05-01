import { FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";
import { useCreatePortfolio } from "@/hooks/usePortfolio";

export function CreatePortfolioModal({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation();
  const create = useCreatePortfolio();
  const [name, setName] = useState("");
  const [currency, setCurrency] = useState("USD");

  async function submit(e: FormEvent) {
    e.preventDefault();
    await create.mutateAsync({ name, currency });
    onClose();
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <form onSubmit={submit} className="bg-card border border-border rounded-lg p-6 w-full max-w-sm space-y-4">
        <h3 className="text-foreground font-semibold">{t("portfolio.new_portfolio")}</h3>
        <div><label className="block text-xs text-muted-foreground mb-1">{t("portfolio.portfolio_name")}</label>
          <input required className="w-full px-3 py-1.5 rounded bg-input border border-border text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring" value={name} onChange={(e) => setName(e.target.value)} /></div>
        <div><label className="block text-xs text-muted-foreground mb-1">{t("portfolio.currency")}</label>
          <select className="w-full px-3 py-1.5 rounded bg-input border border-border text-sm text-foreground" value={currency} onChange={(e) => setCurrency(e.target.value)}>
            <option value="USD">USD</option><option value="TWD">TWD</option>
          </select></div>
        <div className="flex gap-3 justify-end">
          <button type="button" onClick={onClose} className="px-4 py-1.5 text-sm text-muted-foreground hover:text-foreground">{t("common.cancel")}</button>
          <button type="submit" disabled={create.isPending} className="px-4 py-1.5 text-sm bg-primary text-primary-foreground rounded hover:opacity-90 disabled:opacity-50">{t("portfolio.create")}</button>
        </div>
      </form>
    </div>
  );
}
