import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import api from "@/lib/api";
import { Num } from "@/components/Num";
import { DataTable, type DataTableColumn } from "../ui/table";

interface RebalanceTrade {
  symbol: string;
  market: string;
  side: string;
  quantity: number;
  est_price: number;
  price_currency: string;
  est_value: number;
  est_fee: number;
  current_weight_pct: number;
  target_weight_pct: number;
}

interface RebalancePlan {
  currency: string;
  total_value: number;
  target: string;
  trades: RebalanceTrade[];
  frozen: { symbol: string; market: string; current_value: number }[];
  summary: {
    empty?: boolean;
    sell_total?: number;
    buy_total?: number;
    est_fees?: number;
    net_cash_flow?: number;
    trade_count?: number;
  };
}

const TARGETS = ["optimise", "equal_weight"] as const;

export default function RebalancePanel({ portfolioId }: { portfolioId: string }) {
  const { t } = useTranslation();
  const [target, setTarget] = useState<(typeof TARGETS)[number]>("optimise");
  const [feeBps, setFeeBps] = useState("4.27"); // TW 手續費 0.1425%×0.3 折 ≈ 常見值提示用
  const [oddLot, setOddLot] = useState(false);

  const plan = useMutation({
    mutationFn: async (): Promise<RebalancePlan> => {
      const { data } = await api.post(`/portfolio/${portfolioId}/rebalance-plan`, {
        target,
        fee_bps: parseFloat(feeBps) || 0,
        allow_odd_lot: oddLot,
      });
      return data;
    },
  });

  const p = plan.data;

  const columns: DataTableColumn<RebalanceTrade>[] = [
    {
      key: "symbol",
      header: t("portfolio.rebalance.col_symbol"),
      mobile: "primary",
      render: (tr) => (
        <span className="font-medium text-foreground">
          {tr.symbol}
          <span className="ml-1.5 text-[10px] text-muted-foreground">{tr.market}</span>
        </span>
      ),
    },
    {
      key: "side",
      header: t("portfolio.rebalance.col_side"),
      render: (tr) => (
        <span className={tr.side === "buy" ? "text-up" : "text-down"}>
          {tr.side === "buy" ? t("portfolio.rebalance.buy") : t("portfolio.rebalance.sell")}
        </span>
      ),
    },
    {
      key: "qty",
      header: t("portfolio.rebalance.col_qty"),
      numeric: true,
      render: (tr) => <Num value={tr.quantity} />,
    },
    {
      key: "value",
      header: t("portfolio.rebalance.col_value"),
      numeric: true,
      render: (tr) => <Num value={tr.est_value} />,
    },
    {
      key: "fee",
      header: t("portfolio.rebalance.col_fee"),
      numeric: true,
      cellClassName: "text-muted-foreground",
      render: (tr) => <Num value={tr.est_fee} />,
    },
    {
      key: "weights",
      header: t("portfolio.rebalance.col_weights"),
      align: "right",
      cellClassName: "num text-muted-foreground",
      render: (tr) => `${tr.current_weight_pct}% → ${tr.target_weight_pct}%`,
    },
  ];

  return (
    <div className="bg-card border border-border rounded-lg p-5 space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="text-foreground font-medium">{t("portfolio.rebalance.title")}</h2>
        <div className="flex gap-1 bg-secondary/30 p-1 rounded-lg">
          {TARGETS.map((tg) => (
            <button
              key={tg}
              onClick={() => setTarget(tg)}
              className={`px-3 py-1 text-xs rounded-md transition-colors ${
                target === tg ? "bg-card text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground"
              }`}
            >
              {t(`portfolio.rebalance.target_${tg}`)}
            </button>
          ))}
        </div>
        <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
          {t("portfolio.rebalance.fee_bps")}
          <input
            value={feeBps}
            onChange={(e) => setFeeBps(e.target.value)}
            inputMode="decimal"
            className="w-16 bg-secondary/40 border border-border rounded px-2 py-1 text-xs text-foreground num"
          />
        </label>
        <label className="flex items-center gap-1.5 text-xs text-muted-foreground cursor-pointer">
          <input type="checkbox" checked={oddLot} onChange={(e) => setOddLot(e.target.checked)} />
          {t("portfolio.rebalance.odd_lot")}
        </label>
        <button
          onClick={() => plan.mutate()}
          disabled={plan.isPending}
          className="ml-auto text-xs px-3 py-1.5 bg-primary/10 text-primary rounded hover:bg-primary/20 disabled:opacity-40"
        >
          {plan.isPending ? t("common.loading") : t("portfolio.rebalance.generate")}
        </button>
      </div>

      <p className="text-xs text-muted-foreground">{t("portfolio.rebalance.hint")}</p>

      {plan.isError && (
        <p className="text-sm text-danger" role="alert">
          {(plan.error as any)?.response?.data?.detail ?? t("portfolio.rebalance.failed")}
        </p>
      )}

      {p && p.summary.empty && (
        <p className="text-sm text-muted-foreground">{t("portfolio.rebalance.empty")}</p>
      )}

      {p && !p.summary.empty && p.trades.length === 0 && (
        <p className="text-sm text-muted-foreground">{t("portfolio.rebalance.balanced")}</p>
      )}

      {p && p.trades.length > 0 && (
        <>
          <DataTable
            columns={columns}
            rows={p.trades}
            rowKey={(tr) => `${tr.symbol}-${tr.side}`}
            mobileMode="cards"
            aria-label={t("portfolio.rebalance.title")}
          />

          <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-muted-foreground">
            <span>{t("portfolio.rebalance.sum_sell")}:<Num value={p.summary.sell_total ?? 0} className="ml-1 text-foreground" /></span>
            <span>{t("portfolio.rebalance.sum_buy")}:<Num value={p.summary.buy_total ?? 0} className="ml-1 text-foreground" /></span>
            <span>{t("portfolio.rebalance.sum_fees")}:<Num value={p.summary.est_fees ?? 0} className="ml-1 text-foreground" /></span>
            <span>{t("portfolio.rebalance.sum_cash")}:<Num value={p.summary.net_cash_flow ?? 0} className="ml-1 text-foreground" /></span>
          </div>
        </>
      )}

      {p && p.frozen.length > 0 && (
        <p className="text-xs text-warning">
          {t("portfolio.rebalance.frozen_note", { symbols: p.frozen.map((f) => f.symbol).join("、") })}
        </p>
      )}
    </div>
  );
}
