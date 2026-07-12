import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import api from "@/lib/api";
import { formatPct } from "@/lib/formatters";
import { CollapsibleHeader } from "@/components/Collapsible";
import { useCollapsible } from "@/hooks/useCollapsible";
import { DataTable, type DataTableColumn } from "../ui/table";

/**
 * Empirical signal-quality dashboard. PR #250 shipped the backend
 * service + CLI; this is the AdminPage half.
 *
 * Two stacked tables: numeric signals (RSI / volume_ratio / …) with
 * Pearson r + sign-match, and categorical signals (taifex.trend /
 * day_trading_trend / …) with per-bucket mean D5. Both sorted to
 * surface the most-informative rows at the top — numeric by
 * |r| descending, categorical by spread (max-min mean across
 * buckets).
 *
 * The `discussion-quality` namespace is kept distinct from
 * `discussion-audit` (PR #245) because the two answer different
 * questions (citation rate ≠ predictive power) and operators
 * should be able to read them independently.
 */

interface NumericRow {
  label: string;
  path: string;
  n: number;
  mean_value: number;
  mean_d5_return: number;
  pearson_r: number | null;
  sign_match_pct: number | null;
}

interface CategoricalBucket {
  category: string;
  n: number;
  mean_d5_return: number;
  median_d5_return: number;
}

interface CategoricalRow {
  label: string;
  path: string;
  n_total: number;
  buckets: CategoricalBucket[];
}

interface SignalQualityResp {
  discussions_audited: number;
  discussion_ids: string[];
  lookback_days: number;
  market: string | null;
  numeric: NumericRow[];
  categorical: CategoricalRow[];
}

const LOOKBACK_OPTIONS = [30, 60, 90, 180];
const MARKET_OPTIONS: Array<{ value: string | undefined; label: string }> = [
  { value: undefined, label: "All" },
  { value: "TW", label: "TW" },
  { value: "US", label: "US" },
];

/** Color the Pearson r cell so |r|>0.3 is green (directional),
 *  0.1<|r|<0.3 is yellow (weak), |r|<0.1 is muted (noise). */
export function pearsonColor(r: number | null): string {
  if (r === null) return "text-muted-foreground/50";
  const abs = Math.abs(r);
  if (abs >= 0.3) return r >= 0 ? "text-emerald-400" : "text-orange-400";
  if (abs >= 0.1) return "text-yellow-500";
  return "text-muted-foreground";
}

/** Spread of category means — used to sort categorical signals by
 *  how informative they are. A signal where bullish=+1.5% and
 *  bearish=-0.8% has spread 2.3, more useful than one where every
 *  bucket centres around zero. */
export function categoricalSpread(row: CategoricalRow): number {
  if (row.buckets.length < 2) return 0;
  const means = row.buckets.map((b) => b.mean_d5_return);
  return Math.max(...means) - Math.min(...means);
}

function fmtNum(v: number, digits = 2): string {
  const sign = v >= 0 && v !== 0 ? "+" : "";
  return `${sign}${v.toFixed(digits)}`;
}

export function SignalQualityCard() {
  const { t } = useTranslation();
  const { open, toggle } = useCollapsible("signal-quality");
  const [lookback, setLookback] = useState(60);
  const [market, setMarket] = useState<string | undefined>(undefined);

  const { data, isLoading, refetch, isFetching } = useQuery<SignalQualityResp>({
    queryKey: ["signal-quality", lookback, market],
    queryFn: () => {
      const qs = new URLSearchParams({ lookback: String(lookback) });
      if (market) qs.set("market", market);
      return api.get(`/admin/signal-quality?${qs.toString()}`)
        .then((r) => r.data);
    },
    refetchOnWindowFocus: false,
    enabled: open,
  });

  const numericSorted = (data?.numeric ?? [])
    .filter((r) => r.n > 0)
    .sort((a, b) => Math.abs(b.pearson_r ?? 0) - Math.abs(a.pearson_r ?? 0));
  const categoricalSorted = (data?.categorical ?? [])
    .filter((r) => r.n_total > 0)
    .sort((a, b) => categoricalSpread(b) - categoricalSpread(a));

  const numericColumns: DataTableColumn<NumericRow>[] = [
    {
      key: "label",
      header: t("signal_quality.col_signal"),
      render: (row) => <span className="font-mono text-foreground/90">{row.label}</span>,
    },
    {
      key: "n",
      header: "n",
      numeric: true,
    },
    {
      key: "pearson_r",
      header: t("signal_quality.col_pearson"),
      numeric: true,
      render: (row) => (
        <span className={pearsonColor(row.pearson_r)}>
          {row.pearson_r === null ? "n/a" : fmtNum(row.pearson_r, 3)}
        </span>
      ),
    },
    {
      key: "sign_match_pct",
      header: t("signal_quality.col_sign_match"),
      numeric: true,
      render: (row) => (
        <span className="text-muted-foreground">
          {row.sign_match_pct === null
            ? "n/a"
            : `${row.sign_match_pct.toFixed(1)}%`}
        </span>
      ),
    },
    {
      key: "mean_d5_return",
      header: t("signal_quality.col_mean_d5"),
      numeric: true,
      render: (row) => formatPct(row.mean_d5_return),
    },
  ];

  return (
    <div className="bg-card border border-border rounded-lg p-4 space-y-3">
      <CollapsibleHeader
        open={open}
        toggle={toggle}
        title={t("signal_quality.title")}
        subtitle={t("signal_quality.subtitle")}
        headerRight={
          open ? (
            <div
              className="flex items-center gap-2"
              onClick={(e) => e.stopPropagation()}
            >
              <select
                value={market ?? ""}
                onChange={(e) => setMarket(e.target.value || undefined)}
                className="bg-secondary/30 border border-border rounded px-2 py-1 text-xs"
              >
                {MARKET_OPTIONS.map((m) => (
                  <option key={m.label} value={m.value ?? ""}>
                    {m.label}
                  </option>
                ))}
              </select>
              <div className="flex gap-1">
                {LOOKBACK_OPTIONS.map((n) => (
                  <button
                    key={n}
                    onClick={() => setLookback(n)}
                    className={`px-2.5 py-1 text-xs rounded ${
                      lookback === n
                        ? "bg-primary text-primary-foreground"
                        : "border border-border text-muted-foreground hover:text-foreground"
                    }`}
                  >
                    {n}d
                  </button>
                ))}
              </div>
              <button
                onClick={() => refetch()}
                disabled={isFetching}
                className="px-2 py-1 text-xs rounded border border-border text-muted-foreground hover:text-foreground disabled:opacity-50"
                aria-label={t("signal_quality.refresh")}
              >
                ↻
              </button>
            </div>
          ) : null
        }
      />

      {open && (
        <>
          {isLoading || !data ? (
            <p className="text-xs text-muted-foreground animate-pulse">
              {t("common.loading")}
            </p>
          ) : data.discussions_audited === 0 ? (
            <p className="text-xs text-muted-foreground py-3">
              {t("signal_quality.empty")}
            </p>
          ) : (
            <>
              <p className="text-xs text-muted-foreground">
                {t("signal_quality.audited", {
                  count: data.discussions_audited,
                  lookback: data.lookback_days,
                })}
              </p>

              {numericSorted.length > 0 && (
                <div className="space-y-1.5">
                  <p className="text-[10px] text-muted-foreground uppercase tracking-wider">
                    {t("signal_quality.numeric_title")}
                  </p>
                  <DataTable
                    columns={numericColumns}
                    rows={numericSorted}
                    rowKey={(row) => row.path}
                    mobileMode="scroll"
                    aria-label={t("signal_quality.numeric_title")}
                  />
                  <p className="text-[10px] text-muted-foreground/80">
                    {t("signal_quality.numeric_legend")}
                  </p>
                </div>
              )}

              {categoricalSorted.length > 0 && (
                <div className="space-y-1.5">
                  <p className="text-[10px] text-muted-foreground uppercase tracking-wider">
                    {t("signal_quality.categorical_title")}
                  </p>
                  <div className="space-y-3">
                    {categoricalSorted.map((row) => {
                      const sortedBuckets = [...row.buckets].sort(
                        (a, b) => b.mean_d5_return - a.mean_d5_return,
                      );
                      return (
                        <div key={row.path} className="space-y-1">
                          <div className="flex items-baseline gap-2">
                            <span className="font-mono text-xs text-foreground/90">{row.label}</span>
                            <span className="text-[10px] text-muted-foreground">
                              n={row.n_total}
                            </span>
                          </div>
                          <table className="w-full text-xs">
                            <tbody>
                              {sortedBuckets.map((b) => (
                                <tr key={b.category} className="border-b border-border/20">
                                  <td className="py-0.5 pl-3 font-mono text-foreground/80">{b.category}</td>
                                  <td className="py-0.5 text-right tabular-nums text-muted-foreground w-12">
                                    n={b.n}
                                  </td>
                                  <td className="py-0.5 text-right tabular-nums w-20">
                                    {formatPct(b.mean_d5_return)}
                                  </td>
                                  <td className="py-0.5 text-right tabular-nums text-muted-foreground w-20">
                                    {t("signal_quality.median_label")} {formatPct(b.median_d5_return)}
                                  </td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                      );
                    })}
                  </div>
                  <p className="text-[10px] text-muted-foreground/80">
                    {t("signal_quality.categorical_legend")}
                  </p>
                </div>
              )}
            </>
          )}
        </>
      )}
    </div>
  );
}
