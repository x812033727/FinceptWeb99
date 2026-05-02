/**
 * Tests for the formatter / scoreboard / context helpers extracted in
 * Tier-3 A4 (PR #175). The focus is `formatDiscussionTitle` — its
 * verdict / win-threshold / day-1 fallback / per-day strip logic is
 * the single richest pure function in the discussion subsystem and
 * it had zero direct coverage before.
 *
 * The smaller helpers (`signedPct`, `toFixedSmart`, `latestNonNull`,
 * `formatCompactNumber`) are also exercised so a silent re-tune
 * (e.g. dropping the trailing-zero strip) is caught.
 */
import { describe, expect, it } from "vitest";
import type { Conclusion } from "@/types/discussion";
import {
  formatCompactNumber,
  formatDiscussionTitle,
  latestNonNull,
  pctClass,
  signedPct,
  signedPctSafe,
  summarizeContext,
  toFixedSmart,
} from "./_helpers";

// ── number formatters ─────────────────────────────────────────────

describe("toFixedSmart", () => {
  it("strips trailing zeros on integer-valued numbers", () => {
    expect(toFixedSmart(55)).toBe("55");
    expect(toFixedSmart(55.0)).toBe("55");
  });

  it("preserves up to 2 decimals on fractional numbers", () => {
    expect(toFixedSmart(55.4)).toBe("55.4");
    expect(toFixedSmart(55.45)).toBe("55.45");
  });

  it("rounds to 2 decimals", () => {
    expect(toFixedSmart(55.456)).toBe("55.46");
  });
});

describe("signedPct", () => {
  it("multiplies by 100 and adds + on positive", () => {
    expect(signedPct(0.05)).toBe("+5.0%");
  });

  it("preserves the - on negative without re-prefixing", () => {
    expect(signedPct(-0.0234)).toBe("-2.3%");
  });

  it("renders zero with + sign", () => {
    expect(signedPct(0)).toBe("+0.0%");
  });
});

describe("signedPctSafe", () => {
  it("returns em-dash for null", () => {
    expect(signedPctSafe(null)).toBe("—");
    expect(signedPctSafe(undefined)).toBe("—");
  });

  it("uses 2 decimals (vs signedPct's 1) and signed", () => {
    expect(signedPctSafe(0.0512)).toBe("+5.12%");
    expect(signedPctSafe(-0.0099)).toBe("-0.99%");
  });
});

describe("pctClass", () => {
  it("muted for null", () => {
    expect(pctClass(null)).toBe("text-muted-foreground");
    expect(pctClass(undefined)).toBe("text-muted-foreground");
  });

  it("green for non-negative", () => {
    expect(pctClass(0)).toBe("text-green-500");
    expect(pctClass(0.05)).toBe("text-green-500");
  });

  it("red for negative", () => {
    expect(pctClass(-0.001)).toBe("text-red-500");
  });
});

describe("latestNonNull", () => {
  it("returns the last non-null entry", () => {
    expect(latestNonNull([1, 2, null, 3, null])).toBe(3);
  });

  it("returns null when the array is all-null", () => {
    expect(latestNonNull([null, null, null])).toBeNull();
  });

  it("handles undefined / null / empty", () => {
    expect(latestNonNull(null)).toBeNull();
    expect(latestNonNull(undefined)).toBeNull();
    expect(latestNonNull([])).toBeNull();
  });
});

describe("formatCompactNumber", () => {
  it("uses 億 suffix for 1e8+", () => {
    expect(formatCompactNumber(2.5e8)).toBe("2.50 億");
  });

  it("uses 萬 suffix for 1e4–1e8", () => {
    expect(formatCompactNumber(123_456)).toBe("12.3 萬");
  });

  it("falls back to plain locale string for small values", () => {
    expect(formatCompactNumber(999)).toBe("999");
  });

  it("preserves sign on negatives (TW conventions show 負金額 with leading -)", () => {
    expect(formatCompactNumber(-3.4e8)).toBe("-3.40 億");
  });
});

// ── formatDiscussionTitle — the rich one ─────────────────────────

const baseConclusion: Conclusion = {
  recommended_symbols: ["2330"],
  reasoning: "demo",
  risks: [],
  time_horizon: "short_term",
  consensus_score: 0.8,
};

describe("formatDiscussionTitle", () => {
  it("falls back to the user-typed topic when no conclusion exists", () => {
    const out = formatDiscussionTitle({
      topic: "本週台股展望",
      conclusion: null,
      created_at: "2025-05-01T00:00:00Z",
    });
    expect(out.text).toBe("本週台股展望");
    expect(out.lines).toBeUndefined();
  });

  it("falls back to topic when the conclusion has no recommended_symbols", () => {
    const out = formatDiscussionTitle({
      topic: "no picks today",
      conclusion: { ...baseConclusion, recommended_symbols: [] },
      created_at: "2025-05-01T00:00:00Z",
    });
    expect(out.text).toBe("no picks today");
  });

  it("renders compact YYYYMMDD in Asia/Taipei", () => {
    // 2025-05-01 16:00 UTC = 2025-05-02 00:00 in Taipei
    const out = formatDiscussionTitle({
      topic: "x",
      conclusion: baseConclusion,
      created_at: "2025-05-01T16:00:00Z",
    });
    expect(out.date).toBe("20250502");
  });

  it("marks 勝 in green when verdict=win", () => {
    const out = formatDiscussionTitle({
      topic: "x",
      conclusion: baseConclusion,
      verdict: "win",
      created_at: "2025-05-01T00:00:00Z",
    });
    expect(out.verdictMark).toBe("勝");
    expect(out.verdictCls).toBe("text-green-500");
  });

  it("marks 敗 in red when verdict=loss", () => {
    const out = formatDiscussionTitle({
      topic: "x",
      conclusion: baseConclusion,
      verdict: "loss",
      created_at: "2025-05-01T00:00:00Z",
    });
    expect(out.verdictMark).toBe("敗");
    expect(out.verdictCls).toBe("text-red-500");
  });

  it("uses muted styling when verdict=unverifiable", () => {
    const out = formatDiscussionTitle({
      topic: "x",
      conclusion: baseConclusion,
      verdict: "unverifiable",
      created_at: "2025-05-01T00:00:00Z",
    });
    expect(out.verdictMark).toBe("");
    expect(out.verdictCls).toBe("text-muted-foreground");
  });

  it("computes per-day change_pct against day-1 open and applies the +3% win threshold (green)", () => {
    const out = formatDiscussionTitle({
      topic: "x",
      conclusion: { ...baseConclusion, recommended_symbols: ["2330"] },
      day1_open_prices: { "2330": 600 },
      // D1 close +0.33%, D2 +0.83%, …, D5 = 618 → +3.00% exactly = WIN
      daily_close_prices: { "2330": [602, 605, 610, 615, 618] },
      created_at: "2025-05-01T00:00:00Z",
    });
    const line = out.lines![0];
    expect(line.symbol).toBe("2330");
    expect(line.cls).toBe("text-green-500");
    // (618 - 600) / 600 = 0.03 → matches the WIN_THRESHOLD edge
    expect(line.changePcts[4]).toBeCloseTo(0.03, 6);
  });

  it("renders red when no day's close beats the +3% win threshold", () => {
    const out = formatDiscussionTitle({
      topic: "x",
      conclusion: { ...baseConclusion, recommended_symbols: ["2330"] },
      day1_open_prices: { "2330": 600 },
      daily_close_prices: { "2330": [602, 605, 610, 615, 617] }, // max 2.83 %
      created_at: "2025-05-01T00:00:00Z",
    });
    expect(out.lines![0].cls).toBe("text-red-500");
  });

  it("renders muted when every change_pct is null (no resolved closes)", () => {
    const out = formatDiscussionTitle({
      topic: "x",
      conclusion: { ...baseConclusion, recommended_symbols: ["2330"] },
      day1_open_prices: { "2330": 600 },
      daily_close_prices: { "2330": [null, null, null, null, null] },
      created_at: "2025-05-01T00:00:00Z",
    });
    expect(out.lines![0].cls).toBe("text-muted-foreground");
    expect(out.lines![0].changePcts).toEqual([null, null, null, null, null]);
  });

  it("falls back to the legacy day5_close_prices column when daily_close_prices is missing", () => {
    // Pre-PR #140 rows only had day5_close_prices (a single bar).
    // Result: a sparse [None × 4, day5] strip.
    const out = formatDiscussionTitle({
      topic: "x",
      conclusion: { ...baseConclusion, recommended_symbols: ["2330"] },
      day1_open_prices: { "2330": 600 },
      day5_close_prices: { "2330": 618 },
      // daily_close_prices intentionally omitted
      created_at: "2025-05-01T00:00:00Z",
    });
    const line = out.lines![0];
    expect(line.changePcts.slice(0, 4)).toEqual([null, null, null, null]);
    expect(line.changePcts[4]).toBeCloseTo(0.03, 6);
  });

  it("caps at 3 symbols even when conclusion recommends more", () => {
    const out = formatDiscussionTitle({
      topic: "x",
      conclusion: {
        ...baseConclusion,
        recommended_symbols: ["2330", "2317", "2454", "2412", "2308"],
      },
      created_at: "2025-05-01T00:00:00Z",
    });
    expect(out.lines).toHaveLength(3);
    expect(out.lines!.map((l) => l.symbol)).toEqual(["2330", "2317", "2454"]);
  });

  it("yields null change_pct for a symbol missing from day1_open_prices", () => {
    const out = formatDiscussionTitle({
      topic: "x",
      conclusion: { ...baseConclusion, recommended_symbols: ["2330"] },
      day1_open_prices: {}, // 2330 absent
      daily_close_prices: { "2330": [602, 605, 610, 615, 618] },
      created_at: "2025-05-01T00:00:00Z",
    });
    expect(out.lines![0].changePcts).toEqual([null, null, null, null, null]);
  });

  it("yields null change_pct when day-1 open is zero (defensive divide-by-zero guard)", () => {
    const out = formatDiscussionTitle({
      topic: "x",
      conclusion: { ...baseConclusion, recommended_symbols: ["2330"] },
      day1_open_prices: { "2330": 0 },
      daily_close_prices: { "2330": [602, 605, 610, 615, 618] },
      created_at: "2025-05-01T00:00:00Z",
    });
    expect(out.lines![0].changePcts).toEqual([null, null, null, null, null]);
  });
});

// ── summarizeContext (round-context replay summary) ───────────────

describe("summarizeContext", () => {
  it("returns an empty summary on empty context", () => {
    expect(summarizeContext({})).toEqual({});
  });

  it("extracts taiex value + computes history change_pct from first/last close", () => {
    const out = summarizeContext({
      index: {
        value: 17500.5,
        history: [{ close: 17000 }, { close: 17500 }],
      },
    });
    expect(out.taiex_value).toBe(17500.5);
    expect(out.taiex_history_change_pct).toBeCloseTo(0.0294, 4);
  });

  it("skips taiex_history_change_pct when first close is zero (defensive)", () => {
    const out = summarizeContext({
      index: { value: 100, history: [{ close: 0 }, { close: 50 }] },
    });
    expect(out.taiex_value).toBe(100);
    expect(out.taiex_history_change_pct).toBeUndefined();
  });

  it("threads news_sentiment + international_sentiment counts through", () => {
    const out = summarizeContext({
      news_sentiment: { bullish: 7, bearish: 3 },
      international_sentiment: { bullish: 2, bearish: 5 },
    });
    expect(out.news_bullish).toBe(7);
    expect(out.news_bearish).toBe(3);
    expect(out.intl_bullish).toBe(2);
    expect(out.intl_bearish).toBe(5);
  });

  it("picks the first foreign buyer + revenue grower (already pre-sorted upstream)", () => {
    const out = summarizeContext({
      top_foreign_buyers: [
        { symbol: "2330", industry: "半導體業", net_foreign_buy: 1.2e8 },
        { symbol: "2317", industry: "其他", net_foreign_buy: 5e7 },
      ],
      top_revenue_growers: [
        { symbol: "8069", industry: "電子零組件業", revenue_yoy: 45.6 },
      ],
    });
    expect(out.top_foreign_buyer).toEqual({
      symbol: "2330",
      industry: "半導體業",
      net: 1.2e8,
    });
    expect(out.top_revenue_grower).toEqual({
      symbol: "8069",
      industry: "電子零組件業",
      yoy: 45.6,
    });
  });

  // ── new ctx blocks (PR #213) ────────────────────────────────────

  it("renders macro headline from fed_funds + dxy summaries", () => {
    const out = summarizeContext({
      macro: {
        fed_funds_rate: { summary: { latest_value: 4.25, change_1y: -0.75 } },
        usd_index: { summary: { latest_value: 105.2, change_1y: 6.0 } },
      },
    });
    expect(out.macro_summary).toContain("Fed 4.25%");
    expect(out.macro_summary).toContain("(-0.75 YoY)");
    expect(out.macro_summary).toContain("DXY 105.2");
    expect(out.macro_summary).toContain("(+6 YoY)");
  });

  it("skips macro_summary when both summaries are null", () => {
    const out = summarizeContext({
      macro: {
        fed_funds_rate: { summary: null },
        usd_index: { summary: null },
      },
    });
    expect(out.macro_summary).toBeUndefined();
  });

  it("emits one focus_briefs line per symbol with quote / pe / rsi", () => {
    const out = summarizeContext({
      focus_briefs: [
        {
          symbol: "2330",
          name_zh: "台積電",
          quote: { price: 950, change_pct: 1.2 },
          fundamentals: { pe: 22.5 },
          technicals: { rsi14: 62 },
        },
        {
          symbol: "2454",
          name_zh: "聯發科",
          quote: { price: 1200, change_pct: -0.5 },
          fundamentals: { pe: 18 },
          technicals: { rsi14: 45 },
        },
      ],
    });
    expect(out.focus_briefs_summary).toHaveLength(2);
    expect(out.focus_briefs_summary![0]).toContain("2330 台積電");
    expect(out.focus_briefs_summary![0]).toContain("PE 22.5");
    expect(out.focus_briefs_summary![0]).toContain("RSI 62");
    expect(out.focus_briefs_summary![1]).toContain("2454");
  });

  it("collapses user_context to portfolios/holdings/watchlist counts + overlap", () => {
    const out = summarizeContext({
      user_context: {
        portfolios: [{ name: "Main" }, { name: "USD" }],
        holdings: Array.from({ length: 5 }, (_, i) => ({ symbol: String(i) })),
        watchlist_symbols: [{ symbol: "NVDA" }, { symbol: "AAPL" }],
        focus_overlap: { held: ["2330"], watching: ["NVDA"] },
      },
    });
    expect(out.user_context_summary).toContain("2p");
    expect(out.user_context_summary).toContain("5h");
    expect(out.user_context_summary).toContain("2w");
    expect(out.user_context_summary).toContain("held: 2330");
    expect(out.user_context_summary).toContain("watch: NVDA");
  });

  it("omits user_context_summary when block is null / empty arrays", () => {
    expect(summarizeContext({ user_context: null }).user_context_summary).toBeUndefined();
    expect(
      summarizeContext({
        user_context: {
          portfolios: [], holdings: [], watchlist_symbols: [],
          focus_overlap: { held: [], watching: [] },
        },
      }).user_context_summary,
    ).toBeUndefined();
  });

  it("renders prior_discussions as date · symbols → horizon/verdict per row, max 3", () => {
    const out = summarizeContext({
      prior_discussions: [
        {
          id: "a", created_at: "2026-04-22T00:00:00Z",
          matched_symbols: ["2330"], time_horizon: "short_term", verdict: "win",
        },
        {
          id: "b", created_at: "2026-04-15T00:00:00Z",
          matched_symbols: ["2330"], time_horizon: "medium_term", verdict: null,
        },
        {
          id: "c", created_at: "2026-04-08T00:00:00Z",
          matched_symbols: ["2330"], time_horizon: "short_term", verdict: "loss",
        },
        {
          id: "d", created_at: "2026-04-01T00:00:00Z",
          matched_symbols: ["2330"], time_horizon: "long_term", verdict: "win",
        },
      ],
    });
    // Cap at 3 rows.
    expect(out.prior_discussions_summary).toBeDefined();
    expect(out.prior_discussions_summary!.split(" | ")).toHaveLength(3);
    expect(out.prior_discussions_summary!).toContain("2026-04-22 2330 → short_term/win");
    expect(out.prior_discussions_summary!).toContain("2026-04-15 2330 → medium_term/?");
  });

  it("skips prior_discussions_summary when block missing or empty", () => {
    expect(summarizeContext({}).prior_discussions_summary).toBeUndefined();
    expect(summarizeContext({ prior_discussions: [] }).prior_discussions_summary).toBeUndefined();
  });
});
