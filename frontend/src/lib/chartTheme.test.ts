import { describe, it, expect, beforeEach } from "vitest";
import { hslVarToRgb, getChartTheme } from "./chartTheme";

const VARS = [
  "--up",
  "--down",
  "--flat",
  "--border",
  "--muted-foreground",
  "--chart-1",
  "--chart-2",
  "--chart-3",
  "--chart-4",
  "--chart-5",
  "--chart-6",
];

describe("hslVarToRgb", () => {
  it("converts shadcn-style HSL components to an rgb() string", () => {
    // hsl(0 100% 50%) = pure red
    expect(hslVarToRgb("0 100% 50%")).toBe("rgb(255, 0, 0)");
    // hsl(120 100% 50%) = pure green
    expect(hslVarToRgb("120 100% 50%")).toBe("rgb(0, 255, 0)");
    // hsl(222 47% 8%) = --background (dark) ≈ #0b111e
    expect(hslVarToRgb("222 47% 8%")).toBe("rgb(11, 17, 30)");
  });

  it("falls back to mid-grey on unparseable input", () => {
    expect(hslVarToRgb("")).toBe("rgb(128, 128, 128)");
    expect(hslVarToRgb("199 89%")).toBe("rgb(128, 128, 128)");
  });
});

describe("getChartTheme", () => {
  beforeEach(() => {
    // jsdom has no stylesheet cascade — stamp the vars inline on <html>
    // (getComputedStyle surfaces inline custom properties verbatim).
    for (const name of VARS) {
      document.documentElement.style.removeProperty(name);
    }
  });

  it("reads CSS vars off <html> and returns rgb() strings", () => {
    document.documentElement.style.setProperty("--up", "120 100% 50%");
    document.documentElement.style.setProperty("--down", "0 100% 50%");
    document.documentElement.style.setProperty("--flat", "0 0% 50%");
    document.documentElement.style.setProperty("--border", "0 0% 0%");
    document.documentElement.style.setProperty("--muted-foreground", "0 0% 100%");
    for (let i = 1; i <= 6; i++) {
      document.documentElement.style.setProperty(`--chart-${i}`, "0 100% 50%");
    }

    const theme = getChartTheme();
    expect(theme.up).toBe("rgb(0, 255, 0)");
    expect(theme.down).toBe("rgb(255, 0, 0)");
    expect(theme.flat).toBe("rgb(128, 128, 128)");
    expect(theme.grid).toBe("rgb(0, 0, 0)");
    expect(theme.text).toBe("rgb(255, 255, 255)");
    expect(theme.series).toHaveLength(6);
    expect(theme.series.every((c) => c === "rgb(255, 0, 0)")).toBe(true);
  });

  it("reflects updated vars on the next call (theme toggle behaviour)", () => {
    document.documentElement.style.setProperty("--up", "120 100% 50%");
    expect(getChartTheme().up).toBe("rgb(0, 255, 0)");
    document.documentElement.style.setProperty("--up", "0 100% 50%");
    expect(getChartTheme().up).toBe("rgb(255, 0, 0)");
  });

  it("degrades to mid-grey when vars are missing", () => {
    expect(getChartTheme().up).toBe("rgb(128, 128, 128)");
  });
});
