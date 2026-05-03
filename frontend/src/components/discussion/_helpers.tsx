/**
 * Shared helpers, API fetchers, formatters, and inline-markdown
 * renderer for the DiscussionPage extraction. Pulled out so the
 * card components (AutoRunConfig, Conclusion, RoundContexts,
 * Scoreboard) all import from one source — the formatters are used
 * across cards and the main page transcript.
 */
import { useTranslation } from "react-i18next";
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

// ── defaults ──────────────────────────────────────────────────────

export const DEFAULT_TOPIC =
  "找出本週（未來 5 個交易日）值得短線進場的台股 1-3 檔，並列出進場條件與停損點。";

export const DEFAULT_RULES = [
  "1. 每位專家發言 ≤ 200 字。",
  "2. 必須引用至少一個具體數據（價、量、法人、新聞情緒）。",
  "3. 反對其他專家時必須點名是反對誰、為什麼。",
  "4. 不得推薦未在「市場現況」的 top_gainers / top_losers 中出現的標的。",
  "5. 短線定義：5 個交易日內出場。",
].join("\n");

export const DEFAULT_PERSONAS = ["market_analyst", "trading_coach", "lynch", "simons"];

export const STANCE_BADGE: Record<Turn["stance"], { label: string; cls: string }> = {
  agree: { label: "✓ 同意", cls: "bg-green-900/30 text-green-300 border-green-800/50" },
  dissent: { label: "✗ 異議", cls: "bg-red-900/30 text-red-300 border-red-800/50" },
  supplement: { label: "↳ 補充", cls: "bg-blue-900/30 text-blue-300 border-blue-800/50" },
  user_input: { label: "✎ 插話", cls: "bg-amber-900/30 text-amber-300 border-amber-800/50" },
};

// ── localStorage: topic / rules / collapse state ──────────────────

const LS_TOPIC_KEY = "fincept.discussion.last_topic";
const LS_RULES_KEY = "fincept.discussion.last_rules";

export function readDefaultTopic(): string {
  try {
    return localStorage.getItem(LS_TOPIC_KEY) ?? DEFAULT_TOPIC;
  } catch {
    return DEFAULT_TOPIC;
  }
}
export function readDefaultRules(): string {
  try {
    return localStorage.getItem(LS_RULES_KEY) ?? DEFAULT_RULES;
  } catch {
    return DEFAULT_RULES;
  }
}
export function rememberTopic(topic: string): void {
  try {
    localStorage.setItem(LS_TOPIC_KEY, topic);
  } catch {
    /* localStorage disabled (private mode, full quota) — silent */
  }
}
export function rememberRules(rules: string): void {
  try {
    localStorage.setItem(LS_RULES_KEY, rules);
  } catch {
    /* see rememberTopic */
  }
}

const LS_COLLAPSE_KEY = "fincept.discussion.collapse";

export interface CollapseState {
  topic: boolean;
  rules: boolean;
  personas: boolean;
  sidebar: boolean;
  autoRun: boolean;
}

const DEFAULT_COLLAPSE: CollapseState = {
  topic: false,
  rules: false,
  personas: false,
  sidebar: false,
  // Per-user opt-in setting; collapsed by default so it doesn't dominate
  // the sidebar — most days users come here to read the transcript, not
  // tweak the daily auto-run config.
  autoRun: true,
};

export function readCollapse(): CollapseState {
  try {
    const raw = localStorage.getItem(LS_COLLAPSE_KEY);
    if (raw) return { ...DEFAULT_COLLAPSE, ...JSON.parse(raw) };
  } catch {
    // ignore — fall through to mobile-aware default
  }
  // First-time visitors on a phone have ~600px of vertical space — the
  // sidebar + topic + rules + personas all expanded would push the
  // transcript off-screen. Default the heaviest sections closed on
  // mobile so the discussion content is visible immediately. Tablet /
  // desktop (≥ 1024px) keep the original "everything open" default.
  const isMobile =
    typeof window !== "undefined" && window.innerWidth < 1024;
  return isMobile
    ? { ...DEFAULT_COLLAPSE, sidebar: true, personas: true, rules: true }
    : DEFAULT_COLLAPSE;
}

export function rememberCollapse(s: CollapseState): void {
  try {
    localStorage.setItem(LS_COLLAPSE_KEY, JSON.stringify(s));
  } catch {
    /* private mode / quota — silent */
  }
}

// ── API helpers ────────────────────────────────────────────────────

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

  injected_turn_id: number;
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
  topic: string;
  rules: string;
  market: DiscussionMarket;
  persona_ids: string[];
  anchor_date: string;
  trading_days_count: number;
  rounds_per_discussion?: number;
  concurrency?: number;
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


/** localStorage persistence for PostMortemGainersCard (PR #268).
 *  The mutation result is in-memory only — without persistence the
 *  scannable leaderboard disappears on page reload, leaving only
 *  the markdown-formatted gainers list inside the user_input turn.
 *  Operators end up reloading and wondering "did the post-mortem
 *  even happen". Keyed by discussion ID so each backtest carries
 *  its own snapshot. Survive across same-browser reloads but not
 *  across browsers — acceptable trade-off (it's operator
 *  situational awareness, not durable state).
 */
const POST_MORTEM_STORAGE_PREFIX = "discussion.postMortem.";

export function rememberPostMortemResult(
  discussionId: string, data: PostMortemResponse,
): void {
  try {
    localStorage.setItem(
      `${POST_MORTEM_STORAGE_PREFIX}${discussionId}`,
      JSON.stringify(data),
    );
  } catch {
    /* private mode / quota — best-effort */
  }
}

export function readPostMortemResult(
  discussionId: string,
): PostMortemResponse | null {
  try {
    const raw = localStorage.getItem(
      `${POST_MORTEM_STORAGE_PREFIX}${discussionId}`,
    );
    if (!raw) return null;
    const parsed = JSON.parse(raw) as PostMortemResponse;
    // Sanity-check shape — corrupt entries (e.g. older format)
    // shouldn't crash the card.
    if (
      typeof parsed?.next_trading_day === "string" &&
      Array.isArray(parsed?.top_gainers)
    ) {
      return parsed;
    }
    return null;
  } catch {
    return null;
  }
}

export async function fetchRoundContexts(id: string): Promise<RoundContextSnapshot[]> {
  const res = await api.get<RoundContextSnapshot[]>(
    `/discussion/sessions/${id}/contexts`,
  );
  return res.data;
}

export async function fetchScoreboard(id: string): Promise<ScoreboardResponse> {
  const res = await api.get<ScoreboardResponse>(
    `/discussion/sessions/${id}/scoreboard`,
  );
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

// ── persona-name lookup ────────────────────────────────────────────

export function usePersonaName(agents: AgentInfo[]) {
  const { t, i18n } = useTranslation();
  return (id: string) => {
    // The pseudo-persona for between-rounds user injections
    // (PR #211). Not in the agents list — render as a localised
    // "discussion owner" label so the transcript reads naturally.
    if (id === "_user") return t("discussion.user_persona_name");
    const a = agents.find((x) => x.id === id);
    if (!a) return id;
    const key = `personas.agents.${id}.name`;
    return i18n.exists(key) ? t(key) : a.name;
  };
}

// ── date formatting ────────────────────────────────────────────────

export function formatDateShort(iso: string | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  return d.toLocaleDateString(undefined, { month: "numeric", day: "numeric" });
}

export function formatDateLong(iso: string | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// ── dynamic discussion title ──────────────────────────────────────

export function formatTaipeiDateCompact(iso: string): string {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  // en-CA gives YYYY-MM-DD; strip dashes for the compact YYYYMMDD form.
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Taipei",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(d);
  return parts.replace(/-/g, "");
}

export interface FormattedSymbolLine {
  symbol: string;
  changePcts: (number | null)[];
  cls: string;
}

export interface FormattedTitle {
  text?: string;
  date?: string;
  verdictMark?: string;
  verdictCls?: string;
  lines?: FormattedSymbolLine[];
}

export function toFixedSmart(n: number): string {
  // Show prices with up to 2 decimals, trimming trailing zeros so
  // round-number TWSE prices read "55" not "55.00".
  return Number.isInteger(n) ? n.toString() : (Math.round(n * 100) / 100).toString();
}

export function signedPct(n: number): string {
  const sign = n >= 0 ? "+" : "";
  return `${sign}${(n * 100).toFixed(1)}%`;
}

export function signedPctSafe(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  const sign = n >= 0 ? "+" : "";
  return `${sign}${(n * 100).toFixed(2)}%`;
}

export function pctClass(n: number | null | undefined): string {
  if (n === null || n === undefined) return "text-muted-foreground";
  return n >= 0 ? "text-green-500" : "text-red-500";
}

export function latestNonNull(arr: (number | null)[] | undefined | null): number | null {
  if (!arr) return null;
  for (let i = arr.length - 1; i >= 0; i--) {
    const v = arr[i];
    if (v != null) return v;
  }
  return null;
}

export function formatCompactNumber(n: number): string {
  // Foreign-buy volume is in shares; numbers like 12_345_678 are
  // unreadable inline. Convert to 萬/億 (TW convention) so the
  // headline stays terse.
  const abs = Math.abs(n);
  if (abs >= 1e8) return `${(n / 1e8).toFixed(2)} 億`;
  if (abs >= 1e4) return `${(n / 1e4).toFixed(1)} 萬`;
  return n.toLocaleString();
}

export function formatDiscussionTitle(s: {
  topic: string;
  conclusion: Conclusion | null;
  verdict?: "win" | "loss" | "unverifiable" | null;
  created_at: string;
  day1_open_prices?: Record<string, number> | null;
  day5_close_prices?: Record<string, number> | null;
  daily_close_prices?: Record<string, (number | null)[]> | null;
}): FormattedTitle {
  const syms = s.conclusion?.recommended_symbols ?? [];
  if (!syms.length) {
    return { text: s.topic };
  }
  const date = formatTaipeiDateCompact(s.created_at);

  let verdictMark = "";
  let verdictCls = "text-foreground";
  if (s.verdict === "win") { verdictMark = "勝"; verdictCls = "text-green-500"; }
  else if (s.verdict === "loss") { verdictMark = "敗"; verdictCls = "text-red-500"; }
  else if (s.verdict === "unverifiable") { verdictCls = "text-muted-foreground"; }

  const opens = s.day1_open_prices ?? {};
  const closes_legacy = s.day5_close_prices ?? {};
  const closes_daily = s.daily_close_prices ?? {};
  const WIN_THRESHOLD = 0.03;
  const lines: FormattedSymbolLine[] = syms.slice(0, 3).map((sym) => {
    const open = sym in opens ? opens[sym] : null;
    let dailyCloses: (number | null)[] | null = closes_daily[sym] ?? null;
    if (!dailyCloses && sym in closes_legacy) {
      dailyCloses = [null, null, null, null, closes_legacy[sym]];
    }
    const safeDailyCloses = dailyCloses ?? [null, null, null, null, null];
    const changePcts: (number | null)[] = safeDailyCloses.map((c) =>
      c !== null && open !== null && open > 0 ? (c - open) / open : null,
    );
    const maxPct = changePcts.reduce<number | null>(
      (acc, p) => (p !== null && (acc === null || p > acc) ? p : acc),
      null,
    );
    const cls =
      maxPct === null
        ? "text-muted-foreground"
        : maxPct >= WIN_THRESHOLD
          ? "text-green-500"
          : "text-red-500";
    return { symbol: sym, changePcts, cls };
  });

  return { date, verdictMark, verdictCls, lines };
}

// ── inline markdown renderer ──────────────────────────────────────

const _BOLD_RE = /\*\*([^*\n][^*\n]*?)\*\*/g;
const _SIGNED_PCT_RE = /([+-]?\d+(?:\.\d+)?%)/g;

export function colorizeNumbers(text: string, baseKey: string): React.ReactNode[] {
  const parts = text.split(_SIGNED_PCT_RE);
  return parts.map((part, idx) => {
    if (idx % 2 === 1) {
      const positive = part.startsWith("+");
      const negative = part.startsWith("-");
      const cls = positive
        ? "text-emerald-400"
        : negative
          ? "text-red-400"
          : "";
      return cls ? (
        <span key={`${baseKey}-pct-${idx}`} className={cls}>
          {part}
        </span>
      ) : (
        part
      );
    }
    return part;
  });
}

export function renderInlineMarkdown(text: string): React.ReactNode[] {
  const segments: React.ReactNode[] = [];
  let cursor = 0;
  let key = 0;
  for (const match of text.matchAll(_BOLD_RE)) {
    const start = match.index ?? 0;
    if (start > cursor) {
      segments.push(
        ...colorizeNumbers(text.slice(cursor, start), `t-${key++}`),
      );
    }
    segments.push(
      <strong key={`b-${key++}`} className="font-semibold text-primary">
        {colorizeNumbers(match[1], `bp-${key++}`)}
      </strong>,
    );
    cursor = start + match[0].length;
  }
  if (cursor < text.length) {
    segments.push(...colorizeNumbers(text.slice(cursor), `t-${key++}`));
  }
  return segments;
}

// ── round-context summarizer ──────────────────────────────────────

export interface RoundCtxSummary {
  taiex_value?: number | null;
  taiex_history_change_pct?: number | null;
  news_bullish?: number;
  news_bearish?: number;
  intl_bullish?: number;
  intl_bearish?: number;
  top_foreign_buyer?: { symbol: string; industry?: string | null; net?: number };
  top_revenue_grower?: { symbol: string; industry?: string | null; yoy?: number };
  /** Compact headline strings extracted from the new context blocks
   * (PR #213): one short line per block, ready to render in
   * `RoundContextRow` without further processing. Each is only
   * present when the underlying block carried meaningful data —
   * the row renderer decides whether to show the slot. */
  macro_summary?: string;
  focus_briefs_summary?: string[];
  user_context_summary?: string;
  prior_discussions_summary?: string;

  /** True when the context was assembled in backtest mode and the
   * news archive didn't reach `as_of`. Lets the row renderer show
   * "新聞 archive 不及" instead of pretending sentiment was 0/0/0. */
  backtest_news_unavailable?: boolean;
}

export function summarizeContext(ctx: Record<string, unknown>): RoundCtxSummary {
  const out: RoundCtxSummary = {};

  const index = ctx.index as Record<string, unknown> | null | undefined;
  if (index && typeof index === "object") {
    const value = index.value;
    if (typeof value === "number") out.taiex_value = value;
    const history = index.history as Array<{ close?: number }> | undefined;
    if (Array.isArray(history) && history.length >= 2) {
      const first = history[0]?.close;
      const last = history[history.length - 1]?.close;
      if (typeof first === "number" && typeof last === "number" && first > 0) {
        out.taiex_history_change_pct = (last - first) / first;
      }
    }
  }

  const news = ctx.news_sentiment as Record<string, unknown> | null | undefined;
  if (news && typeof news === "object") {
    if (typeof news.bullish === "number") out.news_bullish = news.bullish;
    if (typeof news.bearish === "number") out.news_bearish = news.bearish;
  } else if (ctx.backtest === true && news === null) {
    // gather_market_context drops news_sentiment to null in backtest
    // mode when the archive doesn't reach `as_of`. Surface that as
    // its own state so the renderer doesn't fall back to "0 / 0".
    out.backtest_news_unavailable = true;
  }
  const intl = ctx.international_sentiment as Record<string, unknown> | null | undefined;
  if (intl && typeof intl === "object") {
    if (typeof intl.bullish === "number") out.intl_bullish = intl.bullish;
    if (typeof intl.bearish === "number") out.intl_bearish = intl.bearish;
  }

  const buyers = ctx.top_foreign_buyers as Array<Record<string, unknown>> | undefined;
  if (Array.isArray(buyers) && buyers.length > 0) {
    const top = buyers[0];
    out.top_foreign_buyer = {
      symbol: String(top.symbol ?? ""),
      industry: (top.industry as string | undefined) ?? null,
      net: typeof top.net_foreign_buy === "number" ? top.net_foreign_buy : undefined,
    };
  }

  const growers = ctx.top_revenue_growers as Array<Record<string, unknown>> | undefined;
  if (Array.isArray(growers) && growers.length > 0) {
    const top = growers[0];
    out.top_revenue_grower = {
      symbol: String(top.symbol ?? ""),
      industry: (top.industry as string | undefined) ?? null,
      yoy: typeof top.revenue_yoy === "number" ? top.revenue_yoy : undefined,
    };
  }

  // ── new ctx blocks (PR #213) ──────────────────────────────────────
  // Each branch is defensive: the block may be absent (legacy
  // snapshot taken before the field was added) or partially
  // populated (FRED key missing → all summaries null) — fall through
  // silently rather than render `undefined` strings.

  const macro = ctx.macro as Record<string, unknown> | null | undefined;
  if (macro && typeof macro === "object") {
    const ff = (macro.fed_funds_rate as Record<string, unknown> | undefined)?.summary as
      | { latest_value?: number; change_1y?: number | null }
      | undefined;
    const dxy = (macro.usd_index as Record<string, unknown> | undefined)?.summary as
      | { latest_value?: number; change_1y?: number | null }
      | undefined;
    const parts: string[] = [];
    if (ff && typeof ff.latest_value === "number") {
      parts.push(
        `Fed ${toFixedSmart(ff.latest_value)}%` +
          (typeof ff.change_1y === "number"
            ? ` (${ff.change_1y >= 0 ? "+" : ""}${toFixedSmart(ff.change_1y)} YoY)`
            : ""),
      );
    }
    if (dxy && typeof dxy.latest_value === "number") {
      parts.push(
        `DXY ${toFixedSmart(dxy.latest_value)}` +
          (typeof dxy.change_1y === "number"
            ? ` (${dxy.change_1y >= 0 ? "+" : ""}${toFixedSmart(dxy.change_1y)} YoY)`
            : ""),
      );
    }
    if (parts.length > 0) out.macro_summary = parts.join(" · ");
  }

  const briefs = ctx.focus_briefs as Array<Record<string, unknown>> | undefined;
  if (Array.isArray(briefs) && briefs.length > 0) {
    const lines: string[] = [];
    for (const b of briefs) {
      const sym = String(b.symbol ?? "");
      if (!sym) continue;
      const name = (b.name_zh as string | undefined) || (b.name as string | undefined) || "";
      const quote = b.quote as Record<string, unknown> | null | undefined;
      const price = typeof quote?.price === "number" ? quote.price : null;
      const changePct = typeof quote?.change_pct === "number" ? quote.change_pct : null;
      const tech = b.technicals as Record<string, unknown> | null | undefined;
      const rsi = typeof tech?.rsi14 === "number" ? tech.rsi14 : null;
      const f = b.fundamentals as Record<string, unknown> | null | undefined;
      const pe = typeof f?.pe === "number" ? f.pe : null;
      const segs: string[] = [];
      if (price != null) {
        segs.push(`${toFixedSmart(price)}`);
      }
      if (changePct != null) segs.push(signedPct(changePct / 100));
      if (pe != null) segs.push(`PE ${toFixedSmart(pe)}`);
      if (rsi != null) segs.push(`RSI ${toFixedSmart(rsi)}`);
      if (segs.length > 0) {
        lines.push(`${sym}${name ? ` ${name}` : ""}: ${segs.join(" · ")}`);
      }
    }
    if (lines.length > 0) out.focus_briefs_summary = lines;
  }

  const uc = ctx.user_context as Record<string, unknown> | null | undefined;
  if (uc && typeof uc === "object") {
    const portfolios = uc.portfolios as Array<Record<string, unknown>> | undefined;
    const holdings = uc.holdings as Array<Record<string, unknown>> | undefined;
    const wl = uc.watchlist_symbols as Array<Record<string, unknown>> | undefined;
    const overlap = uc.focus_overlap as Record<string, unknown> | undefined;
    const held = (overlap?.held as string[] | undefined) ?? [];
    const watching = (overlap?.watching as string[] | undefined) ?? [];
    const segs: string[] = [];
    if (Array.isArray(portfolios) && portfolios.length > 0) {
      segs.push(`${portfolios.length}p`);
    }
    if (Array.isArray(holdings) && holdings.length > 0) {
      segs.push(`${holdings.length}h`);
    }
    if (Array.isArray(wl) && wl.length > 0) segs.push(`${wl.length}w`);
    if (held.length > 0) segs.push(`held: ${held.join(",")}`);
    if (watching.length > 0) segs.push(`watch: ${watching.join(",")}`);
    if (segs.length > 0) out.user_context_summary = segs.join(" · ");
  }

  const prior = ctx.prior_discussions as Array<Record<string, unknown>> | undefined;
  if (Array.isArray(prior) && prior.length > 0) {
    // Show the freshest prior verdict per matched symbol so the user
    // can spot cross-discussion drift at a glance.
    const lines: string[] = [];
    for (const p of prior.slice(0, 3)) {
      const horizon = (p.time_horizon as string | undefined) ?? "";
      const verdict = (p.verdict as string | undefined) ?? "?";
      const matched = (p.matched_symbols as string[] | undefined) ?? [];
      const date = String(p.created_at ?? "").slice(0, 10);
      lines.push(`${date} ${matched.join(",")} → ${horizon}/${verdict}`);
    }
    if (lines.length > 0) out.prior_discussions_summary = lines.join(" | ");
  }

  return out;
}
