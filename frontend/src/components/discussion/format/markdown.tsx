/**
 * Inline-markdown renderers + round-context summarizer +
 * markdown-export helpers for the discussion subsystem.
 *
 * `.tsx` because `colorizeNumbers` and `renderInlineMarkdown` return
 * JSX (React nodes). The pure-logic summarizer/export helpers live
 * alongside them because they form the same "render a discussion as
 * text" presentation seam.
 */
import type { DiscussionDetail, Turn } from "@/types/discussion";
import { formatTaipeiDateCompact } from "./dates";
import { signedPct, toFixedSmart } from "./numbers";

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
        ? "text-up"
        : negative
          ? "text-down"
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

  /** When `backtest_news_unavailable`, the classified cause derived
   * from `ctx.news_backfill` (the auto-backfill diagnostic the backend
   * stamps onto the same ctx) so the renderer can explain WHY the
   * archive is empty — paywall / no news on that date / transient
   * lock / non-TW market / upstream error — instead of the generic
   * "archive predates our data" line, which is misleading for a paid
   * Sponsor whose only gap is the market-wide news tier. Absent on
   * snapshots captured before the diagnostic was wired. */
  news_backfill_reason?: "paywall" | "empty" | "lock" | "non_tw" | "error";
  /** Sanitised upstream error string for the `"error"` reason (the
   * FinMind token is already redacted server-side). */
  news_backfill_detail?: string;
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
    // Classify WHY using the auto-backfill diagnostic the backend
    // stamped onto the same ctx, so the renderer can explain a
    // paywall / lock / empty-date instead of the generic archive line.
    const backfill = ctx.news_backfill as Record<string, unknown> | null | undefined;
    if (backfill && typeof backfill === "object") {
      const err = typeof backfill.error === "string" ? backfill.error : "";
      const skipped = typeof backfill.skipped === "string" ? backfill.skipped : "";
      if (err && /paywall|sponsor|requires paid|your level|update your user level/i.test(err)) {
        out.news_backfill_reason = "paywall";
      } else if (err) {
        out.news_backfill_reason = "error";
        out.news_backfill_detail = err;
      } else if (skipped === "lock") {
        out.news_backfill_reason = "lock";
      } else if (skipped === "non-tw") {
        out.news_backfill_reason = "non_tw";
      } else if (backfill.covered === false) {
        out.news_backfill_reason = "empty";
      }
    }
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
