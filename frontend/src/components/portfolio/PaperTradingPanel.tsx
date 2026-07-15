import { FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  useCancelPaperOrder,
  useMatchPaperOrder,
  usePaperOrders,
  useSubmitPaperOrder,
} from "@/hooks/usePortfolio";
import type { PaperOrderCreate } from "@/types/portfolio";

const field = "rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground";
const openStatuses = new Set(["pending", "partially_filled"]);

export default function PaperTradingPanel({ portfolioId }: { portfolioId: string }) {
  const { t } = useTranslation();
  const orders = usePaperOrders(portfolioId);
  const submit = useSubmitPaperOrder(portfolioId);
  const cancel = useCancelPaperOrder(portfolioId);
  const match = useMatchPaperOrder(portfolioId);
  const [form, setForm] = useState<PaperOrderCreate>({
    symbol: "",
    market: "US",
    side: "buy",
    order_type: "limit",
    time_in_force: "day",
    quantity: 1,
    fee_bps: 0,
  });
  const [price, setPrice] = useState("");

  async function createOrder(event: FormEvent) {
    event.preventDefault();
    const parsedPrice = Number(price);
    await submit.mutateAsync({
      ...form,
      symbol: form.symbol.trim().toUpperCase(),
      ...(form.order_type === "limit"
        ? { limit_price: parsedPrice, reference_price: undefined }
        : { reference_price: parsedPrice, limit_price: undefined }),
    });
    setForm((current) => ({ ...current, symbol: "" }));
    setPrice("");
  }

  const error = submit.error || cancel.error || match.error;

  return (
    <div className="space-y-5">
      <section className="rounded-lg border border-border bg-card p-5 shadow-highlight">
        <h2 className="font-medium text-foreground">{t("portfolio.paper.ticket")}</h2>
        <p className="mt-1 text-xs text-muted-foreground">{t("portfolio.paper.subtitle")}</p>
        <form onSubmit={createOrder} className="mt-4 grid grid-cols-2 gap-3 md:grid-cols-4">
          <input
            aria-label={t("portfolio.paper.symbol")}
            required
            placeholder={t("portfolio.paper.symbol")}
            value={form.symbol}
            onChange={(event) => setForm({ ...form, symbol: event.target.value })}
            className={field}
          />
          <select aria-label={t("portfolio.paper.market")} value={form.market} onChange={(event) => setForm({ ...form, market: event.target.value as PaperOrderCreate["market"] })} className={field}>
            <option value="US">US</option><option value="TW">TW</option><option value="CRYPTO">Crypto</option>
          </select>
          <select aria-label={t("portfolio.paper.side")} value={form.side} onChange={(event) => setForm({ ...form, side: event.target.value as PaperOrderCreate["side"] })} className={field}>
            <option value="buy">{t("portfolio.paper.buy")}</option><option value="sell">{t("portfolio.paper.sell")}</option>
          </select>
          <select aria-label={t("portfolio.paper.type")} value={form.order_type} onChange={(event) => setForm({ ...form, order_type: event.target.value as PaperOrderCreate["order_type"] })} className={field}>
            <option value="limit">{t("portfolio.paper.limit")}</option><option value="market">{t("portfolio.paper.market_order")}</option>
          </select>
          <input aria-label={t("portfolio.paper.quantity")} required min="0.000001" step="any" type="number" value={form.quantity} onChange={(event) => setForm({ ...form, quantity: Number(event.target.value) })} className={field} />
          <input aria-label={t("portfolio.paper.price")} required min="0.000001" step="any" type="number" placeholder={form.order_type === "limit" ? t("portfolio.paper.limit_price") : t("portfolio.paper.reference_price")} value={price} onChange={(event) => setPrice(event.target.value)} className={field} />
          <select aria-label={t("portfolio.paper.tif")} value={form.time_in_force} onChange={(event) => setForm({ ...form, time_in_force: event.target.value as PaperOrderCreate["time_in_force"] })} className={field}>
            <option value="day">DAY</option><option value="gtc">GTC</option>
          </select>
          <button disabled={submit.isPending} className="rounded-md bg-primary px-4 py-2 text-sm text-primary-foreground disabled:opacity-50">
            {submit.isPending ? t("common.loading") : t("portfolio.paper.submit")}
          </button>
        </form>
        {error && <p role="alert" className="mt-3 text-sm text-negative">{t("portfolio.paper.failed")}</p>}
      </section>

      <section className="overflow-hidden rounded-lg border border-border bg-card shadow-highlight">
        <div className="border-b border-border px-5 py-4"><h2 className="font-medium text-foreground">{t("portfolio.paper.orders")}</h2></div>
        {orders.isLoading ? <p className="p-5 text-sm text-muted-foreground">{t("common.loading")}</p> : !orders.data?.length ? (
          <p className="p-5 text-sm text-muted-foreground">{t("portfolio.paper.empty")}</p>
        ) : (
          <div className="overflow-x-auto"><table className="w-full text-sm">
            <thead className="bg-secondary/30 text-left text-xs text-muted-foreground"><tr>
              <th className="px-4 py-3">{t("portfolio.paper.symbol")}</th><th className="px-4 py-3">{t("portfolio.paper.side")}</th><th className="px-4 py-3">{t("portfolio.paper.type")}</th><th className="px-4 py-3">{t("portfolio.paper.progress")}</th><th className="px-4 py-3">{t("portfolio.paper.status")}</th><th className="px-4 py-3 text-right">{t("portfolio.paper.actions")}</th>
            </tr></thead>
            <tbody className="divide-y divide-border">{orders.data.map((order) => (
              <tr key={order.id}>
                <td className="px-4 py-3 font-medium text-foreground">{order.symbol}<span className="ml-2 text-xs text-muted-foreground">{order.market}</span></td>
                <td className={`px-4 py-3 ${order.side === "buy" ? "text-positive" : "text-negative"}`}>{t(`portfolio.paper.${order.side}`)}</td>
                <td className="px-4 py-3 text-muted-foreground">{order.order_type.toUpperCase()} · {order.time_in_force.toUpperCase()}<div>{order.limit_price ?? order.reservation_price}</div></td>
                <td className="px-4 py-3 text-muted-foreground">{order.filled_quantity} / {order.quantity}{order.average_fill_price && <div>@ {order.average_fill_price}</div>}</td>
                <td className="px-4 py-3 text-foreground">{t(`portfolio.paper.status_${order.status}`)}</td>
                <td className="px-4 py-3 text-right">{openStatuses.has(order.status) && <div className="flex justify-end gap-2">
                  <button onClick={() => match.mutate(order.id)} disabled={match.isPending} className="text-primary hover:underline disabled:opacity-40">{t("portfolio.paper.match")}</button>
                  <button onClick={() => cancel.mutate(order.id)} disabled={cancel.isPending} className="text-negative hover:underline disabled:opacity-40">{t("portfolio.paper.cancel")}</button>
                </div>}</td>
              </tr>
            ))}</tbody>
          </table></div>
        )}
      </section>
    </div>
  );
}
