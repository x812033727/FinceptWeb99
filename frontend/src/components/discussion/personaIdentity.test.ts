import { describe, it, expect } from "vitest";
import { getPersonaIdentity } from "./personaIdentity";

describe("getPersonaIdentity", () => {
  it("returns the same hue for the same persona id (deterministic)", () => {
    const a = getPersonaIdentity("buffett", "Warren Buffett");
    const b = getPersonaIdentity("buffett", "Warren Buffett");
    expect(a.hue).toBe(b.hue);
    expect(a.accentColor).toBe(b.accentColor);
  });

  it("emits hsl()-formatted accent colours from the hue", () => {
    const id = getPersonaIdentity("buffett", "Warren Buffett");
    expect(id.accentColor).toMatch(/^hsl\(\d+ \d+% \d+%\)$/);
    expect(id.ringColor).toMatch(/^hsl\(\d+ \d+% \d+%\)$/);
    expect(id.softColor).toMatch(/^hsl\(\d+ \d+% \d+% \/ 0\.18\)$/);
  });

  it("avoids the warning-colour band (red/amber/purple) so accents don't collide with stance / status pills", () => {
    // The full persona roster — every assigned hue must sit outside
    // the stance/status semantic bands. Bounds match the
    // documented avoidance bands in personaIdentity.ts.
    const ids = [
      "market_analyst", "portfolio_advisor", "risk_manager",
      "macro_analyst", "earnings_analyst", "trading_coach",
      "claude_research", "buffett", "graham", "munger",
      "lynch", "fisher", "smith", "marks", "klarman",
      "dalio", "soros", "simons", "asness",
      "livermore", "ptj", "minervini", "raschke",
      "kostolany",
    ];
    for (const id of ids) {
      const { hue } = getPersonaIdentity(id, id);
      const inRedBand = hue >= 0 && hue <= 20;
      const inAmberBand = hue >= 25 && hue <= 50;
      const inPurpleBand = hue >= 270 && hue <= 320;
      expect(inRedBand || inAmberBand || inPurpleBand).toBe(false);
    }
  });

  it("derives a single CJK char as the avatar initial when no explicit short label is given", () => {
    expect(getPersonaIdentity("buffett", "巴菲特 Warren Buffett").initial).toBe("巴");
    expect(getPersonaIdentity("munger", "蒙格").initial).toBe("蒙");
  });

  it("uppercases the first ASCII letter when no CJK char is present", () => {
    expect(getPersonaIdentity("buffett", "Warren Buffett").initial).toBe("W");
    expect(getPersonaIdentity("dalio", "ray dalio").initial).toBe("R");
  });

  it("prefers the explicit short label when provided", () => {
    expect(getPersonaIdentity("buffett", "Warren Buffett", "BU").initial).toBe("BU");
    expect(getPersonaIdentity("buffett", "Warren Buffett", "巴").initial).toBe("巴");
  });

  it("falls back to '?' when name is empty and no explicit initial provided", () => {
    expect(getPersonaIdentity("", "").initial).toBe("?");
  });
});
