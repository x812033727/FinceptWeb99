import { describe, expect, it } from "vitest";
import { dedupeBySymbol } from "./dailyCandidates";

describe("dedupeBySymbol", () => {
  it("keeps the highest-scored entry per symbol and sorts descending", () => {
    const items = [
      { symbol: "2330", strategy_score: 10 },
      { symbol: "1101", strategy_score: 20, signal_type: "oversold" },
      { symbol: "2330", strategy_score: 15, signal_type: "breakout" },
      { symbol: undefined, strategy_score: 99 },
    ];
    expect(dedupeBySymbol(items)).toEqual([
      { symbol: "1101", strategy_score: 20, signal_type: "oversold" },
      { symbol: "2330", strategy_score: 15, signal_type: "breakout" },
    ]);
  });

  it("tolerates entries without scores", () => {
    expect(dedupeBySymbol([{ symbol: "2330" }, { symbol: "2330", strategy_score: 1 }])).toEqual([
      { symbol: "2330", strategy_score: 1 },
    ]);
  });
});
