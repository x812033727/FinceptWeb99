/**
 * Shared types for the Discussion subsystem.
 *
 * Pulled out of `pages/DiscussionPage.tsx` and `pages/AdminPage.tsx`
 * so future component splits (round panel, conclusion card,
 * scoreboard card, auto-run config card) can share the same
 * type contract without redefining or shadowing.
 */

export interface AgentInfo {
  id: string;
  name: string;
  description: string;
  default_provider: string;
}

export interface Turn {
  id: number;
  round: number;
  turn_index: number;
  persona_id: string;
  /** `user_input` is the discussion owner's between-rounds injection
   * (PR #211) — rendered as a directive in the transcript instead
   * of an analyst opinion. */
  stance: "agree" | "dissent" | "supplement" | "user_input";
  content: string;
  created_at: string;
}

export type TimeHorizon = "short_term" | "medium_term" | "long_term";

export interface Recommendation {
  symbol: string;
  /** 0.0-1.0 — synthesizer's per-pick confidence that the symbol will
   * see a meaningful positive 5-day move. Pre-PR-C0 conclusions don't
   * carry this; the parser fills 0.5 (neutral) so consumers can
   * always rely on the field. */
  confidence: number;
  /** Calibrated confidence after the strategy's isotonic curve has
   * been applied (PR-C2 follow-up). Optional — only present when the
   * strategy has accumulated enough samples (>= 30) to fit a curve.
   * Cold-start strategies emit only `confidence`; the UI should
   * fall back to that when this field is absent. */
  calibrated_confidence?: number;
}

/** PR-1: Synthesizer post-parse quality signals — surfaces stance /
 * confidence / contradiction / hallucination warnings on conclusions
 * that look structurally ok but smell wrong (built on hallucinated
 * data, contradicting actual stance distribution, all picks too
 * confident). The UI renders badge chips per signal so the operator
 * sees the warning before reading the recommendations. */
export interface QualitySignals {
  /** Stance counts in the latest round, excluding user-input
   * directives. Bucket `other` catches stance values the backend
   * doesn't recognize (forward compat for new stance types). */
  stance_distribution?: {
    agree: number;
    dissent: number;
    supplement: number;
    other: number;
  };
  /** Confidence summary over recommendations. `n=0` when no picks. */
  confidence_stats?: {
    n: number;
    mean?: number;
    median?: number;
    max?: number;
    min?: number;
    /** True when mean > 0.75 OR every pick ≥ 0.8 — the synthesizer
     * prompt forbids this distribution but the parser doesn't enforce
     * it; this signal is the post-parse check. */
    over_confident?: boolean;
  };
  /** True when latest round had more dissent than agree but the
   * synthesizer still emitted recommendations. */
  consensus_contradiction?: boolean;
  /** Triples (round, persona_id, signal) where a persona quoted a
   * numeric value for a signal NOT in the round's prompt context.
   * Empty array = clean transcript. */
  hallucination_warnings?: Array<{
    round: number;
    persona_id: string;
    signal: string;
  }>;
  /** Set when computation was skipped (e.g. parse error placeholder
   * couldn't be analyzed). Distinguishes "parse broke" from "parsed
   * cleanly with no warnings". */
  _skipped?: string;
}

export interface Conclusion {
  recommended_symbols: string[];
  /** Per-pick confidence breakdown (PR-C0). Optional for forward-
   * compat with old conclusions read out of the DB before this PR
   * landed. New conclusions always carry it; consumers should prefer
   * this over the flat `recommended_symbols` when available. */
  recommendations?: Recommendation[];
  reasoning: string;
  risks: string[];
  time_horizon: TimeHorizon;
  consensus_score: number;
  /** PR-1: post-parse quality signals (stance / confidence / contradiction
   * / hallucination). Optional for forward-compat with conclusions
   * synthesized before PR-1 landed. */
  quality_signals?: QualitySignals;
  /** Set by the backend when the synthesizer's JSON couldn't be parsed
   * even via the lenient salvage path — the UI shows a degraded
   * "解析失敗" badge instead of pretending the conclusion is real. */
  _parse_error?: boolean;
}

export type DiscussionStatus = "draft" | "running" | "done";

export type Verdict = "win" | "loss" | "unverifiable";

export type DiscussionMarket = "TW" | "US" | "GLOBAL";

export interface Discussion {
  id: string;
  topic: string;
  rules: string;
  persona_ids: string[];
  market: DiscussionMarket;
  status: DiscussionStatus;
  current_round: number;
  conclusion: Conclusion | null;
  /** PR #272: post-mortem self-critique conclusion. Populated when
   * the user runs the post-mortem flow; preserves the original
   * `conclusion` for side-by-side comparison instead of overwriting
   * it. NULL for any discussion that hasn't been through a
   * post-mortem cycle. */
  post_mortem_conclusion?: Conclusion | null;
  verdict?: Verdict | null;
  verdict_reason?: string | null;
  verified_at?: string | null;
  auto_run?: boolean;
  day1_open_prices?: Record<string, number> | null;
  day5_close_prices?: Record<string, number> | null;
  /** PR #140 scoreboard column. Latest non-null entry feeds the
   * sidebar title's close slot so partial-window discussions
   * (D1-D2 only) update immediately instead of waiting for D5. */
  daily_close_prices?: Record<string, (number | null)[]> | null;
  /** Backtest anchor (PR #224). Non-null = "pretend it's that date":
   * ctx fetches filtered to data on/before this; verifier grades
   * against as_of + 5 trading days instead of created_at + 5. UI
   * surfaces as a `回測` badge. */
  as_of_date?: string | null;
  created_at: string;
  updated_at: string;
}

export interface DiscussionDetail extends Discussion {
  turns: Turn[];
}

export interface RoundContextSnapshot {
  round: number;
  /** The full `gather_market_context` dict. Shape evolves as new
   * blocks are added (international_sentiment, top_revenue_growers,
   * …). We accept any so the UI doesn't need a TS type bump every
   * time the backend grows a new field; component reads known keys
   * defensively. */
  context: Record<string, unknown>;
  captured_at: string;
}

export interface ScoreboardRow {
  symbol: string;
  day1_open: number | null;
  daily_closes: (number | null)[];
  change_pcts: (number | null)[];
  days_resolved: number;
}

export interface ScoreboardResponse {
  discussion_id: string;
  // Anchor date the D1-D5 window starts from. Backtest discussions
  // anchor to `as_of_date`; live discussions anchor to TW-local
  // `created_at`. Prefer this over `created_at_tw_date`.
  anchor_date: string;
  // Backwards-compatible alias of `anchor_date`. Same value as
  // `anchor_date` in both modes; kept so older render paths don't
  // break during the rollout.
  created_at_tw_date: string;
  rows: ScoreboardRow[];
}

export interface AutoRunConfig {
  enabled: boolean;
  persona_ids: string[];
  topic: string;
  rules: string;
  market: DiscussionMarket;
  updated_at: string | null;
}

/**
 * Admin LLM-routing config for system tasks (news_sentiment,
 * discussion_synthesizer). Mirrors the persona-override shape but
 * scoped to the background pipelines listed in
 * `services/system_task_config_service._TASKS`.
 */
export interface SystemTaskConfig {
  task_id: string;
  name: string;
  description: string;
  default_provider: string;
  default_model: string;
  effective_provider: string;
  effective_model: string;
  is_overridden: boolean;
  updated_at: string | null;
  updated_by_email: string | null;
}

export interface SystemTaskTestResult {
  ok: boolean;
  provider: string;
  model: string;
  latency_ms: number;
  sample_output: string | null;
  error: string | null;
}
