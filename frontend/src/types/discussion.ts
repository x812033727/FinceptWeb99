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

/** PR-2: structured delta between an original conclusion and the
 * post-mortem revised one. Computed and persisted at synthesis
 * time so the UI doesn't need to recompute on every render. */
export interface PostMortemDiff {
  /** Symbols in post but not in orig. */
  symbols_added: string[];
  /** Symbols in orig but not in post. */
  symbols_removed: string[];
  /** Per-symbol confidence shift, only included when |delta| >= 0.05
   * (suppress rounding-noise non-changes). */
  confidence_changes: Record<string, { orig: number; post: number; delta: number }>;
  /** post.consensus_score - orig.consensus_score, rounded. */
  consensus_score_delta: number;
  /** True when the time_horizon string changed. */
  time_horizon_changed: boolean;
  /** Jaccard index 0-1 over whitespace-tokenised reasoning text.
   * 1.0 = identical reasoning, 0.0 = entirely different. */
  reasoning_overlap: number;
}

export interface Conclusion {
  recommended_symbols: string[];
  /** Backend enrichment (PR for "show company name in result"): maps
   * symbol → display name when an in-memory lookup is available — TW
   * codes pick up 公司簡稱 from `tw_market_service._name_map`, crypto
   * codes pick up the canonical name from `data.crypto.symbols.NAMES`.
   * Absent / missing entries mean the symbol falls back to the bare
   * code (US, or a TW code not yet loaded by the symbol-map cron). */
  symbol_names?: Record<string, string>;
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
  /** PR-5c: per-conclusion baseline delta. NULL on live discussions
   * without a sweep parent or on cold-start strategies (no health
   * snapshot yet). */
  vs_baseline?: {
    brier_baseline: number | null;
    consensus_baseline: number | null;
    consensus_score: number;
    consensus_pct_change: number | null;
    verify_pending: boolean;
  };
  /** Set by the backend when the synthesizer's JSON couldn't be parsed
   * even via the lenient salvage path — the UI shows a degraded
   * "解析失敗" badge instead of pretending the conclusion is real. */
  _parse_error?: boolean;
  /** Data-freshness anchor: which trading session this conclusion's
   * numbers actually reference. Copied verbatim from
   * `ctx["captured_session"]` at synthesis time so the
   * ConclusionCard can render a "資料截至 5/27 收盤" badge without
   * re-fetching the round-context snapshot. Optional for forward-
   * compat with conclusions written before the freshness phase. */
  captured_session?: CapturedSession;
}

/** Session anchor metadata attached to every discussion ctx + carried
 * onto the conclusion. The string `session_date` is the trading day
 * whose close most of the numeric blocks reference; `phase` describes
 * where the discussion sits relative to that day (backtest /
 * intraday / today_close_published / pre_open_today / etc.). The
 * `hint_zh` field is the human-friendly explanation the persona
 * prompt also surfaces. */
export interface CapturedSession {
  session_date: string | null;
  // Backtest only: the decision / entry / grading day. `session_date`
  // is the info cutoff (previous trading day — what the personas could
  // see), `decision_date` is the day you'd act on (enter at its open).
  // Absent in live modes.
  decision_date?: string | null;
  phase: string;
  is_intraday: boolean;
  hint_zh: string;
}

export type DiscussionStatus = "draft" | "running" | "done";

// 4-band verdict (大勝/勝/大敗/敗) plus legacy "win"/"loss" for rows
// graded before the 4-band cutover. `unverifiable` covers
// no-symbols-recommended and stale-grace cases.
export type Verdict =
  | "big_win"
  | "win"
  | "big_loss"
  | "loss"
  | "unverifiable";

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
  /** PR-2: structured delta between `conclusion` and
   * `post_mortem_conclusion`. Populated by the synthesizer at the
   * moment the post-mortem conclusion is written so the UI can
   * render "what changed" without recomputing on every read.
   * NULL when (a) no post-mortem ran, (b) either side was a parse
   * error, or (c) discussion is from before PR-2. */
  post_mortem_diff?: PostMortemDiff | null;
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
  /** Display name (e.g. "台積電", "Bitcoin") when an in-memory lookup
   * resolves; NULL otherwise so the UI falls back to the bare code. */
  name?: string | null;
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
  // Diagnostic payload populated only when `?debug=true`. Shape is
  // intentionally loose (the backend keeps it open so new fields
  // can be added without forcing a frontend rebuild) so the UI
  // renders it via JSON.stringify rather than a typed schema.
  debug?: Record<string, unknown> | null;
}

export interface AutoRunConfig {
  enabled: boolean;
  persona_ids: string[];
  topic: string;
  rules: string;
  market: DiscussionMarket;
  send_email: boolean;
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
