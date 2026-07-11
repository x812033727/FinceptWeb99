import { describe, it, expect } from "vitest";
import { aggregateBars } from "./aggregateBars";
import type { OHLCVBar } from "@/types/market";

function bar(time: string, o: number, h: number, l: number, c: number, v = 100): OHLCVBar {
  return { time, open: o, high: h, low: l, close: c, volume: v };
}

describe("aggregateBars — weekly", () => {
  it("aggregates one Mon–Fri trading week into a single bar", () => {
    // 2026-07-06 (Mon) … 2026-07-10 (Fri)
    const daily = [
      bar("2026-07-06", 100, 105, 99, 104, 1000),
      bar("2026-07-07", 104, 110, 103, 108, 2000),
      bar("2026-07-08", 108, 109, 95, 96, 1500),
      bar("2026-07-09", 96, 101, 96, 100, 500),
      bar("2026-07-10", 100, 103, 98, 102, 800),
    ];
    const out = aggregateBars(daily, "week");
    expect(out).toHaveLength(1);
    expect(out[0]).toEqual({
      time: "2026-07-06", // first trading day of the week labels the bar
      open: 100,          // Monday's open
      high: 110,          // Tuesday's high
      low: 95,            // Wednesday's low
      close: 102,         // Friday's close
      volume: 5800,       // sum
    });
  });

  it("splits Friday / next Monday into separate ISO weeks", () => {
    const out = aggregateBars(
      [bar("2026-07-10", 1, 2, 1, 2), bar("2026-07-13", 3, 4, 3, 4)],
      "week",
    );
    expect(out).toHaveLength(2);
    expect(out.map((b) => b.time)).toEqual(["2026-07-10", "2026-07-13"]);
  });

  it("keeps a year-straddling ISO week as one bar (Mon 2025-12-29 → Fri 2026-01-02)", () => {
    const out = aggregateBars(
      [
        bar("2025-12-29", 10, 12, 9, 11, 100),
        bar("2025-12-30", 11, 13, 10, 12, 100),
        bar("2026-01-02", 12, 15, 11, 14, 100),
      ],
      "week",
    );
    expect(out).toHaveLength(1);
    expect(out[0].time).toBe("2025-12-29");
    expect(out[0].open).toBe(10);
    expect(out[0].close).toBe(14);
    expect(out[0].high).toBe(15);
    expect(out[0].volume).toBe(300);
  });

  it("labels a TW holiday week by its first actual trading day", () => {
    // Monday 2026-06-22 is a (hypothetical) holiday — week starts Tuesday.
    const out = aggregateBars(
      [bar("2026-06-23", 5, 6, 4, 5), bar("2026-06-24", 5, 7, 5, 6)],
      "week",
    );
    expect(out).toHaveLength(1);
    expect(out[0].time).toBe("2026-06-23");
  });

  it("Sunday belongs to the same ISO week as the preceding Monday", () => {
    // Crypto trades weekends: Mon 2026-07-06 … Sun 2026-07-12 = one week;
    // Mon 2026-07-13 starts the next.
    const out = aggregateBars(
      [
        bar("2026-07-06", 1, 1, 1, 1, 10),
        bar("2026-07-12", 2, 2, 2, 2, 10),
        bar("2026-07-13", 3, 3, 3, 3, 10),
      ],
      "week",
    );
    expect(out).toHaveLength(2);
    expect(out[0].volume).toBe(20);
  });
});

describe("aggregateBars — monthly", () => {
  it("aggregates by calendar month with first/last/extreme/sum semantics", () => {
    const out = aggregateBars(
      [
        bar("2026-06-29", 50, 55, 49, 54, 100),
        bar("2026-06-30", 54, 60, 53, 58, 200),
        bar("2026-07-01", 58, 59, 40, 45, 300),
        bar("2026-07-02", 45, 47, 44, 46, 400),
      ],
      "month",
    );
    expect(out).toHaveLength(2);
    expect(out[0]).toEqual({ time: "2026-06-29", open: 50, high: 60, low: 49, close: 58, volume: 300 });
    expect(out[1]).toEqual({ time: "2026-07-01", open: 58, high: 59, low: 40, close: 46, volume: 700 });
  });

  it("December / January split across years", () => {
    const out = aggregateBars(
      [bar("2025-12-31", 1, 1, 1, 1), bar("2026-01-02", 2, 2, 2, 2)],
      "month",
    );
    expect(out.map((b) => b.time)).toEqual(["2025-12-31", "2026-01-02"]);
  });
});

describe("aggregateBars — robustness", () => {
  it("returns [] for empty input", () => {
    expect(aggregateBars([], "week")).toEqual([]);
    expect(aggregateBars([], "month")).toEqual([]);
  });

  it("sorts unsorted input before bucketing", () => {
    const out = aggregateBars(
      [bar("2026-07-08", 108, 109, 95, 96), bar("2026-07-06", 100, 105, 99, 104)],
      "week",
    );
    expect(out).toHaveLength(1);
    expect(out[0].open).toBe(100); // Monday's open, despite arriving second
    expect(out[0].close).toBe(96);
  });

  it("ignores numeric (intraday) times", () => {
    const intraday: OHLCVBar = { time: 1751871600000, open: 1, high: 1, low: 1, close: 1, volume: 1 };
    expect(aggregateBars([intraday, bar("2026-07-06", 2, 2, 2, 2)], "month")).toHaveLength(1);
  });

  it("does not mutate the input bars", () => {
    const a = bar("2026-07-06", 100, 105, 99, 104, 1000);
    const b = bar("2026-07-07", 104, 110, 103, 108, 2000);
    aggregateBars([a, b], "week");
    expect(a.volume).toBe(1000);
    expect(b.high).toBe(110);
  });
});
