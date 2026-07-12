/**
 * Strategy-template API fetchers + their request/response shapes:
 * template CRUD, the walk-forward orchestrator, Brier history, weight
 * learning, and the strategy-level aggregate rollup (which reuses the
 * `SweepAggregate` shape from `sweeps.ts`).
 */
import { api } from "./_shared";
import type { DiscussionMarket } from "@/types/discussion";
import type { SweepAggregate } from "./sweeps";

// ── Strategy templates (PR-A) ────────────────────────────────────

export interface StrategyTemplate {
  id: string;
  name: string;
  description: string | null;
  topic: string;
  rules: string;
  market: DiscussionMarket;
  persona_ids: string[];
  default_rounds: number;
  default_concurrency: number;
  default_auto_post_mortem: boolean;
  /** PR-C: persona_id -> learned weight. Empty = uniform. */
  persona_weights: Record<string, number>;
  weights_updated_at: string | null;
  /** PR-D: auto-schedule fields. Disabled by default. */
  auto_schedule_enabled: boolean;
  auto_schedule_cadence_hours: number;
  auto_schedule_anchor_offset_days: number;
  auto_schedule_trading_days_count: number;
  auto_schedule_last_run_at: string | null;
  /** PR-4a: lifecycle tier — 'cold_start' | 'learning' | 'mature'
   * | 'drifting' | 'stale'. Defaults to 'cold_start' on a fresh
   * row; the sweep Phase 3 hook updates it after each completed
   * sweep. The UI surfaces this as a colored badge so the operator
   * sees "is this strategy still trustworthy" at a glance.
   * Optional for forward-compat with rows from before PR-4a. */
  maturity_tier?: MaturityTier;
  maturity_computed_at?: string | null;
  /** PR-4b: walk-forward auto-promote knobs. Disabled by default
   * (operators opt in per strategy). When enabled, the
   * walk-forward orchestrator's Phase 4 auto-deploys the test
   * fold's OOS-validated weights to the live `persona_weights`
   * column when both KPI thresholds pass. */
  auto_promote_enabled?: boolean;
  auto_promote_min_oos_brier_improvement?: number;
  auto_promote_min_oos_hit_rate?: number;
  /** PR-4c: per-persona status map. Personas absent from the map
   * default to 'active'. Keys are persona IDs; values are one of
   * 'active' / 'frozen' / 'shadow'. Frozen personas are dropped
   * from the round roster; shadow personas run but their output
   * is excluded from the synthesizer (B5 placeholder). */
  persona_status?: Record<string, "active" | "frozen" | "shadow">;
  persona_status_updated_at?: string | null;
  created_at: string;
  updated_at: string;
  deleted_at: string | null;
}

/** PR-4b: one row per (strategy_id, snapshot_date) from the daily
 * health monitor cron. NULL fields = window had no resolved
 * discussions on that day (cold-start / quiet weekend). */
export interface StrategyHealthSnapshot {
  strategy_id: string;
  snapshot_date: string;
  brier_30d: number | null;
  calibrated_brier_30d: number | null;
  hit_rate_30d: number | null;
  sample_count_30d: number;
  lesson_hit_rate_30d: number | null;
  maturity_tier_at_snapshot: string | null;
  status_flags: string[];
  computed_at: string | null;
}

/** PR-4a: five-tier strategy lifecycle classifier. */
export type MaturityTier =
  | "cold_start"
  | "learning"
  | "mature"
  | "drifting"
  | "stale";

/** PR-4a: signal payload returned by `GET /strategies/{id}/maturity`
 * — the inputs the rule engine used to land on the tier. Lets the
 * UI render "why this tier" tooltip without a second round-trip. */
export interface MaturitySignals {
  production_sweep_count?: number;
  latest_sweep_completed_at?: string | null;
  calibration_sample_count?: number | null;
  has_calibration_curve?: boolean;
  recent_brier?: number | null;
  baseline_brier?: number | null;
  brier_ratio?: number | null;
}

/** PR-4a: strategy version history row. */
export interface StrategyVersionRow {
  id: string;
  strategy_id: string;
  version_number: number;
  artifact_kind: "weights" | "calibration_curve";
  payload: unknown;
  sample_count: number | null;
  source_sweep_id: string | null;
  fit_at: string | null;
  status: "active" | "superseded" | "rolled_back";
  notes: string | null;
}

/** PR-5a: assembled lifecycle timeline payload from
 * `GET /strategies/{id}/timeline`. */
export interface TimelineMetricPoint {
  date: string;
  brier_30d: number | null;
  calibrated_brier_30d: number | null;
  hit_rate_30d: number | null;
  sample_count: number;
  lesson_hit_rate_30d: number | null;
  maturity_tier: string | null;
  status_flags: string[];
}

export type TimelineEventKind =
  | "version_change"
  | "sweep_completed"
  | "maturity_change"
  | "persona_status_change";

export interface TimelineEvent {
  date: string;
  kind: TimelineEventKind;
  /** version_change only */
  artifact?: "weights" | "calibration_curve";
  version_number?: number;
  status?: string;
  sample_count?: number | null;
  source_sweep_id?: string | null;
  trigger?: string;
  notes?: string | null;
  /** sweep_completed only */
  sweep_id?: string;
  fold_kind?: string;
  anchor_date?: string | null;
  discussions_completed?: number;
  discussions_failed?: number;
  rounds_per_discussion?: number;
  /** maturity_change only */
  from?: string;
  to?: string;
  /** persona_status_change only */
  non_active_personas?: string[];
  non_active_count?: number;
}

export interface StrategyTimeline {
  strategy_id: string;
  days: number;
  metrics: TimelineMetricPoint[];
  events: TimelineEvent[];
}

/** PR-5b: leaderboard row from `GET /personas/leaderboard`. */
export interface PersonaLeaderboardRow {
  persona_id: string;
  participation_count: number;
  win_attribution_count: number;
  win_attribution_rate: number | null;
  /** Only populated when the leaderboard is scoped by strategy_id. */
  average_weight: number | null;
  weight_trend_30d: number | null;
  frozen_in_strategies: number;
  shadow_in_strategies: number;
}

export interface PersonaLeaderboardResponse {
  owner_id: string;
  strategy_id: string | null;
  market: string | null;
  days: number;
  items: PersonaLeaderboardRow[];
}

/** PR-5b: lesson library row. Mirrors the backend `lesson_to_dict`
 * extended for PR-5b. */
export interface LessonLibraryRow {
  id: number;
  discussion_id: string;
  market: string;
  as_of_date: string;
  category: string;
  lesson_text: string;
  related_symbols: string[];
  missed_winners: string[];
  tier: "episodic" | "semantic" | "structural" | null;
  regime: string | null;
  usage_count: number;
  hit_count: number;
  recent_hit_rate_10: number | null;
  last_used_at: string | null;
  promoted_at: string | null;
  demoted_at: string | null;
  archived_at: string | null;
  created_at: string | null;
}

export interface LessonLibraryResponse {
  items: LessonLibraryRow[];
  total: number;
  limit: number;
  offset: number;
}

/** PR-5c: regime overlay band on the timeline chart. */
export interface RegimeBand {
  start: string;
  end: string;
  regime: "bull" | "bear" | "high_vol" | "low_vol";
}

export interface RegimeBandsResponse {
  market: string;
  days: number;
  start: string;
  end: string;
  bands: RegimeBand[];
}

/** PR-5c: cross-strategy comparison response. */
export interface StrategyCompareSummary {
  brier_avg: number | null;
  brier_latest: number | null;
  hit_rate_avg: number | null;
  hit_rate_latest: number | null;
  sample_total: number;
}

export interface StrategyCompareEntry {
  strategy_id: string;
  name: string;
  market: string;
  maturity_tier: string | null;
  metrics: Array<{
    date: string;
    brier_30d: number | null;
    calibrated_brier_30d: number | null;
    hit_rate_30d: number | null;
    sample_count: number;
  }>;
  summary: StrategyCompareSummary;
}

export interface StrategyCompareResponse {
  days: number;
  strategies: StrategyCompareEntry[];
}

/** PR-5c: per-conclusion baseline delta. */
export interface ConclusionBaselineDelta {
  brier_baseline: number | null;
  consensus_baseline: number | null;
  consensus_score: number;
  consensus_pct_change: number | null;
  verify_pending: boolean;
}

export interface CreateStrategyInput {
  name: string;
  description?: string | null;
  topic: string;
  rules: string;
  market: DiscussionMarket;
  persona_ids: string[];
  default_rounds?: number;
  default_concurrency?: number;
  default_auto_post_mortem?: boolean;
  auto_schedule_enabled?: boolean;
  auto_schedule_cadence_hours?: number;
  auto_schedule_anchor_offset_days?: number;
  auto_schedule_trading_days_count?: number;
}

export type UpdateStrategyInput = Partial<CreateStrategyInput>;


// ── Walk-forward orchestrator (PR-A1 follow-up #341) ──────────


export interface WalkForwardRequest {
  /** ISO YYYY-MM-DD — most-recent day the test fold should cover.
   *  The orchestrator walks back N folds from here. */
  anchor_date: string;
  train_window_days?: number;   // default 60
  test_window_days?: number;    // default 20
  n_folds?: number;             // default 2, max 6
  rounds_per_discussion?: number;
  concurrency?: number;
  auto_post_mortem?: boolean;
}

export interface WalkForwardFold {
  fold_index: number;
  train_anchor: string;
  train_dates: string[];
  test_anchor: string;
  test_dates: string[];
}

export interface WalkForwardPlanResponse {
  strategy_id: string;
  market: string;
  train_window_days: number;
  test_window_days: number;
  folds: WalkForwardFold[];
  /** True when the background orchestrator was scheduled. The
   *  actual sweep rows appear via `GET /sweeps?strategy_id=...`
   *  as the worker creates them — poll there to follow progress. */
  started: boolean;
}

export async function triggerWalkForward(
  strategyId: string,
  body: WalkForwardRequest,
): Promise<WalkForwardPlanResponse> {
  const r = await api.post<WalkForwardPlanResponse>(
    `/discussion/strategies/${strategyId}/walk-forward`,
    body,
  );
  return r.data;
}


export async function fetchStrategies(): Promise<StrategyTemplate[]> {
  const r = await api.get<StrategyTemplate[]>("/discussion/strategies");
  return r.data;
}

export async function createStrategy(
  body: CreateStrategyInput,
): Promise<StrategyTemplate> {
  const r = await api.post<StrategyTemplate>("/discussion/strategies", body);
  return r.data;
}

export async function updateStrategy(
  id: string, body: UpdateStrategyInput,
): Promise<StrategyTemplate> {
  const r = await api.patch<StrategyTemplate>(
    `/discussion/strategies/${id}`, body,
  );
  return r.data;
}

export async function deleteStrategy(id: string): Promise<void> {
  await api.delete(`/discussion/strategies/${id}`);
}

// ── Strategy Brier history (PR-B) ────────────────────────────────

export interface BrierHistoryPoint {
  sweep_id: string;
  /** ISO YYYY-MM-DD — the trading-day anchor of the sweep,
   *  not when the sweep ran. Useful as the chart's X-axis label
   *  because a single sweep covers N days but visually we want
   *  one bar per fold + per anchor. */
  anchor_date: string | null;
  /** ISO timestamp — when the sweep finished. The trend chart
   *  orders points by this so a backfilled sweep doesn't reorder
   *  the line. */
  completed_at: string | null;
  fold_kind: "train" | "test" | "production" | null;
  /** Sample-weighted mean Brier across the sweep's resolved
   *  discussions. NULL on sweeps that pre-dated PR-C1's brier
   *  computation. */
  raw_brier: number | null;
  /** Same but computed against `calibrated_confidence` —
   *  comparing against `raw_brier` is the "is the curve
   *  helping?" diagnostic. NULL when partial coverage. */
  calibrated_brier: number | null;
  samples: number;
}

export async function fetchStrategyBrierHistory(
  templateId: string,
  windowDays: number = 90,
): Promise<BrierHistoryPoint[]> {
  const r = await api.get<BrierHistoryPoint[]>(
    `/discussion/strategies/${templateId}/brier-history`,
    { params: { window_days: windowDays } },
  );
  return r.data;
}

export interface PersonaWeightLearnResult {
  updated: boolean;
  reason: string | null;
  weights: Record<string, number>;
  samples: Record<string, number>;
}

export async function learnStrategyWeights(
  templateId: string,
): Promise<PersonaWeightLearnResult> {
  const r = await api.post<PersonaWeightLearnResult>(
    `/discussion/strategies/${templateId}/learn`,
  );
  return r.data;
}

export async function fetchStrategyAggregate(
  templateId: string,
): Promise<SweepAggregate> {
  const r = await api.get<SweepAggregate>(
    `/discussion/strategies/${templateId}/aggregate`,
  );
  return r.data;
}
