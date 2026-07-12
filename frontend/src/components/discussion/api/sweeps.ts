/**
 * Backtest sweep API fetchers + their request/response shapes,
 * including the sweep/strategy aggregate payload (`SweepAggregate`),
 * which `strategies.ts` also consumes for its strategy-level rollup.
 */
import { api } from "./_shared";
import type { DiscussionMarket } from "@/types/discussion";

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
    /** 4-band rollout: big_win + win count as wins, big_loss + loss
     *  as losses. Legacy "win"/"loss" rows from before the cutover
     *  land in the matching key without re-classification. */
    big_win?: number;
    win: number;
    big_loss?: number;
    loss: number;
    unverifiable: number;
    pending: number;
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

export async function fetchSweepAggregate(
  sweepId: string,
): Promise<SweepAggregate> {
  const r = await api.get<SweepAggregate>(
    `/discussion/sweeps/${sweepId}/aggregate`,
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
