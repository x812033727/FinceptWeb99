/**
 * Formatters + inline-markdown renderers + context summarizer +
 * markdown-export helpers for the discussion subsystem. Imported by
 * `_helpers.tsx` (re-export shim) so existing call sites continue to
 * import from "./_helpers" without edits.
 *
 * `.tsx` because `colorizeNumbers` and `renderInlineMarkdown` return
 * JSX. Pure formatters live alongside them — splitting further would
 * fragment the discussion presentation logic without a clear seam.
 */
import { useTranslation } from "react-i18next";
import { formatTaipei } from "@/lib/timeFormat";
import type {
  AgentInfo,
  Conclusion,
  DiscussionDetail,
  Turn,
} from "@/types/discussion";

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

// PR-C: per-persona avatar glyph (1-2 char) from
// `personas.agents.<id>.short`. Returns undefined when no explicit
// short label exists, leaving the avatar to derive the initial from
// the localised name (single CJK char or first ASCII letter). The
// _user pseudo-persona uses a dedicated initial.
export function usePersonaShort() {
  const { t, i18n } = useTranslation();
  return (id: string): string | undefined => {
    if (id === "_user") return t("discussion.user_persona_short", { defaultValue: "✎" });
    const key = `personas.agents.${id}.short`;
    return i18n.exists(key) ? t(key) : undefined;
  };
}

// ── date formatting ────────────────────────────────────────────────

export function formatDateShort(iso: string | undefined): string {
  if (!iso) return "";
  // Render in Asia/Taipei regardless of browser TZ. `date` variant
  // returns `2026/05/28`; slice off the year so the short form
  // matches the prior `month/day` shape callers expect.
  const full = formatTaipei(iso, "date");
  if (!full) return "";
  const parts = full.split("/");
  return parts.length === 3 ? `${parts[1]}/${parts[2]}` : full;
}

export function formatDateLong(iso: string | undefined): string {
  if (!iso) return "";
  return formatTaipei(iso, "datetime");
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

export type OutcomeBand = "big_win" | "win" | "big_loss" | "loss";

export interface FormattedSymbolLine {
  symbol: string;
  changePcts: (number | null)[];
  band: OutcomeBand | null;
}

export interface FormattedTitle {
  text?: string;
  date?: string;
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

// 4-band verdict label + colour table (大勝/勝/大敗/敗). Legacy
// "win"/"loss" rows fall into the same buckets via the matching key
// so historical discussions keep their badge.
export const BAND_LABELS: Record<
  string,
  { mark: string; cls: string }
> = {
  big_win: { mark: "大勝", cls: "text-emerald-500" },
  win: { mark: "勝", cls: "text-green-500" },
  big_loss: { mark: "大敗", cls: "text-red-600" },
  loss: { mark: "敗", cls: "text-orange-500" },
  unverifiable: { mark: "", cls: "text-muted-foreground" },
};

// TS mirror of `backend/services/outcome_classifier.py::classify_outcome`.
// Same defaults (20 % / 5 % / -5 %), same precedence (大敗 → 大勝 → 勝 → 敗).
// `changePcts` are FRACTIONS (0.05 = +5 %), matching the values that
// `formatDiscussionTitle` already produces from day1_open + closes.
// D5 = the LAST array slot; partial windows (trailing nulls) block the
// big_win path but still allow big_loss / win / loss to fire.
export function classifySymbolBand(
  changePcts: (number | null)[],
  thresholds: {
    bigWinPct: number;
    winPct: number;
    bigLossPct: number;
  } = { bigWinPct: 0.2, winPct: 0.05, bigLossPct: -0.05 },
): OutcomeBand | null {
  const numeric = changePcts.filter((p): p is number => p !== null);
  if (!numeric.length) return null;
  const peak = Math.max(...numeric);
  const trough = Math.min(...numeric);
  const d5 = changePcts[changePcts.length - 1];
  if (trough <= thresholds.bigLossPct) return "big_loss";
  if (d5 !== null && d5 !== undefined && d5 >= thresholds.bigWinPct) {
    return "big_win";
  }
  if (peak >= thresholds.winPct) return "win";
  return "loss";
}

export function formatDiscussionTitle(s: {
  topic: string;
  conclusion: Conclusion | null;
  created_at: string;
  /** PR #276: backtest discussions display the as_of_date (the
   *  date being analyzed) in the sidebar title rather than
   *  `created_at` (when the row happened to be created — usually
   *  today, regardless of which historical day is being replayed).
   *  Optional / nullable so live discussions fall back to
   *  `created_at` as before. */
  as_of_date?: string | null;
  day1_open_prices?: Record<string, number> | null;
  day5_close_prices?: Record<string, number> | null;
  daily_close_prices?: Record<string, (number | null)[]> | null;
}): FormattedTitle {
  const syms = s.conclusion?.recommended_symbols ?? [];
  if (!syms.length) {
    return { text: s.topic };
  }
  // Prefer as_of_date for backtest discussions — the operator
  // cares about which historical day is being replayed, not when
  // they happened to click create. Live discussions
  // (`as_of_date == null`) fall back to created_at as before.
  const dateSource = s.as_of_date
    ? `${s.as_of_date}T00:00:00Z`
    : s.created_at;
  const date = formatTaipeiDateCompact(dateSource);

  // Per-symbol band classification: each symbol is graded against the
  // 4-band rule independently (大敗優先) rather than rolled up into a
  // single discussion-level verdict. Earlier the sidebar pulled the
  // discussion-level `verdict` field, but pre-cutover rows still
  // carry legacy "win"/"loss" strings that contradict their own
  // close prices — computing live from day1_open + daily_close_prices
  // avoids that stale-string trap.
  const opens = s.day1_open_prices ?? {};
  const closes_legacy = s.day5_close_prices ?? {};
  const closes_daily = s.daily_close_prices ?? {};
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
    const band = classifySymbolBand(changePcts);
    return { symbol: sym, changePcts, band };
  });

  return { date, lines };
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
      // PR #278: prefer as_of_date when present (backtest prior
      // discussions) so the summary shows the historical day each
      // prior discussion was analysing, not when its row happened
      // to be created. Live priors fall through to created_at.
      const asOf = (p.as_of_date as string | null | undefined) ?? null;
      const date = asOf
        ? asOf
        : String(p.created_at ?? "").slice(0, 10);
      lines.push(`${date} ${matched.join(",")} → ${horizon}/${verdict}`);
    }
    if (lines.length > 0) out.prior_discussions_summary = lines.join(" | ");
  }

  return out;
}

// ── markdown export ───────────────────────────────────────────────

/**
 * Renders a Discussion + its turns as a single Markdown document
 * (topic / rules / persona roster / per-round transcript / conclusion).
 * Pure function — no side effects, callable from any component or
 * test. Previously lived inline inside ConclusionCard; lifted here so
 * the DiscussionPage action bar can offer the same export without
 * duplicating logic.
 */
export function buildDiscussionMarkdown(
  detail: DiscussionDetail,
  personaName: (id: string) => string,
): string {
  const lines: string[] = [];
  lines.push(`# ${detail.topic}`);
  lines.push("");
  lines.push("## 共同規則");
  lines.push("");
  lines.push("```");
  lines.push(detail.rules);
  lines.push("```");
  lines.push("");
  lines.push("## 出席專家");
  lines.push("");
  for (const pid of detail.persona_ids) {
    lines.push(`- ${personaName(pid)} (${pid})`);
  }
  lines.push("");

  const turnsByRound = new Map<number, Turn[]>();
  for (const tn of detail.turns) {
    if (!turnsByRound.has(tn.round)) turnsByRound.set(tn.round, []);
    turnsByRound.get(tn.round)!.push(tn);
  }
  const sortedRounds = [...turnsByRound.keys()].sort((a, b) => a - b);
  for (const r of sortedRounds) {
    lines.push(`## 第 ${r} 輪`);
    lines.push("");
    const roundTurns = (turnsByRound.get(r) ?? []).slice().sort(
      (a, b) => a.turn_index - b.turn_index,
    );
    for (const tn of roundTurns) {
      const stanceLabel =
        tn.stance === "agree" ? "✓ 同意" :
        tn.stance === "dissent" ? "✗ 異議" :
        tn.stance === "user_input" ? "✎ 插話" : "↳ 補充";
      lines.push(`### ${personaName(tn.persona_id)} — ${stanceLabel}`);
      lines.push("");
      lines.push(tn.content.trim() || "_（同意，無補充）_");
      lines.push("");
    }
  }

  if (detail.conclusion) {
    const c = detail.conclusion;
    lines.push("## 結論");
    lines.push("");
    if (c.recommended_symbols.length) {
      lines.push(`- 推薦標的：${c.recommended_symbols.join(", ")}`);
    }
    lines.push(`- 共識度：${(c.consensus_score * 100).toFixed(0)}%`);
    lines.push(`- 時間框架：${c.time_horizon}`);
    lines.push("");
    lines.push("### 理由");
    lines.push("");
    lines.push(c.reasoning);
    if (c.risks.length) {
      lines.push("");
      lines.push("### 風險");
      lines.push("");
      for (const risk of c.risks) {
        lines.push(`- ${risk}`);
      }
    }
  }
  lines.push("");
  return lines.join("\n");
}

/**
 * Builds the export filename in the format
 * `{YYYYMMDD}_R{rounds}_{personas}.md` — e.g. `20260508_R5_8.md`.
 *
 * Date source priority:
 *   1. `as_of_date` (backtest mode — the historical day being analysed)
 *   2. `created_at` formatted in Asia/Taipei (live discussions)
 *
 * Round count: `current_round`. Falls back to `max(turn.round)` when
 * `current_round` is 0 but turns exist (defensive — shouldn't happen
 * in practice but keeps the filename meaningful).
 */
export function buildDiscussionExportFilename(detail: {
  as_of_date?: string | null;
  created_at: string;
  current_round: number;
  persona_ids: string[];
  turns?: Turn[];
}): string {
  const dateSource = detail.as_of_date
    ? `${detail.as_of_date}T00:00:00Z`
    : detail.created_at;
  const date = formatTaipeiDateCompact(dateSource);
  const rounds =
    detail.current_round > 0
      ? detail.current_round
      : detail.turns && detail.turns.length > 0
        ? Math.max(...detail.turns.map((tn) => tn.round))
        : 0;
  const personas = detail.persona_ids.length;
  return `${date}_R${rounds}_${personas}.md`;
}

/**
 * Triggers a browser download of the discussion rendered as Markdown.
 * Filename uses {@link buildDiscussionExportFilename}; body uses
 * {@link buildDiscussionMarkdown}. No-op when running outside a DOM
 * (guards `document` for SSR / test environments).
 */
export function downloadDiscussionMarkdown(
  detail: DiscussionDetail,
  personaName: (id: string) => string,
): void {
  if (typeof document === "undefined") return;
  const md = buildDiscussionMarkdown(detail, personaName);
  const blob = new Blob([md], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = buildDiscussionExportFilename(detail);
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}
