import { describe, expect, it } from "vitest";
import { categoricalSpread, pearsonColor } from "./SignalQualityCard";

describe("pearsonColor", () => {
  it("returns muted for null (n<3 or zero variance)", () => {
    expect(pearsonColor(null)).toContain("muted-foreground");
  });

  it("returns muted for noise (|r| < 0.1)", () => {
    expect(pearsonColor(0)).toContain("muted-foreground");
    expect(pearsonColor(0.05)).toContain("muted-foreground");
    expect(pearsonColor(-0.099)).toContain("muted-foreground");
  });

  it("returns yellow for weak correlation (0.1 <= |r| < 0.3)", () => {
    expect(pearsonColor(0.1)).toContain("yellow");
    expect(pearsonColor(0.25)).toContain("yellow");
    expect(pearsonColor(-0.299)).toContain("yellow");
  });

  it("returns emerald for positive directional (|r| >= 0.3, r > 0)", () => {
    expect(pearsonColor(0.3)).toContain("emerald");
    expect(pearsonColor(0.7)).toContain("emerald");
    expect(pearsonColor(1.0)).toContain("emerald");
  });

  it("returns orange for negative directional (|r| >= 0.3, r < 0)", () => {
    // Negative correlation is just as informative as positive
    // (it's a contrarian signal). Visual distinction matters because
    // operators interpret the signal differently — orange flags
    // "fade this" rather than "follow this".
    expect(pearsonColor(-0.3)).toContain("orange");
    expect(pearsonColor(-0.7)).toContain("orange");
  });
});

describe("categoricalSpread", () => {
  it("returns 0 when fewer than 2 buckets (no comparison possible)", () => {
    expect(categoricalSpread({
      label: "x", path: "x", n_total: 5,
      buckets: [{ category: "a", n: 5, mean_d5_return: 1.0, median_d5_return: 1.0 }],
    })).toBe(0);
    expect(categoricalSpread({
      label: "x", path: "x", n_total: 0, buckets: [],
    })).toBe(0);
  });

  it("returns max-min mean across buckets", () => {
    // Spread = (+1.5) - (-0.8) = 2.3 — the bullish-bucket's
    // "extra" return over the bearish-bucket. Higher spread =
    // more informative signal (categories separate the outcomes).
    expect(categoricalSpread({
      label: "x", path: "x", n_total: 10,
      buckets: [
        { category: "bullish", n: 4, mean_d5_return: 1.5,  median_d5_return: 1.2 },
        { category: "neutral", n: 2, mean_d5_return: 0.1,  median_d5_return: 0.0 },
        { category: "bearish", n: 4, mean_d5_return: -0.8, median_d5_return: -0.5 },
      ],
    })).toBeCloseTo(2.3, 5);
  });

  it("handles all-positive or all-negative bucket means", () => {
    // Even when no bucket sign-flips, spread tells you HOW MUCH
    // the signal separates outcomes within the same direction.
    expect(categoricalSpread({
      label: "x", path: "x", n_total: 5,
      buckets: [
        { category: "a", n: 3, mean_d5_return: 2.0, median_d5_return: 2.0 },
        { category: "b", n: 2, mean_d5_return: 0.5, median_d5_return: 0.5 },
      ],
    })).toBe(1.5);
  });
});
