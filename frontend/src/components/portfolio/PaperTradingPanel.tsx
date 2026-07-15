import { FormEvent, Fragment, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  useCancelPaperOrder,
  useMatchPaperOrder,
  usePaperFills,
  usePaperOrders,
  usePaperPerformance,
  usePaperRiskPolicy,
  useSubmitPaperOrder,
  useUpdatePaperRiskPolicy,
} from "@/hooks/usePortfolio";
import type { PaperOrder, PaperOrderCreate, PaperRiskPolicyUpdate } from "@/types/portfolio";

const field = "rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground";
const openStatuses = new Set(["pending", "partially_filled"]);

type RiskField =
  | "max_order_notional_usd"
  | "max_order_notional_twd"
  | "max_position_notional_usd"
  | "max_position_notional_twd"
  | "max_daily_loss_usd"
  | "max_daily_loss_twd"
  | "max_open_orders"
  | "max_symbol_concentration_pct";
type RiskForm = Record<RiskField, string>;

function pnl(value: number | null, currency: string): string {
  if (value == null) return "—";
  return `${value > 0 ? "+" : ""}${value.toFixed(2)} ${currency}`;
}

function PaperPerformancePanel({ portfolioId }: { portfolioId: string }) {
  const { t } = useTranslation();
  const performance = usePaperPerformance(portfolioId);
  if (performance.isLoading) {
    return <section className="rounded-lg border border-border bg-card p-5 text-sm text-muted-foreground">{t("common.loading")}</section>;
  }
  if (performance.error) {
    return <section role="alert" className="rounded-lg border border-border bg-card p-5 text-sm text-negative">{t("portfolio.paper.performance_failed")}</section>;
  }
  const data = performance.data;
  return (
    <section className="rounded-lg border border-border bg-card p-5 shadow-highlight">
      <h2 className="font-medium text-foreground">{t("portfolio.paper.performance_title")}</h2>
      <p className="mt-1 text-xs text-muted-foreground">{t("portfolio.paper.performance_subtitle")}</p>
      {data?.truncated && <p className="mt-2 text-xs text-warning">{t("portfolio.paper.performance_truncated", { shown: data.window_fill_count, total: data.total_fill_count })}</p>}
      {!data?.total_fill_count ? <p className="mt-4 text-sm text-muted-foreground">{t("portfolio.paper.performance_empty")}</p> : (
        <div className="mt-4 grid gap-4 lg:grid-cols-2">
          {data.summaries.map((summary) => {
            const points = data.curve.filter((point) => point.currency === summary.currency);
            const values = points.map((point) => point.cumulative_realized_pnl);
            const high = Math.max(0, ...values);
            const low = Math.min(0, ...values);
            const span = high - low || 1;
            const polyline = points.map((point, index) => {
              const x = points.length === 1 ? 50 : (index / (points.length - 1)) * 100;
              const y = 36 - ((point.cumulative_realized_pnl - low) / span) * 32;
              return `${x},${y}`;
            }).join(" ");
            const zeroY = 36 - ((0 - low) / span) * 32;
            return (
              <article key={summary.currency} className="rounded-md border border-border p-4">
                <div className="flex items-start justify-between gap-3">
                  <div><p className="text-xs text-muted-foreground">{summary.currency}</p><p className={`text-xl font-semibold ${summary.total_realized_pnl >= 0 ? "text-positive" : "text-negative"}`}>{pnl(summary.total_realized_pnl, summary.currency)}</p></div>
                  <div className="text-right text-xs text-muted-foreground">{t("portfolio.paper.performance_fills", { count: summary.fill_count })}</div>
                </div>
                <svg viewBox="0 0 100 40" role="img" aria-label={`${summary.currency} ${t("portfolio.paper.pnl_curve")}`} className="mt-3 h-24 w-full overflow-visible">
                  <line x1="0" x2="100" y1={zeroY} y2={zeroY} className="stroke-border" strokeWidth="0.5" />
                  <polyline points={polyline} fill="none" className={summary.total_realized_pnl >= 0 ? "stroke-positive" : "stroke-negative"} strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
                </svg>
                <dl className="grid grid-cols-2 gap-3 text-xs md:grid-cols-3">
                  <div><dt className="text-muted-foreground">{t("portfolio.paper.win_rate")}</dt><dd className="mt-1 text-foreground">{summary.win_rate_pct == null ? "—" : `${summary.win_rate_pct.toFixed(1)}%`}</dd></div>
                  <div><dt className="text-muted-foreground">{t("portfolio.paper.profit_factor")}</dt><dd className="mt-1 text-foreground">{summary.profit_factor?.toFixed(2) ?? "—"}</dd></div>
                  <div><dt className="text-muted-foreground">{t("portfolio.paper.max_drawdown")}</dt><dd className="mt-1 text-negative">{pnl(summary.max_drawdown, summary.currency)}</dd></div>
                  <div><dt className="text-muted-foreground">{t("portfolio.paper.best_exit")}</dt><dd className="mt-1 text-positive">{pnl(summary.best_exit_pnl, summary.currency)}</dd></div>
                  <div><dt className="text-muted-foreground">{t("portfolio.paper.worst_exit")}</dt><dd className="mt-1 text-negative">{pnl(summary.worst_exit_pnl, summary.currency)}</dd></div>
                  <div><dt className="text-muted-foreground">{t("portfolio.paper.total_fees")}</dt><dd className="mt-1 text-foreground">{pnl(summary.total_fees, summary.currency)}</dd></div>
                </dl>
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}

function PaperFillAudit({ portfolioId, order }: { portfolioId: string; order: PaperOrder }) {
  const { t } = useTranslation();
  const fills = usePaperFills(portfolioId, order.id);
  if (fills.isLoading) {
    return <p className="p-4 text-sm text-muted-foreground">{t("common.loading")}</p>;
  }
  if (fills.error) {
    return <p role="alert" className="p-4 text-sm text-negative">{t("portfolio.paper.fills_failed")}</p>;
  }
  if (!fills.data?.length) {
    return <p className="p-4 text-sm text-muted-foreground">{t("portfolio.paper.fills_empty")}</p>;
  }
  const totalFee = fills.data.reduce((sum, fill) => sum + fill.fee, 0);
  const totalPnl = fills.data.reduce((sum, fill) => sum + fill.realized_pnl, 0);
  const quoted = fills.data.filter((fill) => fill.slippage_bps != null);
  const quotedQuantity = quoted.reduce((sum, fill) => sum + fill.quantity, 0);
  const averageSlippage = quotedQuantity > 0
    ? quoted.reduce((sum, fill) => sum + (fill.slippage_bps ?? 0) * fill.quantity, 0) / quotedQuantity
    : null;
  const currency = fills.data[0].currency;

  return (
    <div className="bg-secondary/10 p-4">
      <div className="mb-4 grid grid-cols-2 gap-3 text-xs md:grid-cols-4">
        <div><span className="text-muted-foreground">{t("portfolio.paper.fill_count")}</span><strong className="mt-1 block text-foreground">{fills.data.length}</strong></div>
        <div><span className="text-muted-foreground">{t("portfolio.paper.total_fees")}</span><strong className="mt-1 block text-foreground">{pnl(totalFee, currency)}</strong></div>
        <div><span className="text-muted-foreground">{t("portfolio.paper.realized_pnl")}</span><strong className={`mt-1 block ${totalPnl >= 0 ? "text-positive" : "text-negative"}`}>{pnl(totalPnl, currency)}</strong></div>
        <div><span className="text-muted-foreground">{t("portfolio.paper.average_slippage")}</span><strong className="mt-1 block text-foreground">{averageSlippage == null ? "—" : `${averageSlippage.toFixed(2)} bps`}</strong></div>
      </div>
      <div className="overflow-x-auto rounded-md border border-border bg-background">
        <table className="w-full min-w-[850px] text-xs">
          <thead className="bg-secondary/30 text-left text-muted-foreground"><tr>
            <th className="px-3 py-2">{t("portfolio.paper.filled_at")}</th>
            <th className="px-3 py-2">{t("portfolio.paper.execution")}</th>
            <th className="px-3 py-2">{t("portfolio.paper.quote")}</th>
            <th className="px-3 py-2">{t("portfolio.paper.slippage")}</th>
            <th className="px-3 py-2">{t("portfolio.paper.liquidity")}</th>
            <th className="px-3 py-2">{t("portfolio.paper.fee_pnl")}</th>
            <th className="px-3 py-2">{t("portfolio.paper.source")}</th>
          </tr></thead>
          <tbody className="divide-y divide-border">{fills.data.map((fill) => (
            <tr key={fill.id}>
              <td className="px-3 py-2 text-muted-foreground">{new Date(fill.filled_at).toLocaleString()}</td>
              <td className="px-3 py-2 text-foreground">{fill.quantity} @ {fill.price}</td>
              <td className="px-3 py-2 text-muted-foreground">{fill.quote_price ?? "—"}</td>
              <td className="px-3 py-2 text-muted-foreground">{fill.slippage_bps == null ? "—" : `${fill.slippage_bps.toFixed(2)} bps`}</td>
              <td className="px-3 py-2 text-muted-foreground">{fill.liquidity_quantity ?? "—"}</td>
              <td className="px-3 py-2"><span>{pnl(fill.fee, fill.currency)}</span><span className={`ml-2 ${fill.realized_pnl >= 0 ? "text-positive" : "text-negative"}`}>{pnl(fill.realized_pnl, fill.currency)}</span></td>
              <td className="px-3 py-2 text-muted-foreground"><span className="block text-foreground">{t(`portfolio.paper.source_${fill.execution_source}`, { defaultValue: fill.execution_source })}</span>{fill.quote_key && <code className="block max-w-48 truncate" title={fill.quote_key}>{fill.quote_key}</code>}</td>
            </tr>
          ))}</tbody>
        </table>
      </div>
    </div>
  );
}

function PaperRiskControls({ portfolioId }: { portfolioId: string }) {
  const { t } = useTranslation();
  const policy = usePaperRiskPolicy(portfolioId);
  const update = useUpdatePaperRiskPolicy(portfolioId);
  const [form, setForm] = useState<Partial<RiskForm>>({});

  function value(key: RiskField): string {
    if (form[key] !== undefined) return form[key];
    const saved = policy.data?.[key];
    return saved == null ? "" : String(saved);
  }

  function payload(tradingEnabled: boolean): PaperRiskPolicyUpdate {
    return {
      trading_enabled: tradingEnabled,
      max_order_notional_usd: value("max_order_notional_usd") ? Number(value("max_order_notional_usd")) : null,
      max_order_notional_twd: value("max_order_notional_twd") ? Number(value("max_order_notional_twd")) : null,
      max_position_notional_usd: value("max_position_notional_usd") ? Number(value("max_position_notional_usd")) : null,
      max_position_notional_twd: value("max_position_notional_twd") ? Number(value("max_position_notional_twd")) : null,
      max_daily_loss_usd: value("max_daily_loss_usd") ? Number(value("max_daily_loss_usd")) : null,
      max_daily_loss_twd: value("max_daily_loss_twd") ? Number(value("max_daily_loss_twd")) : null,
      max_open_orders: value("max_open_orders") ? Number(value("max_open_orders")) : null,
      max_symbol_concentration_pct: value("max_symbol_concentration_pct") ? Number(value("max_symbol_concentration_pct")) : null,
    };
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    await update.mutateAsync(payload(policy.data?.trading_enabled ?? true));
    setForm({});
  }

  const configs: Array<{ key: RiskField; suffix?: string }> = [
    { key: "max_order_notional_usd", suffix: "USD" },
    { key: "max_order_notional_twd", suffix: "TWD" },
    { key: "max_position_notional_usd", suffix: "USD" },
    { key: "max_position_notional_twd", suffix: "TWD" },
    { key: "max_daily_loss_usd", suffix: "USD" },
    { key: "max_daily_loss_twd", suffix: "TWD" },
    { key: "max_open_orders" },
    { key: "max_symbol_concentration_pct", suffix: "%" },
  ];
  const enabled = policy.data?.trading_enabled ?? true;

  return (
    <section className="rounded-lg border border-border bg-card p-5 shadow-highlight">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <h2 className="font-medium text-foreground">{t("portfolio.paper.risk_title")}</h2>
            <span className={`rounded-full px-2 py-0.5 text-xs ${enabled ? "bg-positive/15 text-positive" : "bg-negative/15 text-negative"}`}>
              {enabled ? t("portfolio.paper.risk_active") : t("portfolio.paper.risk_paused")}
            </span>
          </div>
          <p className="mt-1 text-xs text-muted-foreground">{t("portfolio.paper.risk_subtitle")}</p>
          <p className="mt-2 text-xs text-muted-foreground">
            {t("portfolio.paper.daily_pnl")}: USD {policy.data?.daily_realized_pnl_usd ?? 0} · TWD {policy.data?.daily_realized_pnl_twd ?? 0}
          </p>
        </div>
        <button
          type="button"
          disabled={update.isPending || policy.isLoading}
          onClick={() => update.mutate(payload(!enabled))}
          className={`rounded-md px-3 py-2 text-sm text-white disabled:opacity-50 ${enabled ? "bg-negative" : "bg-positive"}`}
        >
          {enabled ? t("portfolio.paper.engage_kill_switch") : t("portfolio.paper.resume_trading")}
        </button>
      </div>
      <form onSubmit={save} className="mt-4 grid grid-cols-2 gap-3 md:grid-cols-4">
        {configs.map(({ key, suffix }) => (
          <label key={key} className="text-xs text-muted-foreground">
            {t(`portfolio.paper.${key}`)} {suffix && <span>({suffix})</span>}
            <input
              aria-label={`${t(`portfolio.paper.${key}`)}${suffix ? ` ${suffix}` : ""}`}
              type="number"
              min={key === "max_open_orders" ? 1 : "0.000001"}
              max={key === "max_symbol_concentration_pct" ? 100 : undefined}
              step={key === "max_open_orders" ? 1 : "any"}
              value={value(key)}
              onChange={(event) => setForm({ ...form, [key]: event.target.value })}
              className={`${field} mt-1 w-full`}
              placeholder={t("portfolio.paper.unlimited")}
            />
          </label>
        ))}
        <button disabled={update.isPending} className="self-end rounded-md bg-primary px-4 py-2 text-sm text-primary-foreground disabled:opacity-50">
          {update.isPending ? t("common.loading") : t("portfolio.paper.save_risk")}
        </button>
      </form>
      {update.error && <p role="alert" className="mt-3 text-sm text-negative">{t("portfolio.paper.risk_failed")}</p>}
    </section>
  );
}

export default function PaperTradingPanel({ portfolioId }: { portfolioId: string }) {
  const { t } = useTranslation();
  const orders = usePaperOrders(portfolioId);
  const risk = usePaperRiskPolicy(portfolioId);
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
  const [expandedOrderId, setExpandedOrderId] = useState<string | null>(null);

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
      <PaperRiskControls portfolioId={portfolioId} />
      <PaperPerformancePanel portfolioId={portfolioId} />
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
          <button disabled={submit.isPending || risk.data?.trading_enabled === false} className="rounded-md bg-primary px-4 py-2 text-sm text-primary-foreground disabled:opacity-50">
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
              <Fragment key={order.id}>
              <tr>
                <td className="px-4 py-3 font-medium text-foreground">{order.symbol}<span className="ml-2 text-xs text-muted-foreground">{order.market}</span></td>
                <td className={`px-4 py-3 ${order.side === "buy" ? "text-positive" : "text-negative"}`}>{t(`portfolio.paper.${order.side}`)}</td>
                <td className="px-4 py-3 text-muted-foreground">{order.order_type.toUpperCase()} · {order.time_in_force.toUpperCase()}<div>{order.limit_price ?? order.reservation_price}</div></td>
                <td className="px-4 py-3 text-muted-foreground">{order.filled_quantity} / {order.quantity}{order.average_fill_price && <div>@ {order.average_fill_price}</div>}</td>
                <td className="px-4 py-3 text-foreground">{t(`portfolio.paper.status_${order.status}`)}</td>
                <td className="px-4 py-3 text-right"><div className="flex justify-end gap-2">
                  <button type="button" aria-expanded={expandedOrderId === order.id} aria-controls={`paper-fills-${order.id}`} onClick={() => setExpandedOrderId((current) => current === order.id ? null : order.id)} className="text-foreground hover:underline">{expandedOrderId === order.id ? t("portfolio.paper.hide_fills") : t("portfolio.paper.view_fills")}</button>
                  {openStatuses.has(order.status) && <>
                    <button type="button" onClick={() => match.mutate(order.id)} disabled={match.isPending} className="text-primary hover:underline disabled:opacity-40">{t("portfolio.paper.match")}</button>
                    <button type="button" onClick={() => cancel.mutate(order.id)} disabled={cancel.isPending} className="text-negative hover:underline disabled:opacity-40">{t("portfolio.paper.cancel")}</button>
                  </>}
                </div></td>
              </tr>
              {expandedOrderId === order.id && <tr id={`paper-fills-${order.id}`}><td colSpan={6} className="p-0"><PaperFillAudit portfolioId={portfolioId} order={order} /></td></tr>}
              </Fragment>
            ))}</tbody>
          </table></div>
        )}
      </section>
    </div>
  );
}
