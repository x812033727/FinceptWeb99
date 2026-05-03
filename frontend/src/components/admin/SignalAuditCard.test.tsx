import { describe, expect, it } from "vitest";
import { rateColor, rateLabel } from "./SignalAuditCard";

describe("rateColor", () => {
  it("returns red for zero-uptake (< 10 %)", () => {
    // The single most actionable bucket — admin's eyes should be
    // drawn to these rows. Any drift in the threshold (e.g. lowering
    // to < 5 %) would silently let near-zero signals slip out of the
    // red bucket and dilute the "fix me first" signal.
    expect(rateColor(0)).toBe("bg-red-500/70");
    expect(rateColor(0.05)).toBe("bg-red-500/70");
    expect(rateColor(0.099)).toBe("bg-red-500/70");
  });

  it("returns yellow for under-utilised (10 % ≤ rate < 50 %)", () => {
    expect(rateColor(0.1)).toBe("bg-yellow-500/70");
    expect(rateColor(0.25)).toBe("bg-yellow-500/70");
    expect(rateColor(0.499)).toBe("bg-yellow-500/70");
  });

  it("returns emerald for healthy (≥ 50 %)", () => {
    expect(rateColor(0.5)).toBe("bg-emerald-500/70");
    expect(rateColor(0.638)).toBe("bg-emerald-500/70");
    expect(rateColor(1.0)).toBe("bg-emerald-500/70");
  });

  it("threshold boundaries are inclusive of the higher bucket", () => {
    // Pin the inclusive-edge contract so a future refactor can't
    // accidentally swap < for <= and shift a row's colour.
    expect(rateColor(0.1)).toBe("bg-yellow-500/70");
    expect(rateColor(0.5)).toBe("bg-emerald-500/70");
  });
});

describe("rateLabel", () => {
  it("formats to one decimal place with % suffix", () => {
    expect(rateLabel(0)).toBe("0.0%");
    expect(rateLabel(1)).toBe("100.0%");
    expect(rateLabel(0.6383)).toBe("63.8%");
    // Round-half-to-even (toFixed semantics) — pin so a refactor
    // to manual rounding doesn't introduce off-by-one in the UI.
    expect(rateLabel(0.085)).toBe("8.5%");
  });

  it("handles tiny non-zero rates without dropping to '0%'", () => {
    // A signal cited 1/200 times = 0.5 % — must NOT collapse to
    // "0.0%" which would mislead operators into thinking it's a
    // zero-uptake offender. `(0.005 * 100).toFixed(1) = "0.5"`.
    expect(rateLabel(0.005)).toBe("0.5%");
  });
});
