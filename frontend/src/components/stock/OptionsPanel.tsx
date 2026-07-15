import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import { Loading } from "./_atoms";
import { fetchOptionsAnalysis, fmt, fmtK } from "./_shared";
import type { OptionExpiryAnalytics, OptionRow } from "./_shared";
import { DataTable, type DataTableColumn } from "../ui/table";

function pctFraction(value: number | null | undefined, decimals = 1) {
  return value == null ? "—" : `${(value * 100).toFixed(decimals)}%`;
}

function ratio(value: number | null | undefined) {
  return value == null ? "—" : value.toFixed(2);
}

function IVSurface({ options, optionType }: { options: OptionRow[]; optionType: "call" | "put" }) {
  const { t } = useTranslation();
  const filtered = options.filter(
    (o) => o.contract_type?.toLowerCase() === optionType && o.implied_volatility != null,
  );
  if (!filtered.length) return null;

  const expiries = [...new Set(filtered.map((o) => o.expiration_date).filter(Boolean))].sort().slice(0, 8);
  const strikes = [...new Set(filtered.map((o) => o.strike_price))].sort((a, b) => a - b);
  const mid = Math.floor(strikes.length / 2);
  const slicedStrikes = strikes.slice(Math.max(0, mid - 6), mid + 6);
  const ivMap: Record<string, number> = {};
  filtered.forEach((o) => {
    if (o.expiration_date && o.strike_price) {
      ivMap[`${o.expiration_date}:${o.strike_price}`] = (o.implied_volatility ?? 0) * 100;
    }
  });
  const allIVs = Object.values(ivMap).filter((iv) => iv > 0);
  if (!allIVs.length || !expiries.length || !slicedStrikes.length) return null;
  const minIV = Math.min(...allIVs);
  const maxIV = Math.max(...allIVs);

  function ivColor(iv: number | undefined): string {
    if (!iv || maxIV === minIV) return "hsl(var(--card))";
    const position = (iv - minIV) / (maxIV - minIV);
    const red = Math.round(position * 239);
    const green = Math.round((1 - Math.abs(position - 0.5) * 2) * 120);
    const blue = Math.round((1 - position) * 239);
    return `rgb(${red},${green},${blue})`;
  }

  return (
    <div className="space-y-2">
      <div className="overflow-x-auto">
        <table className="text-xs border-collapse min-w-max">
          <thead>
            <tr>
              <th className="text-left pr-3 pb-1 text-muted-foreground font-medium">Strike / Expiry</th>
              {expiries.map((expiry) => (
                <th key={expiry} className="px-1 pb-1 text-muted-foreground font-medium text-center min-w-[60px]">
                  {expiry?.slice(5)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {slicedStrikes.map((strike) => (
              <tr key={strike}>
                <td className="pr-3 py-0.5 text-muted-foreground">{strike}</td>
                {expiries.map((expiry) => {
                  const iv = ivMap[`${expiry}:${strike}`];
                  return (
                    <td
                      key={expiry}
                      className="text-center py-0.5 px-1 rounded text-micro font-medium"
                      style={{ backgroundColor: ivColor(iv), color: iv ? "#f9fafb" : "transparent", minWidth: 52 }}
                    >
                      {iv ? `${iv.toFixed(1)}%` : "·"}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="flex items-center gap-2 text-micro text-muted-foreground">
        <span>{t("stock.options_analysis.low_iv")}</span>
        <div className="flex h-2 w-24 rounded overflow-hidden">
          {Array.from({ length: 24 }).map((_, index) => (
            <div
              key={index}
              style={{ flex: 1, backgroundColor: ivColor(minIV + (index / 23) * (maxIV - minIV)) }}
            />
          ))}
        </div>
        <span>{t("stock.options_analysis.high_iv")}</span>
      </div>
    </div>
  );
}

function Metric({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="rounded border border-border bg-muted/10 p-3 min-w-0">
      <p className="text-micro uppercase tracking-wide text-muted-foreground">{label}</p>
      <p className="mt-1 text-lg font-semibold tabular-nums text-foreground">{value}</p>
      {note && <p className="mt-0.5 text-micro text-muted-foreground truncate" title={note}>{note}</p>}
    </div>
  );
}

function TermStructure({ rows }: { rows: OptionExpiryAnalytics[] }) {
  const { t } = useTranslation();
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead className="text-muted-foreground border-b border-border">
          <tr>
            <th className="text-left py-2 pr-3">{t("stock.options_analysis.expiry")}</th>
            <th className="text-right px-2">DTE</th>
            <th className="text-right px-2">ATM IV</th>
            <th className="text-right px-2">{t("stock.options_analysis.expected_move")}</th>
            <th className="text-right px-2">P/C OI</th>
            <th className="text-right pl-2">{t("stock.options_analysis.max_pain")}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.expiration_date} className="border-b border-border/50 tabular-nums">
              <td className="py-2 pr-3 text-foreground">{row.expiration_date}</td>
              <td className="text-right px-2 text-muted-foreground">{row.days_to_expiry}</td>
              <td className="text-right px-2">{pctFraction(row.atm_iv)}</td>
              <td className="text-right px-2">{row.expected_move == null ? "—" : `±$${fmt(row.expected_move)}`}</td>
              <td className="text-right px-2">{ratio(row.put_call_open_interest_ratio)}</td>
              <td className="text-right pl-2">{row.max_pain == null ? "—" : `$${fmt(row.max_pain)}`}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function OptionsPanel({ symbol }: { symbol: string }) {
  const { t } = useTranslation();
  const [optionType, setOptionType] = useState<"call" | "put">("call");
  const [view, setView] = useState<"overview" | "table" | "surface">("overview");
  const [selectedExpiry, setSelectedExpiry] = useState<string>("");
  const { data: analysis, isLoading, isError } = useQuery({
    queryKey: ["options-analysis", symbol],
    queryFn: () => fetchOptionsAnalysis(symbol),
    staleTime: 300_000,
  });

  if (isLoading) return <Loading />;
  if (isError || !analysis) {
    return <div className="p-6 text-danger text-sm">{t("stock.options_analysis.load_error")}</div>;
  }
  if (!analysis.contracts.length) {
    return <div className="p-6 text-muted-foreground text-sm">{t("stock.options_analysis.empty")}</div>;
  }

  const expiry = analysis.expiries.some((row) => row.expiration_date === selectedExpiry)
    ? selectedExpiry
    : analysis.expiries[0]?.expiration_date ?? "";
  const summary = analysis.expiries.find((row) => row.expiration_date === expiry);
  const filtered = analysis.contracts.filter(
    (row) => row.contract_type === optionType && row.expiration_date === expiry,
  );
  const isYFinanceChain = analysis.quality.sources.includes("yfinance");
  const rows = filtered.slice(0, 160);
  const columns: DataTableColumn<OptionRow>[] = [
    {
      key: "strike_price", header: t("stock.options_analysis.strike"), numeric: true,
      render: (row) => <span className="font-medium text-foreground">{fmt(row.strike_price)}</span>,
    },
    { key: "bid", header: "Bid", numeric: true, render: (row) => <span>{fmt(row.bid)}</span> },
    { key: "ask", header: "Ask", numeric: true, render: (row) => <span>{fmt(row.ask)}</span> },
    { key: "last_price", header: "Last", numeric: true, render: (row) => <span>{fmt(row.last_price)}</span> },
    {
      key: "volume", header: t("stock.options_analysis.volume"), numeric: true,
      render: (row) => <span className="text-muted-foreground">{row.volume == null ? "—" : fmtK(row.volume)}</span>,
    },
    {
      key: "open_interest", header: "OI", numeric: true,
      render: (row) => <span className="text-muted-foreground">{row.open_interest == null ? "—" : fmtK(row.open_interest)}</span>,
    },
    {
      key: "implied_volatility", header: "IV", numeric: true,
      render: (row) => <span className="text-muted-foreground">{pctFraction(row.implied_volatility)}</span>,
    },
    {
      key: "delta", header: "Delta", numeric: true,
      render: (row) => <span className="text-muted-foreground">{fmt(row.delta, 3)}</span>,
    },
  ];

  const qualityTone = analysis.quality.status === "good"
    ? "border-success/30 bg-success/5 text-success"
    : analysis.quality.status === "degraded"
      ? "border-warning/30 bg-warning/5 text-warning"
      : "border-danger/30 bg-danger/5 text-danger";

  return (
    <div className="p-4 space-y-4">
      <div className={`rounded border px-3 py-2 text-xs ${qualityTone}`}>
        <div className="flex flex-wrap justify-between gap-2">
          <span className="font-medium">{t(`stock.options_analysis.quality_${analysis.quality.status}`)}</span>
          <span className="tabular-nums">
            IV {analysis.quality.iv_coverage_pct.toFixed(0)}% · OI {analysis.quality.open_interest_coverage_pct.toFixed(0)}%
          </span>
        </div>
        {analysis.quality.flags.length > 0 && (
          <p className="mt-1 opacity-90">
            {analysis.quality.flags.map((flag) => t(`stock.options_analysis.flag_${flag}`)).join(" · ")}
          </p>
        )}
      </div>

      {isYFinanceChain && (
        <div className="text-xs text-muted-foreground bg-muted/30 border border-border rounded px-3 py-2">
          {t("stock.options_hint_yfinance")}
        </div>
      )}

      <div className="flex items-center gap-3 flex-wrap">
        <div className="flex rounded border border-border overflow-hidden text-sm">
          {(["overview", "table", "surface"] as const).map((nextView) => (
            <button
              key={nextView}
              onClick={() => setView(nextView)}
              aria-pressed={view === nextView}
              className={`px-3 py-1.5 transition-colors ${
                view === nextView ? "bg-primary/20 text-primary" : "text-muted-foreground hover:text-foreground"
              }`}
            >
              {t(`stock.options_analysis.view_${nextView}`)}
            </button>
          ))}
        </div>
        <label className="text-xs text-muted-foreground inline-flex items-center gap-2">
          {t("stock.options_analysis.expiry")}
          <select
            aria-label={t("stock.options_analysis.expiry")}
            value={expiry}
            onChange={(event) => setSelectedExpiry(event.target.value)}
            className="bg-background border border-border rounded px-2 py-1.5 text-foreground"
          >
            {analysis.expiries.map((row) => (
              <option key={row.expiration_date} value={row.expiration_date}>
                {row.expiration_date} ({row.days_to_expiry}D)
              </option>
            ))}
          </select>
        </label>
        <span className="text-xs text-muted-foreground">
          {analysis.quality.rows_usable} {t("stock.options_analysis.contracts")}
        </span>
      </div>

      {view === "overview" && summary && (
        <div className="space-y-4">
          <div className="grid grid-cols-2 lg:grid-cols-5 gap-2">
            <Metric
              label="ATM IV"
              value={pctFraction(summary.atm_iv)}
              note={t("stock.options_analysis.call_put_iv", {
                call: pctFraction(summary.atm_call_iv), put: pctFraction(summary.atm_put_iv),
              })}
            />
            <Metric
              label={t("stock.options_analysis.expected_move")}
              value={summary.expected_move == null ? "—" : `±$${fmt(summary.expected_move)}`}
              note={pctFraction(summary.expected_move_pct)}
            />
            <Metric label="Put / Call OI" value={ratio(summary.put_call_open_interest_ratio)} note={`${fmtK(summary.put_open_interest)} / ${fmtK(summary.call_open_interest)}`} />
            <Metric
              label={t("stock.options_analysis.max_pain")}
              value={summary.max_pain == null ? "—" : `$${fmt(summary.max_pain)}`}
              note={summary.max_pain_distance_pct == null ? undefined : t(
                "stock.options_analysis.vs_spot",
                { value: `${summary.max_pain_distance_pct.toFixed(1)}%` },
              )}
            />
            <Metric
              label={t("stock.options_analysis.wing_skew")}
              value={summary.wing_skew_iv_points == null ? "—" : `${summary.wing_skew_iv_points.toFixed(1)} pt`}
              note="90% put IV − 110% call IV"
            />
          </div>
          <TermStructure rows={analysis.expiries} />
        </div>
      )}

      {(view === "table" || view === "surface") && (
        <div className="flex rounded border border-border overflow-hidden text-sm w-fit">
          {(["call", "put"] as const).map((kind) => (
            <button
              key={kind}
              onClick={() => setOptionType(kind)}
              aria-pressed={optionType === kind}
              className={`px-4 py-1.5 transition-colors ${
                optionType === kind
                  ? kind === "call" ? "bg-up/15 text-up" : "bg-down/15 text-down"
                  : "text-muted-foreground hover:text-foreground"
              }`}
            >
              {kind.toUpperCase()}
            </button>
          ))}
        </div>
      )}

      {view === "surface" && <IVSurface options={analysis.contracts} optionType={optionType} />}
      {view === "table" && (
        <DataTable
          columns={columns}
          rows={rows}
          rowKey={(row, index) => row.ticker || `${row.expiration_date}:${row.contract_type}:${row.strike_price}:${index}`}
          mobileMode="scroll"
          aria-label={t("stock.options_analysis.chain_table")}
          empty={<div className="py-6 text-center text-muted-foreground text-xs">{t("stock.options_analysis.no_contracts")}</div>}
        />
      )}

      <details className="text-xs text-muted-foreground border-t border-border pt-3">
        <summary className="cursor-pointer text-foreground">{t("stock.options_analysis.methodology")}</summary>
        <ul className="mt-2 space-y-1 list-disc pl-5">
          {Object.entries(analysis.methodology).map(([key, value]) => (
            <li key={key}><span className="font-medium">{key}:</span> {value}</li>
          ))}
        </ul>
        <p className="mt-2">{t("stock.options_analysis.disclaimer")}</p>
      </details>
    </div>
  );
}
