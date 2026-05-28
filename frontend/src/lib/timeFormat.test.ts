import { describe, expect, it } from "vitest";
import { convertJsonTimestampsToTaipei, formatTaipei } from "./timeFormat";

describe("formatTaipei", () => {
  it("renders a UTC moment as Taipei wall-clock (UTC+8)", () => {
    // 13:57 UTC on 2026-05-28 == 21:57 Taipei the same day
    expect(formatTaipei("2026-05-28T13:57:18.732876+00:00")).toBe(
      "2026/05/28 21:57",
    );
  });

  it("renders a Z-suffixed UTC string identically", () => {
    expect(formatTaipei("2026-05-28T13:57:00Z")).toBe("2026/05/28 21:57");
  });

  it("renders Taipei time unchanged when the source already says +08:00", () => {
    expect(formatTaipei("2026-05-28T21:57:00+08:00")).toBe(
      "2026/05/28 21:57",
    );
  });

  it("appends +08:00 in datetime-tz variant for unambiguous JSON display", () => {
    expect(
      formatTaipei("2026-05-28T13:57:18.732876+00:00", "datetime-tz"),
    ).toBe("2026/05/28 21:57:18 +08:00");
  });

  it("date variant drops the time component", () => {
    expect(formatTaipei("2026-05-28T13:57:18Z", "date")).toBe("2026/05/28");
  });

  it("returns empty string for null / undefined / invalid input", () => {
    expect(formatTaipei(null)).toBe("");
    expect(formatTaipei(undefined)).toBe("");
    expect(formatTaipei("not-a-date")).toBe("");
    expect(formatTaipei("")).toBe("");
  });

  it("crosses midnight boundary correctly — 17:00 UTC = 01:00 Taipei next day", () => {
    expect(formatTaipei("2026-05-28T17:00:00Z")).toBe("2026/05/29 01:00");
  });
});

describe("convertJsonTimestampsToTaipei", () => {
  it("converts a captured_at-style top-level field", () => {
    const result = convertJsonTimestampsToTaipei({
      captured_at: "2026-05-28T13:57:18.732876+00:00",
      market: "TW",
    });
    expect(result.captured_at).toBe("2026/05/28 21:57:18 +08:00");
    expect(result.market).toBe("TW");
  });

  it("walks nested objects and arrays", () => {
    const result = convertJsonTimestampsToTaipei({
      captured_session: {
        session_date: "2026-05-28",  // pure date — pass through
        hint_zh: "資料截至 2026-05-28 收盤",
      },
      news: [
        { published_at: "2026-05-28T12:00:00Z", title: "A" },
        { published_at: "2026-05-28T15:30:00Z", title: "B" },
      ],
    });
    // Pure date strings stay unchanged
    expect(result.captured_session.session_date).toBe("2026-05-28");
    // ISO-8601 with time gets converted
    expect(result.news[0].published_at).toBe("2026/05/28 20:00:00 +08:00");
    expect(result.news[1].published_at).toBe("2026/05/28 23:30:00 +08:00");
    expect(result.news[0].title).toBe("A");
  });

  it("leaves numbers, booleans, and null intact", () => {
    const result = convertJsonTimestampsToTaipei({
      price: 60.6,
      change_pct: 9.9819,
      is_stale: true,
      industry: null,
    });
    expect(result.price).toBe(60.6);
    expect(result.change_pct).toBe(9.9819);
    expect(result.is_stale).toBe(true);
    expect(result.industry).toBe(null);
  });

  it("does not mutate input", () => {
    const input = {
      captured_at: "2026-05-28T13:57:18Z",
      nested: { ts: "2026-05-28T12:00:00Z" },
    };
    const snapshot = JSON.parse(JSON.stringify(input));
    convertJsonTimestampsToTaipei(input);
    expect(input).toEqual(snapshot);
  });

  it("passes through ISO-looking strings that are clearly not timestamps", () => {
    const result = convertJsonTimestampsToTaipei({
      not_a_ts: "2026-05-28 something else",
      year_only: "2026",
    });
    expect(result.not_a_ts).toBe("2026-05-28 something else");
    expect(result.year_only).toBe("2026");
  });
});
