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
}): Promise<Discussion> {
  const res = await api.post<Discussion>("/discussion/sessions", body);
  return res.data;
}

export async function updateSession(
  id: string,
  body: { topic?: string; rules?: string; persona_ids?: string[] },
): Promise<Discussion> {
  const res = await api.patch<Discussion>(`/discussion/sessions/${id}`, body);
  return res.data;
}

export async function deleteSession(id: string): Promise<void> {
  await api.delete(`/discussion/sessions/${id}`);
}

export async function concludeSession(id: string): Promise<{ conclusion: Conclusion }> {
  const res = await api.post<{ conclusion: Conclusion }>(
    `/discussion/sessions/${id}/conclude`,
  );
  return res.data;
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
}): Promise<AutoRunConfig> {
  const res = await api.put<AutoRunConfig>("/discussion/auto-run/config", body);
  return res.data;
}

// ── persona-name lookup ────────────────────────────────────────────

export function usePersonaName(agents: AgentInfo[]) {
  const { t, i18n } = useTranslation();
  return (id: string) => {
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

  return out;
}
