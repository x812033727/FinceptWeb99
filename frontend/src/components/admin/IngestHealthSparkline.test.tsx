import { describe, it, expect } from "vitest";

import { classifyDay, rollingDates } from "./IngestHealthSparkline";

describe("classifyDay", () => {
  it("returns gray 'no data' for null", () => {
    const out = classifyDay(null);
    expect(out.label).toBe("no data");
    expect(out.cls).toContain("muted");
  });

  it("prioritizes failed over every other outcome", () => {
    // Even with successful runs the same day, any failure flags the
    // cell red — operators should see the worst outcome at a glance.
    const out = classifyDay({
      date: "2026-05-01", ok: 5, silent_deny: 2, failed: 1, skipped: 0,
    });
    expect(out.label).toBe("failed");
    expect(out.cls).toContain("danger");
  });

  it("flags silent_deny purple when no failures", () => {
    const out = classifyDay({
      date: "2026-05-01", ok: 3, silent_deny: 1, failed: 0, skipped: 0,
    });
    expect(out.label).toBe("silent deny");
    expect(out.cls).toContain("purple-500");
  });

  it("flags skipped amber only when there were ZERO ok runs", () => {
    // Mix of ok + skipped → green (the cron mostly worked).
    const mixed = classifyDay({
      date: "2026-05-01", ok: 2, silent_deny: 0, failed: 0, skipped: 3,
    });
    expect(mixed.label).toBe("ok");
    expect(mixed.cls).toContain("success");

    // All-skipped (no ok runs) → amber. Distinguishes "the cron is
    // currently disabled" from "the cron ran fine".
    const allSkipped = classifyDay({
      date: "2026-05-01", ok: 0, silent_deny: 0, failed: 0, skipped: 4,
    });
    expect(allSkipped.label).toBe("skipped");
    expect(allSkipped.cls).toContain("warning");
  });

  it("flags ok green when the day has only successful runs", () => {
    const out = classifyDay({
      date: "2026-05-01", ok: 10, silent_deny: 0, failed: 0, skipped: 0,
    });
    expect(out.label).toBe("ok");
    expect(out.cls).toContain("success");
  });

  it("falls back to 'no data' when every counter is zero", () => {
    // Defensive: a day bucket landed in the response but every
    // counter is zero somehow — render gray, don't claim green.
    const out = classifyDay({
      date: "2026-05-01", ok: 0, silent_deny: 0, failed: 0, skipped: 0,
    });
    expect(out.label).toBe("no data");
  });
});


describe("rollingDates", () => {
  it("returns N consecutive UTC dates ending today, oldest-first", () => {
    const today = new Date(Date.UTC(2026, 4, 9));   // 2026-05-09
    const dates = rollingDates(today, 7);
    expect(dates).toHaveLength(7);
    expect(dates[6]).toBe("2026-05-09");
    expect(dates[0]).toBe("2026-05-03");
  });

  it("handles month-boundary correctly via UTC arithmetic", () => {
    const today = new Date(Date.UTC(2026, 4, 2));   // 2026-05-02
    const dates = rollingDates(today, 7);
    expect(dates[6]).toBe("2026-05-02");
    expect(dates[0]).toBe("2026-04-26");
  });

  it("respects a custom day count", () => {
    const today = new Date(Date.UTC(2026, 4, 9));
    expect(rollingDates(today, 3)).toEqual([
      "2026-05-07", "2026-05-08", "2026-05-09",
    ]);
  });
});
