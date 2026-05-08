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
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Conclusion, DiscussionDetail, Turn } from "@/types/discussion";
import {
  buildDiscussionExportFilename,
  buildDiscussionMarkdown,
  formatCompactNumber,
  formatDiscussionTitle,
  latestNonNull,
  pctClass,
  readPostMortemResult,
  rememberPostMortemResult,
  runPostMortemFlowSteps,
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

  it("PR #276: prefers as_of_date over created_at for backtest discussions", () => {
    /* The previous behaviour displayed today's date in the sidebar
       even for a discussion backtesting 2026-01-15 — operators
       were confused which date was being analysed. Now backtest
       rows surface the as_of_date in the title; live rows
       (`as_of_date` null/undefined) fall back to created_at. */
    const out = formatDiscussionTitle({
      topic: "x",
      conclusion: baseConclusion,
      created_at: "2026-05-03T00:00:00Z",   // today, when the row was created
      as_of_date: "2026-01-15",              // the date being backtested
    });
    expect(out.date).toBe("20260115");
  });

  it("PR #276: live discussion (as_of_date=null) keeps created_at", () => {
    const out = formatDiscussionTitle({
      topic: "x",
      conclusion: baseConclusion,
      created_at: "2026-05-03T00:00:00Z",
      as_of_date: null,
    });
    expect(out.date).toBe("20260503");
  });

  it("PR #276: live discussion (as_of_date undefined) keeps created_at", () => {
    /* `as_of_date` is optional in the type — older sidebar rows
       loaded from a stale query cache may not carry the field
       at all. Make sure undefined behaves identically to null. */
    const out = formatDiscussionTitle({
      topic: "x",
      conclusion: baseConclusion,
      created_at: "2026-05-03T00:00:00Z",
    });
    expect(out.date).toBe("20260503");
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

  it("PR #278: prefers as_of_date over created_at when present", () => {
    /* Backtest prior discussions carry an `as_of_date` (the
       historical day they were analysing). The summary line
       should anchor on that, not on when the row was created. */
    const out = summarizeContext({
      prior_discussions: [
        {
          id: "a",
          created_at: "2026-05-03T10:00:00Z",   // today
          as_of_date: "2026-01-15",             // backtest anchor
          matched_symbols: ["2330"],
          time_horizon: "short_term",
          verdict: "win",
        },
      ],
    });
    expect(out.prior_discussions_summary).toContain(
      "2026-01-15 2330 → short_term/win",
    );
    expect(out.prior_discussions_summary).not.toContain("2026-05-03");
  });

  it("PR #278: falls back to created_at when as_of_date is null", () => {
    const out = summarizeContext({
      prior_discussions: [
        {
          id: "a",
          created_at: "2026-05-03T10:00:00Z",
          as_of_date: null,    // live discussion
          matched_symbols: ["2330"],
          time_horizon: "short_term",
          verdict: "win",
        },
      ],
    });
    expect(out.prior_discussions_summary).toContain(
      "2026-05-03 2330 → short_term/win",
    );
  });
});

// ── PR #268: post-mortem result localStorage persistence ─────────

describe("rememberPostMortemResult / readPostMortemResult", () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => localStorage.clear());

  const sampleData = {
    next_trading_day: "2026-04-16",
    top_gainers: [
      { symbol: "2330", change_pct: 5.23, close: 1100, base_close: 1045 },
      { symbol: "2317", change_pct: 4.10, close: 220, base_close: 211 },
    ],
    injected_turn_id: 42,
  };

  it("round-trips a post-mortem payload through localStorage", () => {
    rememberPostMortemResult("disc-A", sampleData);
    const out = readPostMortemResult("disc-A");
    expect(out).toEqual(sampleData);
  });

  it("isolates per-discussion storage so a second discussion doesn't leak the first", () => {
    rememberPostMortemResult("disc-A", sampleData);
    expect(readPostMortemResult("disc-B")).toBeNull();
  });

  it("returns null when nothing is stored for the requested discussion", () => {
    expect(readPostMortemResult("never-saved")).toBeNull();
  });

  it("returns null on corrupted storage instead of crashing the card", () => {
    localStorage.setItem(
      "discussion.postMortem.disc-bad", "not valid json {",
    );
    expect(readPostMortemResult("disc-bad")).toBeNull();
  });

  it("rejects payloads that don't match the expected shape", () => {
    /* A future schema bump could leave older entries on the user's
       machine — the read shouldn't crash, just degrade to null and
       wait for the next post-mortem run to overwrite the slot. */
    localStorage.setItem(
      "discussion.postMortem.disc-old",
      JSON.stringify({ unrelated_field: "x" }),
    );
    expect(readPostMortemResult("disc-old")).toBeNull();
  });

  it("survives localStorage being unavailable (private mode etc.)", () => {
    /* Best-effort writes shouldn't throw even when the storage
       throws on .setItem. Trace via a Proxy that throws on every
       method — the helper's internal try/catch must absorb it. */
    const realStorage = window.localStorage;
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      get() {
        throw new Error("storage unavailable");
      },
    });
    try {
      expect(() => rememberPostMortemResult("x", sampleData)).not.toThrow();
      expect(() => readPostMortemResult("x")).not.toThrow();
      expect(readPostMortemResult("x")).toBeNull();
    } finally {
      Object.defineProperty(window, "localStorage", {
        configurable: true,
        value: realStorage,
      });
    }
  });
});

// ── PR #279: runPostMortemFlowSteps orchestration ────────────────

describe("runPostMortemFlowSteps", () => {
  // Shared synchronous defer so each test can assert ordering
  // without fake-timer plumbing.
  const syncDefer = (fn: () => void) => fn();

  it("short-circuits and calls nothing when canStart returns false", async () => {
    const runPostMortem = vi.fn(async () => undefined);
    const runRound = vi.fn(async () => undefined);
    const runConclude = vi.fn();
    const onError = vi.fn();

    await runPostMortemFlowSteps({
      canStart: () => false,
      runPostMortem,
      runRound,
      runConclude,
      onError,
      defer: syncDefer,
    });

    expect(runPostMortem).not.toHaveBeenCalled();
    expect(runRound).not.toHaveBeenCalled();
    expect(runConclude).not.toHaveBeenCalled();
    expect(onError).not.toHaveBeenCalled();
  });

  it("runs all three steps in order on the happy path", async () => {
    const calls: string[] = [];
    const runPostMortem = vi.fn(async () => {
      calls.push("postMortem");
    });
    const runRound = vi.fn(async () => {
      calls.push("round");
    });
    const runConclude = vi.fn(() => {
      calls.push("conclude");
    });

    await runPostMortemFlowSteps({
      canStart: () => true,
      runPostMortem,
      runRound,
      runConclude,
      onError: vi.fn(),
      defer: syncDefer,
    });

    expect(calls).toEqual(["postMortem", "round", "conclude"]);
  });

  it("short-circuits round + conclude when post-mortem step fails", async () => {
    /* The post-mortem inject failing (e.g. discussion in `running`
       status, or backend-side connector outage) means there's no
       new user_input turn to reflect against. Steps 2 + 3 must
       NOT fire — the page surfaces the error banner instead. */
    const runPostMortem = vi.fn(async () => {
      throw new Error("upstream timeout");
    });
    const runRound = vi.fn(async () => undefined);
    const runConclude = vi.fn();
    const onError = vi.fn();

    await runPostMortemFlowSteps({
      canStart: () => true,
      runPostMortem,
      runRound,
      runConclude,
      onError,
      defer: syncDefer,
    });

    expect(runPostMortem).toHaveBeenCalledOnce();
    expect(runRound).not.toHaveBeenCalled();
    expect(runConclude).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledWith("upstream timeout");
  });

  it("prefers axios-style response.data.detail when present", async () => {
    /* The backend signals "post-mortem requires backtest mode" /
       "no ohlcv data in window" via `detail` on a 400. The flow
       helper must surface the human-readable detail string, not
       axios's generic `Request failed with status 400` message. */
    const runPostMortem = vi.fn(async () => {
      // axios-shaped error
      throw {
        message: "Request failed with status 400",
        response: { data: { detail: "post_mortem requires backtest mode" } },
      };
    });
    const onError = vi.fn();

    await runPostMortemFlowSteps({
      canStart: () => true,
      runPostMortem,
      runRound: vi.fn(async () => undefined),
      runConclude: vi.fn(),
      onError,
      defer: syncDefer,
    });

    expect(onError).toHaveBeenCalledWith(
      "post_mortem requires backtest mode",
    );
  });

  it("calls onSkipped + skips round/conclude when post-mortem returns status=skipped", async () => {
    /* Learning-loop addition: when the recommendation already cleared
       the win threshold the backend short-circuits — no critique
       round is needed. The flow helper must respect that and stop
       firing round + conclude, while letting the page surface a
       success badge via onSkipped. */
    const runPostMortem = vi.fn(async () => ({
      next_trading_day: "2026-03-24",
      top_gainers: [],
      status: "skipped" as const,
      verdict: {
        status: "win" as const,
        threshold_pct: 3.0,
        window_days: 5,
        winners: [{ symbol: "2330", peak_pct: 5.2, peak_day: "2026-03-26" }],
        best_pct: 5.2,
        reason: "ok",
      },
      injected_turn_id: null,
    }));
    const runRound = vi.fn(async () => undefined);
    const runConclude = vi.fn();
    const onSkipped = vi.fn();

    await runPostMortemFlowSteps({
      canStart: () => true,
      runPostMortem,
      runRound,
      runConclude,
      onError: vi.fn(),
      onSkipped,
      defer: syncDefer,
    });

    expect(runPostMortem).toHaveBeenCalledOnce();
    expect(runRound).not.toHaveBeenCalled();
    expect(runConclude).not.toHaveBeenCalled();
    expect(onSkipped).toHaveBeenCalledOnce();
    expect(onSkipped.mock.calls[0][0]?.best_pct).toBe(5.2);
  });

  it("falls through to the run/conclude chain when status is missing or 'ran'", async () => {
    /* Older backends (pre-learning-loop) omit the status field;
       newer backends emit "ran" explicitly. Both paths must run
       the full 3-step chain. */
    const runPostMortem = vi.fn(async () => ({
      next_trading_day: "2026-03-24",
      top_gainers: [],
      injected_turn_id: 42,
      // no status field at all
    }));
    const runRound = vi.fn(async () => undefined);
    const runConclude = vi.fn();
    const onSkipped = vi.fn();

    await runPostMortemFlowSteps({
      canStart: () => true,
      runPostMortem,
      runRound,
      runConclude,
      onError: vi.fn(),
      onSkipped,
      defer: syncDefer,
    });

    expect(runRound).toHaveBeenCalledOnce();
    expect(runConclude).toHaveBeenCalledOnce();
    expect(onSkipped).not.toHaveBeenCalled();
  });

  it("defers conclude through the defer hook (real setTimeout in production)", async () => {
    /* Production wiring uses `setTimeout(fn, 0)` so React state
       updates from runRound flush before the synthesizer fetch.
       Test by injecting a defer that we control + asserting it
       was the conclude that ran inside it. */
    let captured: (() => void) | null = null;
    const customDefer = (fn: () => void) => {
      captured = fn;
    };
    const runConclude = vi.fn();

    await runPostMortemFlowSteps({
      canStart: () => true,
      runPostMortem: vi.fn(async () => undefined),
      runRound: vi.fn(async () => undefined),
      runConclude,
      onError: vi.fn(),
      defer: customDefer,
    });

    // Conclude not yet fired — it's queued in the deferred fn.
    expect(runConclude).not.toHaveBeenCalled();
    expect(captured).not.toBeNull();
    // Drain the deferred fn — conclude runs.
    captured!();
    expect(runConclude).toHaveBeenCalledOnce();
  });
});

// ── buildDiscussionExportFilename ────────────────────────────────

describe("buildDiscussionExportFilename", () => {
  it("uses YYYYMMDD_R{rounds}_{personas}.md format with as_of_date", () => {
    const out = buildDiscussionExportFilename({
      as_of_date: "2026-05-08",
      created_at: "2026-05-09T03:00:00Z",
      current_round: 5,
      persona_ids: ["a", "b", "c", "d", "e", "f", "g", "h"],
    });
    expect(out).toBe("20260508_R5_8.md");
  });

  it("falls back to created_at (Asia/Taipei) when as_of_date is null", () => {
    // 2026-05-08 03:00 UTC = 2026-05-08 11:00 Taipei (UTC+8) → 20260508
    const out = buildDiscussionExportFilename({
      as_of_date: null,
      created_at: "2026-05-08T03:00:00Z",
      current_round: 3,
      persona_ids: ["a", "b"],
    });
    expect(out).toBe("20260508_R3_2.md");
  });

  it("rolls Taipei date forward across UTC midnight", () => {
    // 2026-05-07 17:00 UTC = 2026-05-08 01:00 Taipei → 20260508
    const out = buildDiscussionExportFilename({
      as_of_date: null,
      created_at: "2026-05-07T17:00:00Z",
      current_round: 1,
      persona_ids: ["x"],
    });
    expect(out).toBe("20260508_R1_1.md");
  });

  it("falls back to max(turn.round) when current_round is 0 but turns exist", () => {
    const turns: Turn[] = [
      { id: 1, round: 1, turn_index: 0, persona_id: "a", stance: "agree", content: "", created_at: "" },
      { id: 2, round: 2, turn_index: 0, persona_id: "a", stance: "agree", content: "", created_at: "" },
    ];
    const out = buildDiscussionExportFilename({
      as_of_date: "2026-05-08",
      created_at: "2026-05-08T00:00:00Z",
      current_round: 0,
      persona_ids: ["a"],
      turns,
    });
    expect(out).toBe("20260508_R2_1.md");
  });

  it("emits R0 when no rounds and no turns yet", () => {
    const out = buildDiscussionExportFilename({
      as_of_date: "2026-05-08",
      created_at: "2026-05-08T00:00:00Z",
      current_round: 0,
      persona_ids: ["a", "b", "c"],
    });
    expect(out).toBe("20260508_R0_3.md");
  });
});

// ── buildDiscussionMarkdown ──────────────────────────────────────

describe("buildDiscussionMarkdown", () => {
  const personaName = (id: string) => `Persona ${id}`;
  const detail: DiscussionDetail = {
    id: "d1",
    topic: "本週台股短線",
    rules: "規則 1\n規則 2",
    persona_ids: ["a", "b"],
    market: "TW",
    status: "done",
    current_round: 1,
    conclusion: {
      recommended_symbols: ["2330"],
      reasoning: "理由",
      risks: ["風險 1"],
      time_horizon: "short_term",
      consensus_score: 0.75,
    },
    created_at: "2026-05-08T00:00:00Z",
    updated_at: "2026-05-08T00:00:00Z",
    turns: [
      { id: 1, round: 1, turn_index: 0, persona_id: "a", stance: "agree", content: "同意。", created_at: "2026-05-08T00:01:00Z" },
      { id: 2, round: 1, turn_index: 1, persona_id: "b", stance: "dissent", content: "我反對。", created_at: "2026-05-08T00:02:00Z" },
      { id: 3, round: 1, turn_index: 2, persona_id: "a", stance: "user_input", content: "請聚焦半導體。", created_at: "2026-05-08T00:03:00Z" },
    ],
  };

  it("includes topic, rules, personas, every turn, and conclusion", () => {
    const md = buildDiscussionMarkdown(detail, personaName);
    expect(md).toContain("# 本週台股短線");
    expect(md).toContain("## 共同規則");
    expect(md).toContain("規則 1\n規則 2");
    expect(md).toContain("- Persona a (a)");
    expect(md).toContain("- Persona b (b)");
    expect(md).toContain("## 第 1 輪");
    expect(md).toContain("✓ 同意");
    expect(md).toContain("✗ 異議");
    expect(md).toContain("✎ 插話");
    expect(md).toContain("我反對。");
    expect(md).toContain("請聚焦半導體。");
    expect(md).toContain("## 結論");
    expect(md).toContain("- 推薦標的：2330");
    expect(md).toContain("- 共識度：75%");
    expect(md).toContain("### 風險");
    expect(md).toContain("- 風險 1");
  });

  it("renders turns ordered by (round, turn_index)", () => {
    // Shuffled input — buildDiscussionMarkdown must re-sort.
    const shuffled: DiscussionDetail = {
      ...detail,
      current_round: 2,
      turns: [
        { id: 4, round: 2, turn_index: 0, persona_id: "a", stance: "supplement", content: "R2 first", created_at: "" },
        { id: 1, round: 1, turn_index: 1, persona_id: "b", stance: "agree", content: "R1 second", created_at: "" },
        { id: 2, round: 1, turn_index: 0, persona_id: "a", stance: "agree", content: "R1 first", created_at: "" },
      ],
    };
    const md = buildDiscussionMarkdown(shuffled, personaName);
    const idxR1First = md.indexOf("R1 first");
    const idxR1Second = md.indexOf("R1 second");
    const idxR2First = md.indexOf("R2 first");
    expect(idxR1First).toBeGreaterThan(-1);
    expect(idxR1Second).toBeGreaterThan(idxR1First);
    expect(idxR2First).toBeGreaterThan(idxR1Second);
  });

  it("renders an empty-content agree turn as the placeholder line", () => {
    const empty: DiscussionDetail = {
      ...detail,
      conclusion: null,
      turns: [
        { id: 1, round: 1, turn_index: 0, persona_id: "a", stance: "agree", content: "", created_at: "" },
      ],
    };
    const md = buildDiscussionMarkdown(empty, personaName);
    expect(md).toContain("_（同意，無補充）_");
  });

  it("omits the conclusion section when there is no conclusion", () => {
    const noConc: DiscussionDetail = { ...detail, conclusion: null };
    const md = buildDiscussionMarkdown(noConc, personaName);
    expect(md).not.toContain("## 結論");
  });
});
