import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import api from "@/lib/api";

type Position = {
  symbol: string;
  market: string;
  start_weight_pct: number | null;
  contribution_pct: number | null;
  position_return_pct: number | null;
  pnl_after_flows: number;
  net_cash_flow: number;
};

type Attribution = {
  currency: string;
  methodology_version: string;
  empty: boolean;
  portfolio_return_pct: number | null;
  benchmark: string | null;
  benchmark_return_pct: number | null;
  active_return_pct: number | null;
  markets: { market: string; start_weight_pct: number | null; market_return_pct: number | null; contribution_pct: number | null; pnl_after_flows: number }[];
  positions: Position[];
  excluded: { symbol: string; market: string; reason: string }[];
  disclaimer: string;
};

const PERIODS = [30, 90, 180, 365] as const;

function pct(value: number | null) {
  return value == null ? "—" : `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
}

export default function AttributionPanel({ portfolioId }: { portfolioId: string }) {
  const { t } = useTranslation();
  const [days, setDays] = useState<number>(90);
  const query = useQuery<Attribution>({
    queryKey: ["portfolio-attribution", portfolioId, days],
    queryFn: () => api.get(`/portfolio/${portfolioId}/attribution?days=${days}`).then((r) => r.data),
    staleTime: 3_600_000,
  });

  if (query.isLoading) return <div className="p-8 text-sm text-muted-foreground">{t("common.loading")}</div>;
  if (query.isError) return <div className="p-8 text-sm text-negative">{t("portfolio.attribution.error")}</div>;
  const data = query.data;
  if (!data || data.empty) return <div className="p-8 text-sm text-muted-foreground">{t("portfolio.attribution.empty")}</div>;
  const maxContribution = Math.max(...data.positions.map((row) => Math.abs(row.contribution_pct ?? 0)), 0.01);

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="font-medium text-foreground">{t("portfolio.attribution.title")}</h2>
          <p className="mt-1 text-xs text-muted-foreground">{t("portfolio.attribution.subtitle")}</p>
        </div>
        <div className="flex gap-1 rounded-lg bg-secondary/30 p-1">
          {PERIODS.map((period) => (
            <button key={period} onClick={() => setDays(period)} className={`rounded px-2.5 py-1 text-xs ${days === period ? "bg-card text-foreground shadow-sm" : "text-muted-foreground"}`}>
              {period === 365 ? "1Y" : `${period}D`}
            </button>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {[
          [t("portfolio.attribution.portfolio_return"), pct(data.portfolio_return_pct), data.portfolio_return_pct],
          [data.benchmark ?? t("portfolio.attribution.benchmark"), pct(data.benchmark_return_pct), data.benchmark_return_pct],
          [t("portfolio.attribution.active_return"), pct(data.active_return_pct), data.active_return_pct],
          [t("portfolio.attribution.method"), "Modified Dietz", null],
        ].map(([label, value, numeric]) => (
          <div key={String(label)} className="rounded-lg border border-border bg-card p-4 shadow-highlight">
            <p className="text-xs text-muted-foreground">{label}</p>
            <p className={`mt-1 text-lg font-semibold ${typeof numeric === "number" ? numeric >= 0 ? "text-positive" : "text-negative" : "text-foreground"}`}>{value}</p>
          </div>
        ))}
      </div>

      <div className="grid gap-3 sm:grid-cols-3">
        {data.markets.map((row) => (
          <div key={row.market} className="rounded-lg border border-border bg-secondary/10 p-4">
            <div className="flex items-center justify-between"><span className="font-medium">{row.market}</span><span className="text-xs text-muted-foreground">{row.start_weight_pct?.toFixed(1) ?? "—"}% {t("portfolio.attribution.start_weight").toLowerCase()}</span></div>
            <div className="mt-2 flex gap-4 text-sm"><span>{t("portfolio.attribution.position_return")}: <b>{pct(row.market_return_pct)}</b></span><span>{t("portfolio.attribution.contribution")}: <b className={(row.contribution_pct ?? 0) >= 0 ? "text-positive" : "text-negative"}>{pct(row.contribution_pct)}</b></span></div>
          </div>
        ))}
      </div>

      <div className="overflow-hidden rounded-lg border border-border bg-card">
        <div className="border-b border-border px-4 py-3">
          <h3 className="text-sm font-medium">{t("portfolio.attribution.contributors")}</h3>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[680px] text-sm">
            <thead className="bg-secondary/20 text-left text-xs text-muted-foreground">
              <tr><th className="p-3">{t("portfolio.holdings.symbol")}</th><th className="p-3">{t("portfolio.attribution.start_weight")}</th><th className="p-3">{t("portfolio.attribution.position_return")}</th><th className="p-3">{t("portfolio.attribution.contribution")}</th><th className="p-3">{t("portfolio.attribution.flow_adjusted_pnl")}</th></tr>
            </thead>
            <tbody>
              {data.positions.map((row) => {
                const contribution = row.contribution_pct ?? 0;
                return (
                  <tr key={`${row.market}:${row.symbol}`} className="border-t border-border">
                    <td className="p-3 font-medium">{row.symbol}<span className="ml-2 text-xs text-muted-foreground">{row.market}</span></td>
                    <td className="p-3">{row.start_weight_pct?.toFixed(2) ?? "—"}%</td>
                    <td className={`p-3 ${row.position_return_pct != null && row.position_return_pct >= 0 ? "text-positive" : "text-negative"}`}>{pct(row.position_return_pct)}</td>
                    <td className="p-3">
                      <div className="flex items-center gap-2">
                        <div className="h-2 w-20 overflow-hidden rounded bg-secondary"><div className={`h-full ${contribution >= 0 ? "bg-positive" : "bg-negative"}`} style={{ width: `${Math.abs(contribution) / maxContribution * 100}%` }} /></div>
                        <span className={contribution >= 0 ? "text-positive" : "text-negative"}>{pct(row.contribution_pct)}</span>
                      </div>
                    </td>
                    <td className={`p-3 ${row.pnl_after_flows >= 0 ? "text-positive" : "text-negative"}`}>{row.pnl_after_flows.toLocaleString()} {data.currency}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      {data.excluded.length > 0 && <p className="text-xs text-warning">{t("portfolio.attribution.excluded", { symbols: data.excluded.map((row) => row.symbol).join(", ") })}</p>}
      <p className="text-xs text-muted-foreground">{t("portfolio.attribution.disclaimer")}</p>
    </div>
  );
}
