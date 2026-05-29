import { describe, expect, it } from "vitest";
import {
  formatCompact,
  formatNumber,
  formatPct,
  formatProgressPct,
} from "./formatters";

describe("formatPct", () => {
  it("prefixes positive values with + by default", () => {
    expect(formatPct(9.93)).toBe("+9.93%");
  });

  it("preserves the leading minus on negatives", () => {
    expect(formatPct(-2.5)).toBe("-2.50%");
  });

  it("multiplies by 100 when alreadyPct=false", () => {
    expect(formatPct(0.0993, { alreadyPct: false })).toBe("+9.93%");
  });

  it("treats backend percent units as already-percent by default — guards the PR #156 regression", () => {
    // 9.93 means +9.93%, not 0.0993. Default `alreadyPct=true` keeps
    // it from re-multiplying into "+993%".
    expect(formatPct(9.93)).toBe("+9.93%");
  });

  it("omits the sign when signed=false", () => {
    expect(formatPct(15.2, { signed: false })).toBe("15.20%");
    expect(formatPct(-3.4, { signed: false })).toBe("-3.40%");
  });

  it("respects custom decimals", () => {
    expect(formatPct(9.9301, { decimals: 4 })).toBe("+9.9301%");
  });

  it("returns em-dash for null / undefined", () => {
    expect(formatPct(null)).toBe("—");
    expect(formatPct(undefined)).toBe("—");
  });

  it("renders zero with sign", () => {
    expect(formatPct(0)).toBe("+0.00%");
    expect(formatPct(0, { signed: false })).toBe("0.00%");
  });
});

describe("formatNumber", () => {
  it("uses locale thousands separators", () => {
    expect(formatNumber(1234567.89)).toMatch(/^1[,.]234[,.]567[,.]89$/);
  });

  it("respects decimals (rounds 1234.5 → '1,235' or locale equivalent)", () => {
    expect(formatNumber(1234.5, 0)).toMatch(/^1[,.  ]?235$/);
  });

  it("returns em-dash for null", () => {
    expect(formatNumber(null)).toBe("—");
  });
});

describe("formatCompact", () => {
  it("uses B for billions", () => {
    expect(formatCompact(1.5e9)).toBe("1.50B");
  });

  it("uses M for millions", () => {
    expect(formatCompact(2.4e6)).toBe("2.4M");
  });

  it("uses K for thousands", () => {
    expect(formatCompact(15_000)).toBe("15K");
  });

  it("returns the raw number string for small values", () => {
    expect(formatCompact(42)).toBe("42");
  });

  it("returns em-dash for null", () => {
    expect(formatCompact(null)).toBe("—");
  });
});

describe("formatProgressPct", () => {
  it("rounds num/denom to an integer percent", () => {
    expect(formatProgressPct(7, 12)).toBe("58%");
    expect(formatProgressPct(1, 4)).toBe("25%");
  });

  it("returns 0% when denom is non-positive — guards NaN%", () => {
    expect(formatProgressPct(0, 0)).toBe("0%");
    expect(formatProgressPct(5, -1)).toBe("0%");
  });

  it("renders complete progress as 100%", () => {
    expect(formatProgressPct(10, 10)).toBe("100%");
  });
});
