/**
 * Defaults + localStorage helpers for the discussion UI. Topic / rules
 * / collapse-state / post-mortem result snapshots — anything that
 * survives a page reload but doesn't belong on the server.
 *
 * Imported by `_helpers.tsx` (re-export shim) so the existing 38
 * importers across the discussion/ component group keep working.
 */
import type { DiscussionMarket, Turn } from "@/types/discussion";
import type { PostMortemResponse } from "./_api";

// ── defaults ──────────────────────────────────────────────────────

export const DEFAULT_TOPIC =
  "找出本週（未來 5 個交易日）值得短線進場的台股 1-3 檔，並列出進場條件與停損點。";

export const DEFAULT_RULES = [
  "1. 每位專家發言 ≤ 200 字。",
  "2. 必須引用至少一個具體數據（價、量、法人、新聞情緒）。",
  "3. 反對其他專家時必須點名是反對誰、為什麼。",
  "4. 只能從本場「候選股」中推薦；若候選股皆不符合進場條件，請明確棄權（recommended_symbols 留空並在結論說明棄權原因），不得改推候選股以外的標的。",
  "5. 短線定義：5 個交易日內出場。",
].join("\n");

export const DEFAULT_PERSONAS = ["market_analyst", "trading_coach", "lynch", "simons"];

export const STANCE_BADGE: Record<Turn["stance"], { label: string; cls: string }> = {
  agree: { label: "✓ 同意", cls: "bg-success/10 text-success border-success/30" },
  dissent: { label: "✗ 異議", cls: "bg-danger/10 text-danger border-danger/30" },
  supplement: { label: "↳ 補充", cls: "bg-blue-900/30 text-blue-300 border-blue-800/50" },
  user_input: { label: "✎ 插話", cls: "bg-warning/10 text-warning border-warning/30" },
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

// Personas + market: the original code reset these to the
// `DEFAULT_PERSONAS` / `"TW"` constants every time the user opened a
// fresh draft. Users who don't trade TW or who have a stable preferred
// roster (e.g. "always include trading_coach + lynch + my macro pick")
// had to re-toggle 4 buttons every session. Now the discussion config
// form's 「儲存為預設」 button persists the current selection, and a
// fresh draft hydrates from here instead.

const LS_PERSONAS_KEY = "fincept.discussion.default_personas";
const LS_MARKET_KEY = "fincept.discussion.default_market";

const VALID_MARKETS: ReadonlyArray<DiscussionMarket> = ["TW", "US", "GLOBAL"];

export function readDefaultPersonas(): string[] {
  try {
    const raw = localStorage.getItem(LS_PERSONAS_KEY);
    if (!raw) return [...DEFAULT_PERSONAS];
    const parsed = JSON.parse(raw);
    if (
      !Array.isArray(parsed) ||
      parsed.length === 0 ||
      !parsed.every((x) => typeof x === "string" && x.length > 0)
    ) {
      return [...DEFAULT_PERSONAS];
    }
    return parsed as string[];
  } catch {
    return [...DEFAULT_PERSONAS];
  }
}

export function rememberPersonas(personaIds: string[]): void {
  try {
    localStorage.setItem(LS_PERSONAS_KEY, JSON.stringify(personaIds));
  } catch {
    /* private mode / quota — silent, see rememberTopic */
  }
}

export function readDefaultMarket(): DiscussionMarket {
  try {
    const raw = localStorage.getItem(LS_MARKET_KEY);
    if (!raw) return "TW";
    if ((VALID_MARKETS as readonly string[]).includes(raw)) {
      return raw as DiscussionMarket;
    }
    return "TW";
  } catch {
    return "TW";
  }
}

export function rememberMarket(market: DiscussionMarket): void {
  try {
    localStorage.setItem(LS_MARKET_KEY, market);
  } catch {
    /* see rememberTopic */
  }
}

// Single entry point for the config-form 「儲存為預設」 button —
// snapshots the four user-facing fields in one call so callers don't
// forget one (e.g. saving topic but not market).
export function rememberDiscussionDefaults(values: {
  topic: string;
  rules: string;
  personaIds: string[];
  market: DiscussionMarket;
}): void {
  rememberTopic(values.topic);
  rememberRules(values.rules);
  rememberPersonas(values.personaIds);
  rememberMarket(values.market);
}

const LS_ROUNDS_PER_CLICK_KEY = "fincept.discussion.rounds_per_click";
const DEFAULT_ROUNDS_PER_CLICK = 5;
const MIN_ROUNDS_PER_CLICK = 1;
const MAX_ROUNDS_PER_CLICK = 10;

export function readRoundsPerClick(): number {
  try {
    const raw = localStorage.getItem(LS_ROUNDS_PER_CLICK_KEY);
    if (raw === null) return DEFAULT_ROUNDS_PER_CLICK;
    const n = Number.parseInt(raw, 10);
    if (!Number.isFinite(n)) return DEFAULT_ROUNDS_PER_CLICK;
    return Math.min(MAX_ROUNDS_PER_CLICK, Math.max(MIN_ROUNDS_PER_CLICK, n));
  } catch {
    return DEFAULT_ROUNDS_PER_CLICK;
  }
}

export function rememberRoundsPerClick(n: number): void {
  try {
    const clamped = Math.min(
      MAX_ROUNDS_PER_CLICK,
      Math.max(MIN_ROUNDS_PER_CLICK, Math.trunc(n)),
    );
    localStorage.setItem(LS_ROUNDS_PER_CLICK_KEY, String(clamped));
  } catch {
    /* private mode / quota — silent */
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

// ── localStorage: post-mortem snapshot persistence (PR #268) ─────
// The mutation result is in-memory only — without persistence the
// scannable leaderboard disappears on page reload, leaving only the
// markdown-formatted gainers list inside the user_input turn.
// Operators end up reloading and wondering "did the post-mortem even
// happen". Keyed by discussion ID so each backtest carries its own
// snapshot. Survives same-browser reloads but not across browsers —
// acceptable trade-off (it's operator situational awareness, not
// durable state).

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
