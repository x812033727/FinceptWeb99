/**
 * Session / discussion lifecycle API fetchers + their request/response
 * shapes: core CRUD, mid-round interjection, post-mortem, and the
 * round-context / scoreboard / auto-run config endpoints.
 */
import { api } from "./_shared";
import type {
  AgentInfo,
  AutoRunConfig,
  Conclusion,
  Discussion,
  DiscussionDetail,
  PersonaUsageDetail,
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

/** Per-round token tally (input + output) fed to the AI, persisted in
 * `llm_usage_events`. Empty for discussions that ran before per-round
 * attribution was wired in. */
export interface RoundUsage {
  round: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cost_usd: number;
}

export async function fetchRoundUsage(id: string): Promise<RoundUsage[]> {
  const res = await api.get<RoundUsage[]>(`/discussion/sessions/${id}/round-usage`);
  return res.data;
}

/** Finer per-round usage: exact per-persona tokens / cost / tool counts
 * joined with each turn's prompt-composition breakdown. Backs the round
 * "ctx 用量明細" panel. */
export async function fetchRoundUsageDetail(
  id: string,
): Promise<PersonaUsageDetail[]> {
  const res = await api.get<PersonaUsageDetail[]>(
    `/discussion/sessions/${id}/round-usage/detail`,
  );
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

// ── B4: mid-round interjection + post-conclusion 追問 ─────────────

/** `queued` while the discussion is running (the question/answer
 * turns arrive over the round's SSE stream); `answered` on the
 * concluded 追問 path, with both turns returned inline. */
export interface InterjectResponse {
  status: "queued" | "answered";
  target_persona?: string | null;
  question_turn?: Turn | null;
  answer_turn?: Turn | null;
}

export async function interjectSession(
  id: string,
  body: { question: string; target_persona?: string },
): Promise<InterjectResponse> {
  const res = await api.post<InterjectResponse>(
    `/discussion/sessions/${id}/interject`, body,
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
  send_email?: boolean;
  strategy_run_counts?: AutoRunConfig["strategy_run_counts"];
}): Promise<AutoRunConfig> {
  const res = await api.put<AutoRunConfig>("/discussion/auto-run/config", body);
  return res.data;
}
