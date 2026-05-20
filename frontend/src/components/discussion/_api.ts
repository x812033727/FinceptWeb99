/**
 * API fetchers + their request/response shapes for the discussion
 * subsystem. All HTTP calls live here; UI helpers / formatters /
 * localStorage live in sibling modules. Imported by `_helpers.tsx`
 * (re-export shim) so existing call sites keep working.
 */
import api from "@/lib/api";
import type {
  AgentInfo,
  AutoRunConfig,
  Conclusion,
  Discussion,
  DiscussionDetail,
  DiscussionMarket,
  RoundContextSnapshot,
  ScoreboardResponse,
  Turn,
} from "@/types/discussion";

// ── core session CRUD ────────────────────────────────────────────

export async function fetchAgents(): Promise<AgentInfo[]> {
  const res = await api.get<AgentInfo[]>("/ai/agents");
  return res.data;
}

export async function fetchSessions(): Promise<Discussion[]> {
  const res = await api.get<Discussion[]>("/discussion/sessions");
  return res.data;
}

export async function fetchSession(id: string): Promise<DiscussionDetail> {
  const res = await api.get<DiscussionDetail>(`/discussion/sessions/${id}`);
  return res.data;
}

export async function createSession(body: {
  topic: string;
  rules: string;
  persona_ids: string[];
  market?: string;
  /** Backtest anchor (PR #224). ISO date string ("2025-01-15") or
   * undefined for live mode. */
  as_of_date?: string;
}): Promise<Discussion> {
  const res = await api.post<Discussion>("/discussion/sessions", body);
  return res.data;
}

export async function updateSession(
  id: string,
  body: {
    topic?: string;
    rules?: string;
    persona_ids?: string[];
    market?: string;
  },
): Promise<Discussion> {
  const res = await api.patch<Discussion>(`/discussion/sessions/${id}`, body);
  return res.data;
}

export async function deleteSession(id: string): Promise<void> {
  await api.delete(`/discussion/sessions/${id}`);
}

export async function injectUserMessage(
  id: string, content: string,
): Promise<Turn> {
  const res = await api.post<Turn>(
    `/discussion/sessions/${id}/inject`, { content },
  );
  return res.data;
}

export async function concludeSession(id: string): Promise<{ conclusion: Conclusion }> {
  const res = await api.post<{ conclusion: Conclusion }>(
    `/discussion/sessions/${id}/conclude`,
  );
  return res.data;
}

// ── post-mortem (PR #273) ─────────────────────────────────────────

export interface PostMortemGainer {
  symbol: string;
  change_pct: number;
  close: number;
  base_close: number;
  /** PR #273: which trading day this gainer is for. Older
   *  payloads (D1-only) populated only the flat `top_gainers`
   *  field; the optional marker is "" / undefined for those. */
  trading_day?: string;
}

export interface PostMortemDailyGainers {
  trading_day: string;
  gainers: PostMortemGainer[];
}

export interface PostMortemDayPerformance {
  trading_day: string;
  close: number;
  /** Cumulative since as_of (entry-day) close. */
  change_pct: number;
}

export interface PostMortemRecommendedPerformance {
  symbol: string;
  /** Close on as_of_date — the entry price we're comparing against. */
  base_close: number;
  days: PostMortemDayPerformance[];
}

export interface PostMortemWinner {
  symbol: string;
  peak_pct: number;
  peak_day: string;
}

export interface PostMortemVerdict {
  status: "win" | "miss" | "insufficient_data";
  threshold_pct: number;
  window_days: number;
  winners: PostMortemWinner[];
  best_pct: number | null;
  reason: string;
}

export interface PostMortemResponse {
  /** PR #273: D1-D5 trading-day window. Older backends omit the
   *  field; frontend treats it as `[next_trading_day]` for the
   *  single-day legacy view. */
  trading_days?: string[];
  /** PR #273: per-recommendation D1-D5 self-eval. Empty array
   *  when there are no recommendations or the window is too thin. */
  recommended_performance?: PostMortemRecommendedPerformance[];
  /** PR #273: per-day top-N gainers across the window. Empty
   *  array when the archive doesn't reach D1. */
  daily_top_gainers?: PostMortemDailyGainers[];

  // Back-compat aliases — populated from D1's leaderboard so older
  // clients keep working unchanged.
  next_trading_day: string;
  top_gainers: PostMortemGainer[];

  /** Learning-loop addition: "ran" when the personas were asked to
   * critique, "skipped" when the recommendation already cleared the
   * win threshold so no critique round was fired. Older backends
   * omit; frontend treats undefined as "ran" for back-compat. */
  status?: "ran" | "skipped";
  verdict?: PostMortemVerdict | null;
  /** Null when status === "skipped" — no turn was injected. */
  injected_turn_id: number | null;
}

/** Triggers the post-mortem self-critique injection. Backtest mode +
 *  has-conclusion only; backend returns 400 otherwise. After this
 *  resolves, caller is expected to run a new round (so personas
 *  react) and then re-conclude (so the conclusion incorporates the
 *  review). The 3-step chain is the user-facing 「事後檢討」 flow. */
export async function runPostMortem(id: string): Promise<PostMortemResponse> {
  const res = await api.post<PostMortemResponse>(
    `/discussion/sessions/${id}/post-mortem`,
  );
  return res.data;
}

// ── Backtest sweep (PR #274) ─────────────────────────────────────

export interface BacktestSweepFailedDate {
  date: string;
  error: string;
}

export type BacktestSweepStatus =
  | "pending"
  | "running"
  | "completed"
  | "cancelled"
  | "failed";

export interface BacktestSweep {
  id: string;
  status: BacktestSweepStatus;
  topic: string;
  rules: string;
  market: DiscussionMarket;
  persona_ids: string[];
  anchor_date: string;
  trading_days_count: number;
  rounds_per_discussion: number;
  concurrency: number;
  /** PR #275: auto-trigger the post-mortem self-critique after each
   *  spawned discussion's conclude. Older backends omit; treat as
   *  true since that's the new default. */
  auto_post_mortem?: boolean;
  /** PR-A: source strategy template, if the sweep was launched
   *  from one. Lets the dashboard group sweeps per template. */
  strategy_id?: string | null;
  resolved_dates: string[];
  completed_dates: string[];
  failed_dates: BacktestSweepFailedDate[];
  error_message: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  cancelled_at: string | null;
}

export interface CreateBacktestSweepInput {
  /** PR-A: when set, server back-fills any unspecified field from
   *  the template. Submit just {strategy_id, anchor_date,
   *  trading_days_count} for a load-and-go flow. */
  strategy_id?: string;
  topic?: string;
  rules?: string;
  market?: DiscussionMarket;
  persona_ids?: string[];
  anchor_date: string;
  trading_days_count: number;
  rounds_per_discussion?: number;
  concurrency?: number;
  /** Defaults to true on the backend; sent explicitly so the
   *  toggle's off-state is respected. */
  auto_post_mortem?: boolean;
}

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

// ── Sweep / strategy aggregates (PR-B) ───────────────────────────

export interface AggregatePersona {
  persona_id: string;
  discussions_count: number;
  win_count: number;
  hit_rate: number | null;
  agree_turn_count: number;
  dissent_turn_count: number;
}

export interface AggregateLesson {
  category: string;
  lesson_text: string;
  as_of_date: string;
  related_symbols: string[];
  created_at: string | null;
}

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


export interface ReliabilityBucket {
  bucket_lower: number;
  bucket_upper: number;
  /** Mean of the raw confidences that landed in this bucket. NULL
   *  when the bucket has zero samples — the chart should render
   *  it as a grey gap so the diagram stays continuous. */
  mean_confidence: number | null;
  /** Observed positive rate (outcome=1) for this bucket. NULL when
   *  the bucket is empty. Perfect calibration: hit_rate ≈ bucket
   *  midpoint across the row. */
  hit_rate: number | null;
  count: number;
}

export interface SweepAggregate {
  scope: "sweep" | "strategy";
  sweep_id?: string | null;
  strategy_id?: string | null;
  sweep_count?: number | null;
  anchor_date?: string | null;
  trading_days_count?: number | null;
  completed_count?: number | null;
  failed_count?: number | null;
  /** PR-A0 walk-forward fold metadata. Only present on
   *  scope="sweep" responses; strategy-level aggregates roll up
   *  across all fold kinds and don't expose this field. */
  fold_kind?: "train" | "test" | "production" | null;
  parent_sweep_id?: string | null;
  discussions_total: number;
  verdict_counts: {
    win: number; loss: number; unverifiable: number; pending: number;
  };
  win_rate: number | null;
  avg_pnl_pct: (number | null)[];
  /** PR-C1 sample-weighted Brier over raw synthesizer
   *  confidence. NULL when no resolved discussion contributed. */
  brier_score?: number | null;
  brier_samples?: number;
  /** PR-C2 follow-up: parallel Brier over post-curve calibrated
   *  confidence. NULL when no discussion had complete
   *  calibration coverage; comparing against `brier_score` is
   *  the "is the curve helping?" diagnostic. */
  calibrated_brier_score?: number | null;
  calibrated_brier_samples?: number;
  reliability?: ReliabilityBucket[];
  per_persona: AggregatePersona[];
  lessons: AggregateLesson[];
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

export async function fetchSweepAggregate(
  sweepId: string,
): Promise<SweepAggregate> {
  const r = await api.get<SweepAggregate>(
    `/discussion/sweeps/${sweepId}/aggregate`,
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

export async function fetchSweeps(): Promise<BacktestSweep[]> {
  const r = await api.get<BacktestSweep[]>("/discussion/sweeps");
  return r.data;
}

export async function createSweep(
  body: CreateBacktestSweepInput,
): Promise<BacktestSweep> {
  const r = await api.post<BacktestSweep>("/discussion/sweeps", body);
  return r.data;
}

export async function startSweep(id: string): Promise<BacktestSweep> {
  const r = await api.post<BacktestSweep>(`/discussion/sweeps/${id}/start`);
  return r.data;
}

export async function cancelSweep(id: string): Promise<BacktestSweep> {
  const r = await api.post<BacktestSweep>(`/discussion/sweeps/${id}/cancel`);
  return r.data;
}

export async function deleteSweep(id: string): Promise<void> {
  await api.delete(`/discussion/sweeps/${id}`);
}


/** Pure orchestration helper for the post-mortem 3-step chain
 *  (PR #279). Extracted from `DiscussionPage.runPostMortemFlow` so
 *  the ordering / short-circuit semantics can be unit-tested
 *  without booting the page's full mutation + SSE stack.
 *
 *  Steps:
 *    1. `runPostMortem()` — POST /sessions/:id/post-mortem
 *       (inject the user_input critique prompt)
 *    2. `runRound()` — SSE the next round so personas reflect
 *    3. `runConclude()` — re-synthesize → routes to
 *       `post_mortem_conclusion` (PR #272 routing)
 *
 *  Failures at step 1 short-circuit (steps 2 + 3 don't fire) and
 *  call `onError(detail)` so the page can surface the error
 *  banner. Step 2 / 3 errors are owned by their own callers
 *  (runRound has its own SSE error handling; conclude is fire-
 *  and-forget so a failure shows up in the conclusion card's
 *  parse-error state).
 *
 *  `runConclude` is fired inside `setTimeout(0)` so React state
 *  updates from runRound flush before the synthesizer fetch
 *  begins — without the defer, the SSE-completion state and the
 *  conclude trigger race and the conclude can run with a stale
 *  `current_round`. */
export interface PostMortemFlowDeps {
  canStart: () => boolean;
  runPostMortem: () => Promise<PostMortemResponse | void>;
  runRound: () => Promise<void>;
  runConclude: () => void;
  onError: (detail: string) => void;
  /** Optional: caller wants to be told the post-mortem was skipped
   *  (so it can flash a toast like "推薦已達標，無需檢討"). */
  onSkipped?: (verdict: PostMortemVerdict | null) => void;
  /** Defer hook — defaults to `setTimeout(fn, 0)`. Tests inject a
   *  synchronous runner so they can assert ordering without
   *  fake-timer plumbing. */
  defer?: (fn: () => void) => void;
}

export async function runPostMortemFlowSteps({
  canStart,
  runPostMortem,
  runRound,
  runConclude,
  onError,
  onSkipped,
  defer = (fn) => {
    setTimeout(fn, 0);
  },
}: PostMortemFlowDeps): Promise<void> {
  if (!canStart()) return;
  let result: PostMortemResponse | void;
  try {
    result = await runPostMortem();
  } catch (err) {
    const detail =
      (err as { response?: { data?: { detail?: string } } })?.response?.data
        ?.detail ?? (err as Error).message;
    onError(detail);
    return;
  }
  // Win-skip path (learning loop): backend already short-circuited
  // because the recommendation cleared the win threshold. No round
  // to run, no synthesizer to fire — just notify the UI so it can
  // surface the success badge and stop the spinner.
  if (result && (result as PostMortemResponse).status === "skipped") {
    onSkipped?.((result as PostMortemResponse).verdict ?? null);
    return;
  }
  await runRound();
  defer(() => {
    runConclude();
  });
}


// ── round-context / scoreboard / auto-run config ────────────────

export async function fetchRoundContexts(id: string): Promise<RoundContextSnapshot[]> {
  const res = await api.get<RoundContextSnapshot[]>(
    `/discussion/sessions/${id}/contexts`,
  );
  return res.data;
}

export async function fetchScoreboard(
  id: string,
  options?: { debug?: boolean },
): Promise<ScoreboardResponse> {
  const url = options?.debug
    ? `/discussion/sessions/${id}/scoreboard?debug=true`
    : `/discussion/sessions/${id}/scoreboard`;
  const res = await api.get<ScoreboardResponse>(url);
  return res.data;
}

export async function fetchAutoRunConfig(): Promise<AutoRunConfig> {
  const res = await api.get<AutoRunConfig>("/discussion/auto-run/config");
  return res.data;
}

export async function saveAutoRunConfig(body: {
  enabled: boolean;
  persona_ids: string[];
  topic: string;
  rules: string;
  market?: string;
}): Promise<AutoRunConfig> {
  const res = await api.put<AutoRunConfig>("/discussion/auto-run/config", body);
  return res.data;
}
