import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { formatQuoteFreshness } from "./freshness";

// Pin the system clock so "today vs not-today" branches are stable.
const NOW = new Date("2026-04-30T14:31:00+00:00").getTime();

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});

afterEach(() => {
  vi.useRealTimers();
});

describe("formatQuoteFreshness", () => {
  it("returns null for missing / non-positive timestamps", () => {
    expect(formatQuoteFreshness(null, "en-US")).toBeNull();
    expect(formatQuoteFreshness(undefined, "en-US")).toBeNull();
    expect(formatQuoteFreshness(0, "en-US")).toBeNull();
    expect(formatQuoteFreshness(-1, "en-US")).toBeNull();
  });

  it("formats same-day timestamps as time only", () => {
    // 5 min before NOW, same day in UTC.
    const ts = NOW - 5 * 60 * 1000;
    const out = formatQuoteFreshness(ts, "en-US");
    expect(out).not.toBeNull();
    // No "/" → no date in output.
    expect(out!.includes("/")).toBe(false);
  });

  it("includes date when timestamp is from a different day", () => {
    // 25 hours before NOW → previous calendar day.
    const ts = NOW - 25 * 60 * 60 * 1000;
    const out = formatQuoteFreshness(ts, "en-US");
    expect(out).not.toBeNull();
    expect(out!.includes("/")).toBe(true);
  });

  it("respects `seconds: true` for high-precision displays", () => {
    const ts = NOW - 1000;
    const out = formatQuoteFreshness(ts, "en-US", { seconds: true });
    expect(out).not.toBeNull();
    // Two colons → HH:MM:SS, not HH:MM.
    expect(out!.match(/:/g)?.length).toBe(2);
  });
});
