import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Num } from "@/components/Num";
import { usePortfolioRisk } from "@/hooks/usePortfolio";
import { correlationCellStyle } from "@/components/portfolio/_shared";
import type {
  RiskCorrelation,
  RiskVaREntry,
  RiskWarning,
  RiskWeightRow,
} from "@/types/portfolio";

/**
 * Risk dashboard (feature C1) — GET /portfolio/{id}/risk.
 *
 * Renders: three-method VaR tiles (95% headline, 99% + CVaR subline),
 * metric stat tiles (vol / Sharpe / Sortino / maxDD / beta), a
 * holdings weight bar, a pairwise correlation heatmap, and
 * concentration warning banners.
 *
 * Color rules follow the design system:
 *  - categorical series (weight bar) use the fixed chart-1…6 token
 *    order, folding overflow into an "other" bucket in --flat;
 *  - the heatmap is a diverging scale around 0 — negative correlation
 *    fades toward hsl(var(--down)), positive toward hsl(var(--up)),
 *    with the neutral midpoint staying near the surface color;
 *  - warnings use the semantic `warning` token, never raw palette
 *    classes (ESLint bans them).
 */

const METHODS = ["historical", "parametric", "monte_carlo"] as const;
const CHART_TOKENS = [1, 2, 3, 4, 5, 6] as const;

function chartColor(i: number): string {
  // Fixed assignment order; callers must fold index ≥ 6 into "other".
  return `hsl(var(--chart-${CHART_TOKENS[i]}))`;
}

function StatTile({ label, children, hint }: {
  label: string; children: ReactNode; hint?: string;
}) {
  return (
    <div className="bg-secondary/30 rounded p-3 min-w-0">
      <p className="text-xs text-muted-foreground truncate" title={label}>{label}</p>
      <p className="text-sm font-medium text-foreground mt-0.5">{children}</p>
      {hint && <p className="text-[10px] text-muted-foreground mt-0.5">{hint}</p>}
    </div>
  );
}

function VaRTiles({ entries, currency }: { entries: RiskVaREntry[]; currency: string }) {
  const { t } = useTranslation();
  return (
    <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
      {METHODS.map((method) => {
        const v95 = entries.find((e) => e.method === method && e.confidence_level === 0.95);
        const v99 = entries.find((e) => e.method === method && e.confidence_level === 0.99);
        return (
          <div key={method} className="bg-secondary/30 rounded p-3">
            <p className="text-xs text-muted-foreground">
              {t(`portfolio.risk.method_${method}`)}
            </p>
            {v95 ? (
              <>
                <p className="text-base font-semibold text-foreground mt-0.5">
                  <Num value={v95.var_pct * 100} format="percent" />
                </p>
                <p className="text-[11px] text-muted-foreground">
                  {t("portfolio.risk.var95_amount", {
                    amount: `${currency} ${v95.var_amount.toLocaleString()}`,
                  })}
                </p>
                {v99 && (
                  <p className="text-[11px] text-muted-foreground">
                    {t("portfolio.risk.var99")}:{" "}
                    <Num value={v99.var_pct * 100} format="percent" />
                  </p>
                )}
              </>
            ) : (
              <p className="text-sm text-muted-foreground mt-0.5">—</p>
            )}
          </div>
        );
      })}
    </div>
  );
}

function WarningBanners({ warnings }: { warnings: RiskWarning[] }) {
  const { t } = useTranslation();
  if (!warnings.length) return null;
  return (
    <div className="space-y-2" data-testid="risk-warnings">
      {warnings.map((w) => (
        <div
          key={`${w.kind}-${w.key}`}
          className="flex items-start gap-2 text-xs bg-warning/10 border border-warning/30 text-warning rounded-md px-3 py-2"
          role="alert"
        >
          <span aria-hidden>⚠</span>
          <span>
            {t(`portfolio.risk.warning_${w.kind}`, {
              key: w.key,
              weight: w.weight_pct.toFixed(1),
              threshold: w.threshold_pct.toFixed(0),
            })}
          </span>
        </div>
      ))}
    </div>
  );
}

function WeightBar({ weights }: { weights: RiskWeightRow[] }) {
  const { t } = useTranslation();
  if (!weights.length) return null;
  const sorted = [...weights].sort((a, b) => b.weight_pct - a.weight_pct);
  const named = sorted.slice(0, 6);
  const rest = sorted.slice(6);
  const otherPct = rest.reduce((s, w) => s + w.weight_pct, 0);
  const segments = [
    ...named.map((w, i) => ({ label: w.symbol, pct: w.weight_pct, color: chartColor(i), contribution: w.risk_contribution_pct })),
    ...(otherPct > 0
      ? [{ label: t("portfolio.risk.other"), pct: otherPct, color: "hsl(var(--flat))", contribution: null as number | null }]
      : []),
  ];
  return (
    <div className="space-y-2">
      <div className="flex h-3 rounded overflow-hidden" data-testid="weight-bar">
        {segments.map((s) => (
          <div
            key={s.label}
            title={`${s.label} ${s.pct.toFixed(1)}%`}
            style={{
              width: `${s.pct}%`,
              backgroundColor: s.color,
              // 2px surface gap between adjacent fills
              boxShadow: "inset -2px 0 0 hsl(var(--card))",
            }}
          />
        ))}
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1">
        {segments.map((s) => (
          <span key={s.label} className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <span className="inline-block w-2 h-2 rounded-sm" style={{ backgroundColor: s.color }} aria-hidden />
            {s.label} <Num value={s.pct} format="percent" decimals={1} />
            {s.contribution != null && (
              <span className="text-[10px]">
                ({t("portfolio.risk.risk_contribution_short")} <Num value={s.contribution} format="percent" decimals={1} />)
              </span>
            )}
          </span>
        ))}
      </div>
    </div>
  );
}

function CorrelationHeatmap({ correlation }: { correlation: RiskCorrelation }) {
  const { t } = useTranslation();
  const { symbols, matrix } = correlation;
  const n = symbols.length;
  if (!n) return null;
  const showValues = n <= 8;
  return (
    <div className="overflow-x-auto">
      <div
        className="grid gap-px w-max min-w-full"
        style={{ gridTemplateColumns: `minmax(3.5rem, auto) repeat(${n}, minmax(2.5rem, 1fr))` }}
        role="table"
        aria-label={t("portfolio.risk.correlation_title")}
        data-testid="correlation-heatmap"
      >
        <div />
        {symbols.map((s) => (
          <div key={`col-${s}`} className="text-[10px] text-muted-foreground text-center px-1 py-0.5 truncate" title={s}>
            {s}
          </div>
        ))}
        {symbols.map((rowSym, i) => (
          <div key={`row-${rowSym}`} className="contents">
            <div className="text-[10px] text-muted-foreground pr-2 py-0.5 text-right truncate self-center" title={rowSym}>
              {rowSym}
            </div>
            {symbols.map((colSym, j) => {
              const v = matrix[i]?.[j] ?? 0;
              return (
                <div
                  key={`${rowSym}-${colSym}`}
                  className="aspect-square min-h-8 rounded-sm flex items-center justify-center"
                  style={correlationCellStyle(v)}
                  title={`${rowSym} × ${colSym}: ${v.toFixed(2)}`}
                  aria-label={`${rowSym} × ${colSym}: ${v.toFixed(2)}`}
                >
                  {showValues && (
                    <span className="text-[10px] font-mono tabular-nums text-foreground/80">
                      {v.toFixed(2)}
                    </span>
                  )}
                </div>
              );
            })}
          </div>
        ))}
      </div>
    </div>
  );
}

function Skeleton() {
  return (
    <div className="space-y-3 animate-pulse" data-testid="risk-skeleton">
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        {[0, 1, 2].map((i) => <div key={i} className="h-20 bg-secondary/30 rounded" />)}
      </div>
      <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
        {[0, 1, 2, 3, 4].map((i) => <div key={i} className="h-16 bg-secondary/30 rounded" />)}
      </div>
      <div className="h-40 bg-secondary/30 rounded" />
    </div>
  );
}

export function RiskDashboardPanel({ portfolioId }: { portfolioId: string }) {
  const { t } = useTranslation();
  const { data, isLoading, isFetching, isError, refetch } = usePortfolioRisk(portfolioId);

  return (
    <div className="bg-card border border-border rounded-lg p-5 space-y-4">
      <div className="flex items-center justify-between gap-2">
        <div>
          <h2 className="text-foreground font-medium">{t("portfolio.risk.title")}</h2>
          <p className="text-xs text-muted-foreground mt-0.5">
            {data && !data.empty
              ? t("portfolio.risk.subtitle", {
                  observations: data.observations,
                  benchmark: data.benchmark ?? "—",
                })
              : t("portfolio.risk.subtitle_generic")}
          </p>
        </div>
        <button
          onClick={() => refetch()}
          disabled={isFetching}
          className="text-xs px-3 py-1 bg-primary/10 text-primary rounded hover:bg-primary/20 disabled:opacity-40"
        >
          {isFetching ? t("portfolio.risk.refreshing") : t("portfolio.risk.refresh")}
        </button>
      </div>

      {isLoading && <Skeleton />}

      {isError && !isLoading && (
        <p className="text-sm text-muted-foreground">{t("portfolio.risk.error")}</p>
      )}

      {data?.empty && (
        <p className="text-sm text-muted-foreground py-6 text-center">
          {t("portfolio.risk.empty")}
        </p>
      )}

      {data && !data.empty && (
        <>
          <WarningBanners warnings={data.warnings} />

          {/* VaR — three methods, 95% headline / 99% subline */}
          {data.var.length > 0 && (
            <section className="space-y-2">
              <h3 className="text-xs font-medium text-muted-foreground uppercase tracking-wide">
                {t("portfolio.risk.var_title")}
              </h3>
              <VaRTiles entries={data.var} currency={data.currency} />
            </section>
          )}

          {/* Metric tiles */}
          {data.metrics && (
            <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
              <StatTile label={t("portfolio.risk.vol")}>
                <Num value={data.metrics.annualised_volatility * 100} format="percent" decimals={1} />
              </StatTile>
              <StatTile label={t("portfolio.risk.sharpe")}>
                <Num value={data.metrics.sharpe_ratio} />
              </StatTile>
              <StatTile label={t("portfolio.risk.sortino")}>
                <Num value={data.metrics.sortino_ratio} />
              </StatTile>
              <StatTile label={t("portfolio.risk.max_drawdown")}>
                <Num value={data.metrics.max_drawdown * 100} format="percent" decimals={1} />
              </StatTile>
              <StatTile
                label={t("portfolio.risk.beta")}
                hint={data.benchmark ? t("portfolio.risk.vs_benchmark", { benchmark: data.benchmark }) : undefined}
              >
                {data.metrics.beta != null ? <Num value={data.metrics.beta} /> : "—"}
              </StatTile>
            </div>
          )}

          {/* Holdings weight bar */}
          {data.weights.length > 0 && (
            <section className="space-y-2">
              <h3 className="text-xs font-medium text-muted-foreground uppercase tracking-wide">
                {t("portfolio.risk.weights_title")}
              </h3>
              <WeightBar weights={data.weights} />
            </section>
          )}

          {/* Correlation heatmap */}
          {data.correlation && data.correlation.symbols.length > 1 && (
            <section className="space-y-2">
              <h3 className="text-xs font-medium text-muted-foreground uppercase tracking-wide">
                {t("portfolio.risk.correlation_title")}
              </h3>
              <CorrelationHeatmap correlation={data.correlation} />
            </section>
          )}

          {/* Excluded holdings note */}
          {data.excluded.length > 0 && (
            <p className="text-xs text-muted-foreground" data-testid="risk-excluded">
              {t("portfolio.risk.excluded_note", {
                symbols: data.excluded.map((e) => e.symbol).join(", "),
              })}
            </p>
          )}
        </>
      )}
    </div>
  );
}

export default RiskDashboardPanel;
