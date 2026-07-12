/**
 * Per-symbol outcome-band classification + dynamic discussion-title
 * assembly for the discussion subsystem. Pure logic — no JSX (the band
 * labels are plain strings + class names, consumed by renderers
 * elsewhere).
 */
import type { Conclusion } from "@/types/discussion";
import { formatTaipeiDateCompact } from "./dates";

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

// 4-band verdict label + colour table (大勝/勝/大敗/敗). Legacy
// "win"/"loss" rows fall into the same buckets via the matching key
// so historical discussions keep their badge.
export const BAND_LABELS: Record<
  string,
  { mark: string; cls: string }
> = {
  big_win: { mark: "大勝", cls: "text-up" },
  win: { mark: "勝", cls: "text-up" },
  big_loss: { mark: "大敗", cls: "text-down" },
  loss: { mark: "敗", cls: "text-warning" },
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
