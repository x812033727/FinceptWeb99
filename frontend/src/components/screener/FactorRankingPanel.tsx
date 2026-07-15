import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import api from "@/lib/api";
import { DataTable, type DataTableColumn } from "@/components/ui/table";

type Profile = "balanced" | "value" | "momentum" | "defensive" | "income";
type FactorName = "value" | "quality" | "momentum" | "low_volatility" | "income" | "liquidity";
type DiagnosticSignal = "composite" | FactorName;
type QualityStatus = "good" | "degraded" | "unavailable";
type Benchmark = "taiex_total_return" | "equal_weight";
type WeightMode = "fixed" | "walk_forward";

interface FactorCandidate {
  rank: number;
  symbol: string;
  name_zh: string | null;
  industry: string | null;
  price: number | null;
  price_session: string | null;
  fundamentals_as_of: string | null;
  quality_period_end: string | null;
  quality_available_on: string | null;
  score: number;
  composite_z: number;
  raw_composite_z: number;
  sector_adjustment: number | null;
  factor_coverage: number;
  missing_factors: string[];
  factors: Record<FactorName, { raw: number | null; z: number | null }>;
}

interface FactorQuality {
  status: QualityStatus;
  flags: string[];
  sources: string[];
  universe_size?: number;
  eligible_count?: number;
  returned_count?: number;
  momentum_coverage_pct?: number;
  quality_factor_coverage_pct?: number;
  adjusted_price_coverage_pct?: number;
  point_in_time_universe?: boolean;
  classification_coverage_pct?: number;
  security_master_coverage_pct?: number;
  sector_coverage_pct?: number;
  sector_neutral_applied?: boolean;
  stale_price_history_excluded?: number;
  future_dated_inputs_excluded?: number;
  benchmark_used?: Benchmark;
  benchmark_coverage_pct?: number;
  factor_forward_return_coverage_pct?: number;
}

interface FactorRankingResponse {
  as_of: string;
  profile: Profile;
  methodology_version: string;
  weights: Record<FactorName, number>;
  candidates: FactorCandidate[];
  quality: FactorQuality;
  methodology: Record<string, string>;
  sector_neutral: boolean;
  weight_source: "profile" | "champion";
  model_id: string | null;
}

interface FactorPromotionGate {
  eligible: boolean;
  failed_checks: string[];
  threshold_version: string;
}

interface FactorModelVersion {
  id: string;
  profile: Profile;
  version_number: number;
  methodology_version: string;
  status: "candidate" | "champion" | "retired";
  weights: Record<FactorName, number>;
  metrics: {
    period_count?: number;
    average_excess_return_pct?: number | null;
    composite_rank_ic?: number | null;
  };
  gate_result: FactorPromotionGate;
  created_at: string;
}

interface FactorPortfolioResponse {
  as_of: string;
  profile: Profile;
  methodology_version: string;
  factor_methodology_version: string;
  weight_source: "profile" | "champion";
  model_id: string | null;
  converged: boolean;
  solver_message: string;
  positions: Array<{
    symbol: string;
    name_zh: string | null;
    industry: string | null;
    price: number | null;
    weight: number;
    notional_twd: number;
    factor_score: number;
    liquidity_cap: number;
    average_daily_value_twd: number;
    risk_contribution: number;
  }>;
  summary: {
    invested_weight: number;
    cash_weight: number;
    annual_volatility: number;
    tracking_error: number;
    turnover: number;
    weighted_factor_score: number;
  };
  risk_comparison: {
    pre_trade_annual_volatility: number | null;
    post_trade_annual_volatility: number | null;
    pre_trade_tracking_error: number | null;
    post_trade_tracking_error: number | null;
    current_weight_coverage: number | null;
  };
  sector_weights: Record<string, number>;
  constraints: Array<{
    name: string;
    actual: number;
    limit: number;
    operator: string;
    passed: boolean;
    binding: boolean;
  }>;
  quality: {
    status: QualityStatus;
    flags: string[];
    requested_candidate_count: number;
    eligible_candidate_count: number;
    return_observations: number;
    excluded: Array<{ symbol: string; reason: string }>;
    adjusted_price_coverage_pct: number;
  };
}

interface PortfolioListItem {
  id: string;
  name: string;
  currency: string;
}

interface FactorRebalancePreview {
  portfolio_id: string;
  portfolio_name: string;
  portfolio_notional_twd: number;
  ledger_cash_twd: number;
  additional_cash_twd: number;
  preview_only: boolean;
  target_portfolio: FactorPortfolioResponse;
  trades: Array<{
    symbol: string;
    side: "buy" | "sell";
    quantity: number;
    execution_price_twd: number;
    gross_value_twd: number;
    fee_twd: number;
    tax_twd: number;
    total_cost_twd: number;
    impact_bps: number;
    target_weight: number;
  }>;
  cost_scenarios: Array<{ name: string; estimated_cost_twd: number }>;
  frozen: Array<{ symbol: string; market: string; reason: string }>;
  quality_flags: string[];
  summary: {
    trade_count: number;
    gross_turnover_twd: number;
    estimated_total_cost_twd: number;
    estimated_cost_bps: number;
    ending_cash_twd: number;
    funding_shortfall_twd: number;
    funded: boolean;
  };
}

interface ValidationPeriod {
  anchor: string;
  holding_count: number;
  turnover: number;
  net_return_pct: number;
  benchmark_return_pct: number;
  excess_return_pct: number;
  average_fill_pct: number;
  impact_cost_pct: number;
  deferred_trade_count: number;
  capacity_limited_count: number;
  benchmark_volatility_pct: number | null;
  market_regime: "bull" | "bear" | null;
  forward_return_observation_count: number;
  forward_return_universe_count: number;
  forward_return_coverage_pct: number;
  rank_ic: Partial<Record<DiagnosticSignal, number | null>>;
  quintile_returns_pct: Array<number | null>;
  top_bottom_spread_pct: number | null;
  factor_weights: Partial<Record<FactorName, number>>;
  weight_source_period_count: number;
  weight_fallback_reason: string | null;
}

interface FactorValidationResponse {
  profile: Profile;
  start_date: string;
  end_date: string;
  top_n: number;
  holding_sessions: number;
  transaction_cost_bps: number;
  portfolio_notional_twd: number;
  max_participation_rate: number;
  impact_coefficient_bps: number;
  benchmark_requested: Benchmark;
  benchmark_used: Benchmark;
  weight_mode: WeightMode;
  periods: ValidationPeriod[];
  summary: {
    period_count: number;
    cumulative_return_pct: number | null;
    average_period_return_pct: number | null;
    average_excess_return_pct: number | null;
    positive_period_rate_pct: number | null;
    max_drawdown_pct: number | null;
    average_turnover_pct: number | null;
    average_fill_pct: number | null;
    average_impact_cost_pct: number | null;
    blocked_trade_count: number;
    positive_excess_rate_pct: number | null;
    annualized_information_ratio: number | null;
    excess_return_t_stat: number | null;
    excess_return_ci_low_pct: number | null;
    excess_return_ci_high_pct: number | null;
  };
  regime_analysis: Record<string, {
    period_count?: number;
    average_return_pct?: number | null;
    average_excess_return_pct?: number | null;
    positive_excess_rate_pct?: number | null;
  }>;
  factor_diagnostics: Record<DiagnosticSignal, {
    period_count: number;
    average_rank_ic: number | null;
    median_rank_ic: number | null;
    positive_ic_rate_pct: number | null;
    ic_t_stat: number | null;
    p_value: number | null;
    annualized_ic_ir: number | null;
    holm_adjusted_p_value: number | null;
    significant_after_holm_5pct: boolean;
  }>;
  factor_correlation_matrix: Record<FactorName, Partial<Record<FactorName, number | null>>>;
  quantile_analysis: {
    period_count: number;
    average_returns_pct: Array<number | null>;
    average_top_bottom_spread_pct: number | null;
    positive_spread_rate_pct: number | null;
  };
  sensitivity_analysis: {
    holding_sessions: Record<string, {
      period_count: number;
      average_rank_ic: number | null;
      average_top_bottom_spread_pct: number | null;
    }>;
    top_n: Record<string, {
      period_count: number;
      average_forward_return_pct: number | null;
    }>;
  };
  factor_decay_analysis: Record<DiagnosticSignal, {
    average_rank_ic_by_horizon: Record<string, number | null>;
    peak_absolute_ic_horizon: number | null;
    direction_consistent: boolean | null;
  }>;
  weight_stability: {
    mode: WeightMode;
    base_weights: Record<FactorName, number>;
    adaptive_period_count: number;
    fallback_period_count: number;
    average_weight_turnover_pct: number;
    maximum_weight_turnover_pct: number;
    factor_ranges: Partial<Record<FactorName, {
      minimum: number | null;
      maximum: number | null;
      latest: number | null;
    }>>;
  };
  quality: FactorQuality;
  methodology: Record<string, string>;
  sector_neutral: boolean;
}

const PROFILES: Profile[] = ["balanced", "value", "momentum", "defensive", "income"];
const FACTORS: FactorName[] = ["value", "quality", "momentum", "low_volatility", "income", "liquidity"];
const DIAGNOSTIC_SIGNALS: DiagnosticSignal[] = ["composite", ...FACTORS];

function isoDate(offsetYears = 0) {
  const value = new Date();
  value.setFullYear(value.getFullYear() + offsetYears);
  return value.toISOString().slice(0, 10);
}

function pct(value: number | null | undefined) {
  return value == null ? "—" : `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
}

function unsignedPct(value: number | null | undefined) {
  return value == null ? "—" : `${value.toFixed(2)}%`;
}

function zScore(value: number | null | undefined) {
  return value == null ? "—" : `${value >= 0 ? "+" : ""}${value.toFixed(2)}`;
}

function parseCurrentWeights(value: string): Record<string, number> {
  const result: Record<string, number> = {};
  for (const item of value.split(",").map((part) => part.trim()).filter(Boolean)) {
    const [rawSymbol, rawWeight] = item.trim().split(":");
    const weight = Number(rawWeight);
    if (!rawSymbol || rawWeight == null || !Number.isFinite(weight) || weight < 0) {
      throw new Error("invalid current weight input");
    }
    const symbol = rawSymbol.trim().toUpperCase();
    result[symbol] = (result[symbol] ?? 0) + weight / 100;
  }
  if (Object.values(result).reduce((sum, weight) => sum + weight, 0) > 1 + 1e-9) {
    throw new Error("current weights exceed 100%");
  }
  return result;
}

function QualityBanner({ quality }: { quality: FactorQuality }) {
  const { t } = useTranslation();
  const tone = quality.status === "good"
    ? "border-success/30 bg-success/5"
    : quality.status === "degraded"
      ? "border-warning/30 bg-warning/5"
      : "border-border bg-muted/20";
  return (
    <div className={`rounded-lg border px-4 py-3 ${tone}`}>
      <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
        <span className="font-medium text-foreground">{t(`screener.factor.quality_${quality.status}`)}</span>
        {quality.universe_size != null && (
          <span className="text-xs text-muted-foreground">
            {t("screener.factor.coverage_summary", {
              eligible: quality.eligible_count ?? 0,
              universe: quality.universe_size,
              momentum: quality.momentum_coverage_pct ?? 0,
            })}
          </span>
        )}
      </div>
      {quality.adjusted_price_coverage_pct != null && (
        <p className="mt-1 text-xs text-muted-foreground">
          {t("screener.factor.adjusted_price_coverage", {
            coverage: quality.adjusted_price_coverage_pct,
          })}
        </p>
      )}
      {quality.quality_factor_coverage_pct != null && (
        <p className="mt-1 text-xs text-muted-foreground">
          {t("screener.factor.quality_factor_coverage", {
            coverage: quality.quality_factor_coverage_pct,
          })}
        </p>
      )}
      {quality.classification_coverage_pct != null && (
        <p className="mt-1 text-xs text-muted-foreground">
          {t("screener.factor.classification_coverage", {
            coverage: quality.classification_coverage_pct,
          })}
          {quality.sector_neutral_applied
            ? ` · ${t("screener.factor.sector_neutral_applied")}`
            : ""}
        </p>
      )}
      {quality.security_master_coverage_pct != null && (
        <p className="mt-1 text-xs text-muted-foreground">
          {t("screener.factor.security_master_coverage", {
            coverage: quality.security_master_coverage_pct,
          })}
        </p>
      )}
      {quality.benchmark_used && (
        <p className="mt-1 text-xs text-muted-foreground">
          {t("screener.factor.benchmark_summary", {
            benchmark: t(`screener.factor.benchmark_${quality.benchmark_used}`),
            coverage: quality.benchmark_coverage_pct ?? 0,
          })}
        </p>
      )}
      {quality.factor_forward_return_coverage_pct != null && (
        <p className="mt-1 text-xs text-muted-foreground">
          {t("screener.factor.factor_forward_coverage", {
            coverage: quality.factor_forward_return_coverage_pct,
          })}
        </p>
      )}
      {quality.flags.length > 0 && (
        <p className="mt-1 text-xs text-muted-foreground">
          {quality.flags.map((flag) => t(`screener.factor.flag_${flag}`)).join(" · ")}
        </p>
      )}
    </div>
  );
}

export function FactorRankingPanel() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [profile, setProfile] = useState<Profile>("balanced");
  const [sectorNeutral, setSectorNeutral] = useState(true);
  const [startDate, setStartDate] = useState(isoDate(-1));
  const [endDate, setEndDate] = useState(isoDate());
  const [portfolioNotional, setPortfolioNotional] = useState(10_000_000);
  const [maxParticipationPct, setMaxParticipationPct] = useState(5);
  const [impactCoefficientBps, setImpactCoefficientBps] = useState(10);
  const [benchmark, setBenchmark] = useState<Benchmark>("taiex_total_return");
  const [weightMode, setWeightMode] = useState<WeightMode>("walk_forward");
  const [autoPromote, setAutoPromote] = useState(false);
  const [candidateCount, setCandidateCount] = useState(30);
  const [maxPositionPct, setMaxPositionPct] = useState(10);
  const [maxSectorPct, setMaxSectorPct] = useState(30);
  const [targetVolatilityPct, setTargetVolatilityPct] = useState(20);
  const [maxTrackingErrorPct, setMaxTrackingErrorPct] = useState(12);
  const [turnoverBudgetPct, setTurnoverBudgetPct] = useState(50);
  const [minimumInvestedPct, setMinimumInvestedPct] = useState(80);
  const [currentWeightsText, setCurrentWeightsText] = useState("");
  const [selectedPortfolioId, setSelectedPortfolioId] = useState("");
  const [additionalCashTwd, setAdditionalCashTwd] = useState(0);
  const [allowOddLot, setAllowOddLot] = useState(true);
  const [feeBps, setFeeBps] = useState(14.25);
  const [slippageBps, setSlippageBps] = useState(5);
  const [validationKey, setValidationKey] = useState<string | null>(null);

  const ranking = useQuery({
    queryKey: ["tw-factor-ranking", profile, sectorNeutral],
    queryFn: () => api.get<FactorRankingResponse>(
      `/tw/factor-ranking?profile=${profile}&limit=100&sector_neutral=${sectorNeutral}`,
    ).then((r) => r.data),
    staleTime: 15 * 60_000,
  });
  const validation = useQuery({
    queryKey: ["tw-factor-validation", validationKey],
    queryFn: () => api.get<FactorValidationResponse>(`/tw/factor-validation?${validationKey}`).then((r) => r.data),
    enabled: validationKey !== null,
    retry: false,
  });
  const models = useQuery({
    queryKey: ["tw-factor-models", profile],
    queryFn: () => api.get<FactorModelVersion[]>(`/tw/factor-models?profile=${profile}`).then((r) => r.data),
  });
  const portfolios = useQuery({
    queryKey: ["portfolios", "factor-rebalance"],
    queryFn: () => api.get<PortfolioListItem[]>("/portfolio").then((response) => response.data),
  });
  const registerResearch = useMutation({
    mutationFn: () => api.post("/tw/factor-research-runs", {
      start_date: startDate, end_date: endDate, profile,
      top_n: 20, holding_sessions: 21, transaction_cost_bps: 20,
      sector_neutral: sectorNeutral, portfolio_notional_twd: portfolioNotional,
      max_participation_rate: maxParticipationPct / 100,
      impact_coefficient_bps: impactCoefficientBps,
      benchmark, weight_mode: weightMode, auto_promote: autoPromote,
    }),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["tw-factor-models", profile] }),
        queryClient.invalidateQueries({ queryKey: ["tw-factor-ranking", profile] }),
      ]);
    },
  });
  const promoteModel = useMutation({
    mutationFn: (modelId: string) => api.post(`/tw/factor-models/${modelId}/promote`),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["tw-factor-models", profile] }),
        queryClient.invalidateQueries({ queryKey: ["tw-factor-ranking", profile] }),
      ]);
    },
  });
  const portfolioPlan = useMutation({
    mutationFn: () => {
      const currentWeights = parseCurrentWeights(currentWeightsText);
      return api.post<FactorPortfolioResponse>("/tw/factor-portfolio", {
        profile, sector_neutral: sectorNeutral, weight_source: "champion",
        candidate_count: candidateCount, portfolio_notional_twd: portfolioNotional,
        max_position_weight: maxPositionPct / 100,
        max_sector_weight: maxSectorPct / 100,
        target_volatility: targetVolatilityPct / 100,
        max_tracking_error: maxTrackingErrorPct / 100,
        turnover_budget: turnoverBudgetPct / 100,
        minimum_invested_weight: minimumInvestedPct / 100,
        max_participation_rate: maxParticipationPct / 100,
        current_weights: Object.keys(currentWeights).length ? currentWeights : null,
      }).then((response) => response.data);
    },
  });
  const rebalancePreview = useMutation({
    mutationFn: () => api.post<FactorRebalancePreview>("/tw/factor-portfolio/rebalance-preview", {
      portfolio_id: selectedPortfolioId, profile, sector_neutral: sectorNeutral,
      weight_source: "champion", candidate_count: candidateCount,
      additional_cash_twd: additionalCashTwd,
      max_position_weight: maxPositionPct / 100,
      max_sector_weight: maxSectorPct / 100,
      target_volatility: targetVolatilityPct / 100,
      max_tracking_error: maxTrackingErrorPct / 100,
      turnover_budget: turnoverBudgetPct / 100,
      minimum_invested_weight: minimumInvestedPct / 100,
      max_participation_rate: maxParticipationPct / 100,
      allow_odd_lot: allowOddLot, fee_bps: feeBps,
      slippage_bps: slippageBps, impact_coefficient_bps: impactCoefficientBps,
    }).then((response) => response.data),
  });

  const columns = useMemo<DataTableColumn<FactorCandidate>[]>(() => [
    { key: "rank", header: "#", numeric: true, render: (row) => row.rank },
    {
      key: "symbol", header: t("market.table.symbol"),
      render: (row) => (
        <button type="button" className="text-left" onClick={() => navigate(`/stock/TW/${row.symbol}`)}>
          <span className="font-medium text-primary">{row.symbol}</span>
          <span className="block text-xs text-muted-foreground">{row.name_zh ?? row.industry ?? "—"}</span>
        </button>
      ),
    },
    {
      key: "score", header: t("screener.factor.score"), numeric: true,
      render: (row) => <span className="font-semibold text-primary">{row.score.toFixed(1)}</span>,
    },
    ...FACTORS.map((factor): DataTableColumn<FactorCandidate> => ({
      key: factor,
      header: t(`screener.factor.${factor}`),
      numeric: true,
      render: (row) => {
        const z = row.factors[factor]?.z;
        const title = factor === "quality" && row.quality_period_end
          ? t("screener.factor.quality_point_in_time", {
            period: row.quality_period_end,
            available: row.quality_available_on ?? "—",
          })
          : undefined;
        return <span title={title} className={z == null ? "text-muted-foreground" : z >= 0 ? "text-up" : "text-down"}>{zScore(z)}</span>;
      },
    })),
    {
      key: "coverage", header: t("screener.factor.coverage"), numeric: true,
      render: (row) => `${(row.factor_coverage * 100).toFixed(0)}%`,
    },
  ], [navigate, t]);

  const runValidation = () => {
    const query = new URLSearchParams({
      start_date: startDate, end_date: endDate, profile,
      top_n: "20", holding_sessions: "21", transaction_cost_bps: "20",
      sector_neutral: String(sectorNeutral),
      portfolio_notional_twd: String(portfolioNotional),
      max_participation_rate: String(maxParticipationPct / 100),
      impact_coefficient_bps: String(impactCoefficientBps),
      benchmark,
      weight_mode: weightMode,
    });
    setValidationKey(query.toString());
  };

  return (
    <div className="space-y-4 min-h-0 overflow-y-auto pb-8">
      <div className="flex flex-wrap gap-2">
        {PROFILES.map((item) => (
          <button
            key={item}
            type="button"
            onClick={() => { setProfile(item); setValidationKey(null); }}
            className={`rounded-md border px-3 py-2 text-sm transition-colors ${
              profile === item ? "border-primary bg-primary/10 text-primary" : "border-border text-muted-foreground hover:text-foreground"
            }`}
          >
            {t(`screener.factor.profile_${item}`)}
          </button>
        ))}
        <label className="ml-auto flex items-center gap-2 rounded-md border border-border px-3 py-2 text-sm text-muted-foreground">
          <input
            type="checkbox"
            checked={sectorNeutral}
            onChange={(event) => {
              setSectorNeutral(event.target.checked);
              setValidationKey(null);
            }}
          />
          {t("screener.factor.sector_neutral")}
        </label>
      </div>

      {ranking.isLoading && <div className="rounded-lg border border-border p-8 text-center text-sm text-muted-foreground">{t("common.loading")}</div>}
      {ranking.isError && <div className="rounded-lg border border-danger/30 bg-danger/5 p-4 text-sm text-danger">{t("screener.factor.load_failed")}</div>}
      {ranking.data && (
        <>
          <QualityBanner quality={ranking.data.quality} />
          <div className="rounded-lg border border-border bg-card p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <h2 className="font-semibold text-foreground">{t("screener.factor.ranking_title")}</h2>
                <p className="text-xs text-muted-foreground">
                  {t("screener.factor.as_of", { date: ranking.data.as_of })} · {ranking.data.methodology_version} · {t(`screener.factor.weight_source_${ranking.data.weight_source ?? "profile"}`)}
                </p>
              </div>
              <div className="flex flex-wrap gap-2 text-xs text-muted-foreground">
                {FACTORS.map((factor) => (
                  <span key={factor} className="rounded bg-secondary/40 px-2 py-1">
                    {t(`screener.factor.${factor}`)} {(ranking.data.weights[factor] * 100).toFixed(0)}%
                  </span>
                ))}
              </div>
            </div>
            <p className="mt-3 text-xs text-muted-foreground">{t("screener.factor.method_note")}</p>
          </div>
          {ranking.data.candidates.length ? (
            <div className="rounded-lg border border-border bg-card overflow-hidden">
              <DataTable
                columns={columns}
                rows={ranking.data.candidates}
                rowKey={(row) => row.symbol}
                mobileMode="scroll"
                aria-label={t("screener.factor.ranking_title")}
              />
            </div>
          ) : (
            <div className="rounded-lg border border-border p-8 text-center text-sm text-muted-foreground">{t("screener.factor.no_archive")}</div>
          )}
        </>
      )}

      <div className="rounded-lg border border-border bg-card p-4 space-y-3">
        <div>
          <h2 className="font-semibold text-foreground">{t("screener.factor.validation_title")}</h2>
          <p className="text-xs text-muted-foreground">{t("screener.factor.validation_desc")}</p>
        </div>
        <div className="flex flex-wrap items-end gap-3">
          <label className="text-xs text-muted-foreground">
            {t("screener.factor.start_date")}
            <input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} className="mt-1 block rounded border border-border bg-background px-3 py-2 text-sm text-foreground" />
          </label>
          <label className="text-xs text-muted-foreground">
            {t("screener.factor.portfolio_notional")}
            <input type="number" min={100000} step={1000000} value={portfolioNotional} onChange={(e) => setPortfolioNotional(Number(e.target.value))} className="mt-1 block w-36 rounded border border-border bg-background px-3 py-2 text-sm text-foreground" />
          </label>
          <label className="text-xs text-muted-foreground">
            {t("screener.factor.max_participation")}
            <input type="number" min={0.1} max={20} step={0.5} value={maxParticipationPct} onChange={(e) => setMaxParticipationPct(Number(e.target.value))} className="mt-1 block w-24 rounded border border-border bg-background px-3 py-2 text-sm text-foreground" />
          </label>
          <label className="text-xs text-muted-foreground">
            {t("screener.factor.impact_coefficient")}
            <input type="number" min={0} max={100} step={1} value={impactCoefficientBps} onChange={(e) => setImpactCoefficientBps(Number(e.target.value))} className="mt-1 block w-24 rounded border border-border bg-background px-3 py-2 text-sm text-foreground" />
          </label>
          <label className="text-xs text-muted-foreground">
            {t("screener.factor.end_date")}
            <input type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)} className="mt-1 block rounded border border-border bg-background px-3 py-2 text-sm text-foreground" />
          </label>
          <label className="text-xs text-muted-foreground">
            {t("screener.factor.benchmark_label")}
            <select value={benchmark} onChange={(e) => setBenchmark(e.target.value as Benchmark)} className="mt-1 block rounded border border-border bg-background px-3 py-2 text-sm text-foreground">
              <option value="taiex_total_return">{t("screener.factor.benchmark_taiex_total_return")}</option>
              <option value="equal_weight">{t("screener.factor.benchmark_equal_weight")}</option>
            </select>
          </label>
          <label className="text-xs text-muted-foreground">
            {t("screener.factor.weight_mode")}
            <select value={weightMode} onChange={(e) => setWeightMode(e.target.value as WeightMode)} className="mt-1 block rounded border border-border bg-background px-3 py-2 text-sm text-foreground">
              <option value="walk_forward">{t("screener.factor.weight_mode_walk_forward")}</option>
              <option value="fixed">{t("screener.factor.weight_mode_fixed")}</option>
            </select>
          </label>
          <button type="button" onClick={runValidation} disabled={validation.isFetching || !startDate || !endDate} className="rounded bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50">
            {validation.isFetching ? t("common.loading") : t("screener.factor.run_validation")}
          </button>
          <label className="flex items-center gap-2 pb-2 text-xs text-muted-foreground">
            <input type="checkbox" checked={autoPromote} onChange={(e) => setAutoPromote(e.target.checked)} />
            {t("screener.factor.auto_promote")}
          </label>
          <button type="button" onClick={() => registerResearch.mutate()} disabled={registerResearch.isPending || !startDate || !endDate} className="rounded border border-primary px-4 py-2 text-sm font-medium text-primary disabled:opacity-50">
            {registerResearch.isPending ? t("common.loading") : t("screener.factor.register_research")}
          </button>
        </div>

        <div className="space-y-2 rounded border border-border bg-background p-3">
          <div>
            <h3 className="text-sm font-semibold text-foreground">{t("screener.factor.model_registry")}</h3>
            <p className="text-xs text-muted-foreground">{t("screener.factor.model_registry_desc")}</p>
          </div>
          {registerResearch.isSuccess && <p className="text-xs text-success">{t("screener.factor.research_registered")}</p>}
          {(registerResearch.isError || promoteModel.isError) && <p className="text-xs text-danger">{t("screener.factor.registry_failed")}</p>}
          {models.data?.length ? (
            <div className="grid gap-2 lg:grid-cols-2">
              {models.data.map((model) => (
                <div key={model.id} className={`rounded border p-3 ${model.status === "champion" ? "border-success/40 bg-success/5" : "border-border"}`}>
                  <div className="flex items-center justify-between gap-2">
                    <div className="text-sm font-medium text-foreground">v{model.version_number} · {t(`screener.factor.model_${model.status}`)}</div>
                    {model.status === "candidate" && model.gate_result.eligible && (
                      <button type="button" onClick={() => promoteModel.mutate(model.id)} disabled={promoteModel.isPending} className="rounded bg-primary px-2 py-1 text-xs text-primary-foreground disabled:opacity-50">
                        {t("screener.factor.promote_model")}
                      </button>
                    )}
                  </div>
                  <div className="mt-1 text-xs text-muted-foreground">
                    n={model.metrics.period_count ?? 0} · {t("screener.factor.average_excess")} {pct(model.metrics.average_excess_return_pct)} · IC {zScore(model.metrics.composite_rank_ic)}
                  </div>
                  {!model.gate_result.eligible && (
                    <div className="mt-1 text-xs text-warning">{t("screener.factor.failed_gates", { gates: model.gate_result.failed_checks.map((gate) => t(`screener.factor.gate_${gate}`)).join(", ") })}</div>
                  )}
                </div>
              ))}
            </div>
          ) : <p className="text-xs text-muted-foreground">{t("screener.factor.no_registered_models")}</p>}
        </div>

        <div className="space-y-3 rounded border border-border bg-background p-3">
          <div>
            <h3 className="text-sm font-semibold text-foreground">{t("screener.factor.portfolio_optimizer")}</h3>
            <p className="text-xs text-muted-foreground">{t("screener.factor.portfolio_optimizer_desc")}</p>
          </div>
          <div className="flex flex-wrap items-end gap-2">
            {([
              ["candidate_count", candidateCount, setCandidateCount, 10, 100],
              ["max_position_pct", maxPositionPct, setMaxPositionPct, 2, 50],
              ["max_sector_pct", maxSectorPct, setMaxSectorPct, 10, 100],
              ["target_volatility_pct", targetVolatilityPct, setTargetVolatilityPct, 5, 100],
              ["max_tracking_error_pct", maxTrackingErrorPct, setMaxTrackingErrorPct, 2, 100],
              ["turnover_budget_pct", turnoverBudgetPct, setTurnoverBudgetPct, 0, 100],
              ["minimum_invested_pct", minimumInvestedPct, setMinimumInvestedPct, 20, 100],
            ] as const).map(([key, value, setter, min, max]) => (
              <label key={key} className="text-xs text-muted-foreground">
                {t(`screener.factor.${key}`)}
                <input type="number" min={min} max={max} value={value} onChange={(event) => setter(Number(event.target.value))} className="mt-1 block w-24 rounded border border-border bg-background px-2 py-2 text-sm text-foreground" />
              </label>
            ))}
            <label className="min-w-52 flex-1 text-xs text-muted-foreground">
              {t("screener.factor.current_weights")}
              <input value={currentWeightsText} onChange={(event) => setCurrentWeightsText(event.target.value)} placeholder="2330:30, 2317:20" className="mt-1 block w-full rounded border border-border bg-background px-3 py-2 text-sm text-foreground" />
            </label>
            <button type="button" onClick={() => portfolioPlan.mutate()} disabled={portfolioPlan.isPending} className="rounded bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50">
              {portfolioPlan.isPending ? t("common.loading") : t("screener.factor.build_portfolio")}
            </button>
          </div>
          {portfolioPlan.isError && <p className="text-xs text-danger">{t("screener.factor.portfolio_failed")}</p>}
          {portfolioPlan.data && (
            <div className="space-y-3">
              {!portfolioPlan.data.converged && <p className="rounded border border-danger/30 bg-danger/5 p-3 text-sm text-danger">{t("screener.factor.portfolio_infeasible")}: {portfolioPlan.data.solver_message}</p>}
              <div className="grid grid-cols-2 gap-2 lg:grid-cols-6">
                {([
                  ["portfolio_invested", unsignedPct(portfolioPlan.data.summary.invested_weight * 100)],
                  ["portfolio_cash", unsignedPct(portfolioPlan.data.summary.cash_weight * 100)],
                  ["portfolio_volatility", unsignedPct(portfolioPlan.data.summary.annual_volatility * 100)],
                  ["portfolio_tracking_error", unsignedPct(portfolioPlan.data.summary.tracking_error * 100)],
                  ["portfolio_turnover", unsignedPct(portfolioPlan.data.summary.turnover * 100)],
                  ["portfolio_factor_score", portfolioPlan.data.summary.weighted_factor_score.toFixed(1)],
                ] as const).map(([key, value]) => (
                  <div key={key} className="rounded bg-secondary/30 p-2">
                    <div className="text-xs text-muted-foreground">{t(`screener.factor.${key}`)}</div>
                    <div className="mt-1 text-sm font-semibold text-foreground">{value}</div>
                  </div>
                ))}
              </div>
              <div className="flex flex-wrap gap-2">
                {portfolioPlan.data.constraints.map((constraint) => (
                  <span key={constraint.name} className={`rounded border px-2 py-1 text-xs ${constraint.passed ? constraint.binding ? "border-warning/40 text-warning" : "border-success/40 text-success" : "border-danger/40 text-danger"}`}>
                    {t(`screener.factor.constraint_${constraint.name}`)} {constraint.operator} {unsignedPct(constraint.limit * 100)}
                  </span>
                ))}
              </div>
              <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                <span>{t("screener.factor.sector_exposure")}:</span>
                {Object.entries(portfolioPlan.data.sector_weights).map(([sector, weight]) => (
                  <span key={sector} className="rounded bg-secondary/30 px-2 py-1">{sector} {unsignedPct(weight * 100)}</span>
                ))}
              </div>
              {portfolioPlan.data.positions.length > 0 && (
                <div className="overflow-x-auto rounded border border-border">
                  <table className="w-full text-xs">
                    <thead><tr className="bg-secondary/20 text-muted-foreground"><th className="p-2 text-left">{t("market.table.symbol")}</th><th className="p-2 text-left">{t("screener.factor.industry")}</th><th className="p-2 text-right">{t("screener.factor.target_weight")}</th><th className="p-2 text-right">{t("screener.factor.notional_twd")}</th><th className="p-2 text-right">{t("screener.factor.score")}</th><th className="p-2 text-right">{t("screener.factor.risk_contribution")}</th></tr></thead>
                    <tbody>{portfolioPlan.data.positions.map((position) => (
                      <tr key={position.symbol} className="border-t border-border/60">
                        <td className="p-2 font-medium text-primary">{position.symbol}<span className="ml-1 text-muted-foreground">{position.name_zh}</span></td>
                        <td className="p-2 text-muted-foreground">{position.industry ?? "—"}</td>
                        <td className="p-2 text-right">{unsignedPct(position.weight * 100)}</td>
                        <td className="p-2 text-right">{position.notional_twd.toLocaleString()}</td>
                        <td className="p-2 text-right">{position.factor_score.toFixed(1)}</td>
                        <td className="p-2 text-right">{pct(position.risk_contribution * 100)}</td>
                      </tr>
                    ))}</tbody>
                  </table>
                </div>
              )}
              {portfolioPlan.data.quality.flags.length > 0 && <p className="text-xs text-warning">{portfolioPlan.data.quality.flags.map((flag) => t(`screener.factor.flag_${flag}`)).join(" · ")}</p>}
            </div>
          )}

          <div className="space-y-3 border-t border-border pt-3">
            <div>
              <h3 className="text-sm font-semibold text-foreground">{t("screener.factor.rebalance_preview")}</h3>
              <p className="text-xs text-muted-foreground">{t("screener.factor.rebalance_preview_desc")}</p>
            </div>
            <div className="flex flex-wrap items-end gap-2">
              <label className="text-xs text-muted-foreground">
                {t("screener.factor.source_portfolio")}
                <select aria-label={t("screener.factor.source_portfolio")} value={selectedPortfolioId} onChange={(event) => setSelectedPortfolioId(event.target.value)} className="mt-1 block min-w-48 rounded border border-border bg-background px-3 py-2 text-sm text-foreground">
                  <option value="">{t("screener.factor.select_portfolio")}</option>
                  {(portfolios.data ?? []).map((item) => <option key={item.id} value={item.id}>{item.name} ({item.currency})</option>)}
                </select>
              </label>
              <label className="text-xs text-muted-foreground">
                {t("screener.factor.additional_cash_twd")}
                <input type="number" min={0} step={10000} value={additionalCashTwd} onChange={(event) => setAdditionalCashTwd(Number(event.target.value))} className="mt-1 block w-36 rounded border border-border bg-background px-3 py-2 text-sm text-foreground" />
              </label>
              <label className="text-xs text-muted-foreground">
                {t("screener.factor.fee_bps")}
                <input type="number" min={0} max={100} step={0.25} value={feeBps} onChange={(event) => setFeeBps(Number(event.target.value))} className="mt-1 block w-24 rounded border border-border bg-background px-3 py-2 text-sm text-foreground" />
              </label>
              <label className="text-xs text-muted-foreground">
                {t("screener.factor.slippage_bps")}
                <input type="number" min={0} max={500} value={slippageBps} onChange={(event) => setSlippageBps(Number(event.target.value))} className="mt-1 block w-24 rounded border border-border bg-background px-3 py-2 text-sm text-foreground" />
              </label>
              <label className="flex items-center gap-2 pb-2 text-xs text-muted-foreground">
                <input type="checkbox" checked={allowOddLot} onChange={(event) => setAllowOddLot(event.target.checked)} />
                {t("screener.factor.allow_odd_lot")}
              </label>
              <button type="button" onClick={() => rebalancePreview.mutate()} disabled={!selectedPortfolioId || rebalancePreview.isPending} className="rounded bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50">
                {rebalancePreview.isPending ? t("common.loading") : t("screener.factor.preview_rebalance")}
              </button>
            </div>
            {rebalancePreview.isError && <p className="text-xs text-danger">{t("screener.factor.rebalance_failed")}</p>}
            {rebalancePreview.data && (
              <div className="space-y-3">
                {!rebalancePreview.data.summary.funded && <p className="rounded border border-danger/30 bg-danger/5 p-3 text-sm text-danger">{t("screener.factor.funding_shortfall", { amount: rebalancePreview.data.summary.funding_shortfall_twd.toLocaleString() })}</p>}
                <div className="grid grid-cols-2 gap-2 lg:grid-cols-6">
                  {([
                    ["rebalance_notional", rebalancePreview.data.portfolio_notional_twd.toLocaleString()],
                    ["ledger_cash_twd", rebalancePreview.data.ledger_cash_twd.toLocaleString()],
                    ["trade_count", rebalancePreview.data.summary.trade_count],
                    ["gross_turnover_twd", rebalancePreview.data.summary.gross_turnover_twd.toLocaleString()],
                    ["estimated_total_cost", rebalancePreview.data.summary.estimated_total_cost_twd.toLocaleString()],
                    ["estimated_cost_bps", rebalancePreview.data.summary.estimated_cost_bps.toFixed(2)],
                    ["ending_cash_twd", rebalancePreview.data.summary.ending_cash_twd.toLocaleString()],
                  ] as const).map(([key, value]) => <div key={key} className="rounded bg-secondary/30 p-2"><div className="text-xs text-muted-foreground">{t(`screener.factor.${key}`)}</div><div className="mt-1 text-sm font-semibold text-foreground">{value}</div></div>)}
                </div>
                <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
                  {([
                    ["pre_trade_volatility", rebalancePreview.data.target_portfolio.risk_comparison.pre_trade_annual_volatility],
                    ["post_trade_volatility", rebalancePreview.data.target_portfolio.risk_comparison.post_trade_annual_volatility],
                    ["pre_trade_tracking_error", rebalancePreview.data.target_portfolio.risk_comparison.pre_trade_tracking_error],
                    ["post_trade_tracking_error", rebalancePreview.data.target_portfolio.risk_comparison.post_trade_tracking_error],
                  ] as const).map(([key, value]) => <div key={key} className="rounded border border-border p-2"><div className="text-xs text-muted-foreground">{t(`screener.factor.${key}`)}</div><div className="mt-1 text-sm font-medium text-foreground">{value == null ? "—" : unsignedPct(value * 100)}</div></div>)}
                </div>
                <div className="flex flex-wrap gap-2 text-xs text-muted-foreground">
                  {rebalancePreview.data.cost_scenarios.map((scenario) => <span key={scenario.name} className="rounded border border-border px-2 py-1">{t(`screener.factor.cost_${scenario.name}`)} TWD {scenario.estimated_cost_twd.toLocaleString()}</span>)}
                </div>
                {rebalancePreview.data.trades.length > 0 && <div className="overflow-x-auto rounded border border-border"><table className="w-full text-xs"><thead><tr className="bg-secondary/20 text-muted-foreground"><th className="p-2 text-left">{t("market.table.symbol")}</th><th className="p-2 text-left">{t("screener.factor.trade_side")}</th><th className="p-2 text-right">{t("screener.factor.quantity")}</th><th className="p-2 text-right">{t("screener.factor.execution_price")}</th><th className="p-2 text-right">{t("screener.factor.gross_value")}</th><th className="p-2 text-right">{t("screener.factor.fee_tax")}</th><th className="p-2 text-right">{t("screener.factor.total_cost")}</th></tr></thead><tbody>{rebalancePreview.data.trades.map((trade) => <tr key={`${trade.symbol}-${trade.side}`} className="border-t border-border/60"><td className="p-2 font-medium text-primary">{trade.symbol}</td><td className={`p-2 ${trade.side === "buy" ? "text-up" : "text-down"}`}>{t(`screener.factor.trade_${trade.side}`)}</td><td className="p-2 text-right">{trade.quantity.toLocaleString()}</td><td className="p-2 text-right">{trade.execution_price_twd.toLocaleString()}</td><td className="p-2 text-right">{trade.gross_value_twd.toLocaleString()}</td><td className="p-2 text-right">{(trade.fee_twd + trade.tax_twd).toLocaleString()}</td><td className="p-2 text-right">{trade.total_cost_twd.toLocaleString()}</td></tr>)}</tbody></table></div>}
                {rebalancePreview.data.frozen.length > 0 && <p className="text-xs text-warning">{t("screener.factor.frozen_non_tw", { symbols: rebalancePreview.data.frozen.map((item) => item.symbol).join(", ") })}</p>}
                <p className="text-xs text-muted-foreground">{t("screener.factor.preview_only_notice")}</p>
              </div>
            )}
          </div>
        </div>

        {validation.isError && <p className="text-sm text-danger">{t("screener.factor.validation_failed")}</p>}
        {validation.data && (
          <div className="space-y-3">
            <QualityBanner quality={validation.data.quality} />
            <div className="grid grid-cols-2 lg:grid-cols-5 xl:grid-cols-10 gap-2">
              {([
                ["period_count", validation.data.summary.period_count],
                ["cumulative_return", pct(validation.data.summary.cumulative_return_pct)],
                ["average_return", pct(validation.data.summary.average_period_return_pct)],
                ["average_excess", pct(validation.data.summary.average_excess_return_pct)],
                ["information_ratio", validation.data.summary.annualized_information_ratio?.toFixed(2) ?? "—"],
                ["excess_t_stat", validation.data.summary.excess_return_t_stat?.toFixed(2) ?? "—"],
                ["positive_excess_rate", pct(validation.data.summary.positive_excess_rate_pct)],
                ["max_drawdown", pct(validation.data.summary.max_drawdown_pct)],
                ["turnover", pct(validation.data.summary.average_turnover_pct)],
                ["average_fill", pct(validation.data.summary.average_fill_pct)],
                ["impact_cost", pct(validation.data.summary.average_impact_cost_pct)],
                ["blocked_trades", validation.data.summary.blocked_trade_count],
              ] as const).map(([key, value]) => (
                <div key={key} className="rounded border border-border bg-background p-3">
                  <div className="text-xs text-muted-foreground">{t(`screener.factor.${key}`)}</div>
                  <div className="mt-1 font-semibold text-foreground">{value}</div>
                </div>
              ))}
            </div>
            <div className="rounded border border-border bg-background p-3 text-sm">
              <span className="text-muted-foreground">{t("screener.factor.excess_confidence_interval")}: </span>
              <span className="font-medium text-foreground">
                {pct(validation.data.summary.excess_return_ci_low_pct)} – {pct(validation.data.summary.excess_return_ci_high_pct)}
              </span>
            </div>
            <div className="space-y-2 rounded border border-border bg-background p-3">
              <div>
                <h3 className="text-sm font-semibold text-foreground">{t("screener.factor.weight_stability")}</h3>
                <p className="text-xs text-muted-foreground">{t("screener.factor.weight_stability_desc")}</p>
              </div>
              <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
                {([
                  ["adaptive_periods", validation.data.weight_stability.adaptive_period_count],
                  ["fallback_periods", validation.data.weight_stability.fallback_period_count],
                  ["average_weight_turnover", pct(validation.data.weight_stability.average_weight_turnover_pct)],
                  ["maximum_weight_turnover", pct(validation.data.weight_stability.maximum_weight_turnover_pct)],
                ] as const).map(([key, value]) => (
                  <div key={key} className="rounded bg-secondary/30 p-2">
                    <div className="text-xs text-muted-foreground">{t(`screener.factor.${key}`)}</div>
                    <div className="mt-1 text-sm font-medium text-foreground">{value}</div>
                  </div>
                ))}
              </div>
              <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                {FACTORS.map((factor) => {
                  const range = validation.data.weight_stability.factor_ranges[factor];
                  return (
                    <div key={factor} className="rounded border border-border/60 p-2 text-xs">
                      <div className="font-medium text-foreground">{t(`screener.factor.${factor}`)}</div>
                      <div className="mt-1 text-muted-foreground">
                        {t("screener.factor.latest_weight")} {pct(range?.latest == null ? null : range.latest * 100)} · {t("screener.factor.weight_range")} {pct(range?.minimum == null ? null : range.minimum * 100)}–{pct(range?.maximum == null ? null : range.maximum * 100)}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
              {(["bull", "bear", "high_volatility", "low_volatility"] as const).map((regime) => {
                const item = validation.data.regime_analysis[regime];
                return (
                  <div key={regime} className="rounded border border-border bg-background p-3">
                    <div className="text-xs text-muted-foreground">{t(`screener.factor.regime_${regime}`)} · {item?.period_count ?? 0}</div>
                    <div className="mt-1 font-semibold text-foreground">{pct(item?.average_excess_return_pct)}</div>
                  </div>
                );
              })}
            </div>
            <div className="space-y-2 rounded border border-border bg-background p-3">
              <div>
                <h3 className="text-sm font-semibold text-foreground">{t("screener.factor.signal_diagnostics")}</h3>
                <p className="text-xs text-muted-foreground">{t("screener.factor.signal_diagnostics_desc")}</p>
              </div>
              <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                {DIAGNOSTIC_SIGNALS.map((signal) => {
                  const item = validation.data.factor_diagnostics[signal];
                  return (
                    <div key={signal} className={`rounded border p-3 ${item?.significant_after_holm_5pct ? "border-success/40 bg-success/5" : "border-border"}`}>
                      <div className="flex items-center justify-between gap-2 text-xs text-muted-foreground">
                        <span>{t(`screener.factor.${signal}`)}</span>
                        <span>n={item?.period_count ?? 0}</span>
                      </div>
                      <div className="mt-1 font-semibold text-foreground">IC {zScore(item?.average_rank_ic)}</div>
                      <div className="mt-1 text-xs text-muted-foreground">
                        ICIR {zScore(item?.annualized_ic_ir)} · Holm p {item?.holm_adjusted_p_value?.toFixed(3) ?? "—"}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
            <div className="grid gap-3 lg:grid-cols-[1fr_1.4fr]">
              <div className="rounded border border-border bg-background p-3">
                <h3 className="text-sm font-semibold text-foreground">{t("screener.factor.quantile_returns")}</h3>
                <div className="mt-3 grid grid-cols-5 gap-2">
                  {validation.data.quantile_analysis.average_returns_pct.map((value, index) => (
                    <div key={index} className="rounded bg-secondary/30 p-2 text-center">
                      <div className="text-xs text-muted-foreground">Q{index + 1}</div>
                      <div className={`mt-1 text-sm font-medium ${(value ?? 0) >= 0 ? "text-up" : "text-down"}`}>{pct(value)}</div>
                    </div>
                  ))}
                </div>
                <p className="mt-3 text-xs text-muted-foreground">
                  {t("screener.factor.quantile_spread")}: {pct(validation.data.quantile_analysis.average_top_bottom_spread_pct)} · {t("screener.factor.positive_spread_rate")}: {pct(validation.data.quantile_analysis.positive_spread_rate_pct)}
                </p>
              </div>
              <div className="overflow-x-auto rounded border border-border bg-background p-3">
                <h3 className="text-sm font-semibold text-foreground">{t("screener.factor.factor_correlation")}</h3>
                <table className="mt-2 w-full text-xs">
                  <thead><tr><th className="p-1 text-left" />{FACTORS.map((factor) => <th key={factor} className="p-1 text-right text-muted-foreground">{t(`screener.factor.${factor}`)}</th>)}</tr></thead>
                  <tbody>{FACTORS.map((left) => (
                    <tr key={left} className="border-t border-border/50">
                      <th className="p-1 text-left font-normal text-muted-foreground">{t(`screener.factor.${left}`)}</th>
                      {FACTORS.map((right) => <td key={right} className="p-1 text-right text-foreground">{zScore(validation.data.factor_correlation_matrix[left]?.[right])}</td>)}
                    </tr>
                  ))}</tbody>
                </table>
              </div>
            </div>
            <div className="grid gap-3 lg:grid-cols-2">
              <div className="rounded border border-border bg-background p-3">
                <h3 className="text-sm font-semibold text-foreground">{t("screener.factor.holding_sensitivity")}</h3>
                <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-4">
                  {Object.entries(validation.data.sensitivity_analysis.holding_sessions).map(([sessions, item]) => (
                    <div key={sessions} className="rounded bg-secondary/30 p-2">
                      <div className="text-xs text-muted-foreground">{sessions} {t("screener.factor.sessions")}</div>
                      <div className="mt-1 text-sm font-medium text-foreground">IC {zScore(item.average_rank_ic)}</div>
                      <div className="text-xs text-muted-foreground">Q5−Q1 {pct(item.average_top_bottom_spread_pct)}</div>
                    </div>
                  ))}
                </div>
              </div>
              <div className="rounded border border-border bg-background p-3">
                <h3 className="text-sm font-semibold text-foreground">{t("screener.factor.breadth_sensitivity")}</h3>
                <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-4">
                  {Object.entries(validation.data.sensitivity_analysis.top_n).map(([size, item]) => (
                    <div key={size} className="rounded bg-secondary/30 p-2">
                      <div className="text-xs text-muted-foreground">Top {size}</div>
                      <div className="mt-1 text-sm font-medium text-foreground">{pct(item.average_forward_return_pct)}</div>
                      <div className="text-xs text-muted-foreground">n={item.period_count}</div>
                    </div>
                  ))}
                </div>
              </div>
            </div>
            <div className="overflow-x-auto rounded border border-border bg-background p-3">
              <h3 className="text-sm font-semibold text-foreground">{t("screener.factor.factor_decay")}</h3>
              <p className="text-xs text-muted-foreground">{t("screener.factor.factor_decay_desc")}</p>
              <table className="mt-2 w-full text-xs">
                <thead><tr><th className="p-1 text-left" />{["5", "21", "63"].map((horizon) => <th key={horizon} className="p-1 text-right text-muted-foreground">{horizon} {t("screener.factor.sessions")}</th>)}<th className="p-1 text-right text-muted-foreground">{t("screener.factor.peak_horizon")}</th></tr></thead>
                <tbody>{DIAGNOSTIC_SIGNALS.map((signal) => {
                  const decay = validation.data.factor_decay_analysis[signal];
                  return (
                    <tr key={signal} className="border-t border-border/50">
                      <th className="p-1 text-left font-normal text-muted-foreground">{t(`screener.factor.${signal}`)}</th>
                      {["5", "21", "63"].map((horizon) => <td key={horizon} className="p-1 text-right text-foreground">{zScore(decay?.average_rank_ic_by_horizon[horizon])}</td>)}
                      <td className="p-1 text-right text-foreground">{decay?.peak_absolute_ic_horizon ?? "—"}</td>
                    </tr>
                  );
                })}</tbody>
              </table>
            </div>
            {validation.data.periods.length > 0 && (
              <DataTable
                columns={[
                  { key: "anchor", header: t("screener.factor.anchor"), render: (row: ValidationPeriod) => row.anchor },
                  { key: "holdings", header: t("screener.factor.holdings"), numeric: true, render: (row: ValidationPeriod) => row.holding_count },
                  { key: "net", header: t("screener.factor.net_return"), numeric: true, render: (row: ValidationPeriod) => <span className={row.net_return_pct >= 0 ? "text-up" : "text-down"}>{pct(row.net_return_pct)}</span> },
                  { key: "benchmark", header: t("screener.factor.benchmark_return"), numeric: true, render: (row: ValidationPeriod) => pct(row.benchmark_return_pct) },
                  { key: "excess", header: t("screener.factor.excess_return"), numeric: true, render: (row: ValidationPeriod) => <span className={row.excess_return_pct >= 0 ? "text-up" : "text-down"}>{pct(row.excess_return_pct)}</span> },
                  { key: "regime", header: t("screener.factor.market_regime"), render: (row: ValidationPeriod) => row.market_regime ? t(`screener.factor.regime_${row.market_regime}`) : "—" },
                  { key: "turnover", header: t("screener.factor.turnover"), numeric: true, render: (row: ValidationPeriod) => `${(row.turnover * 100).toFixed(1)}%` },
                  { key: "fill", header: t("screener.factor.average_fill"), numeric: true, render: (row: ValidationPeriod) => pct(row.average_fill_pct) },
                  { key: "impact", header: t("screener.factor.impact_cost"), numeric: true, render: (row: ValidationPeriod) => pct(row.impact_cost_pct) },
                ]}
                rows={[...validation.data.periods].reverse()}
                rowKey={(row) => row.anchor}
                mobileMode="scroll"
                aria-label={t("screener.factor.validation_title")}
              />
            )}
            {!validation.data.periods.length && <p className="text-sm text-muted-foreground">{t("screener.factor.validation_no_data")}</p>}
          </div>
        )}
      </div>
    </div>
  );
}
