/**
 * Client-side 日K → 週K / 月K aggregation (A2 多週期切換).
 *
 * The daily history the chart already fetched is rolled up locally —
 * no extra endpoint. Buckets:
 *   - week:  ISO-8601 week (Monday-start; the year-boundary week belongs
 *            to the year that owns its Thursday), which matches TW
 *            brokerage 週K conventions.
 *   - month: calendar month.
 *
 * Semantics per bucket: open = first trading day's open, close = last
 * trading day's close, high/low = extremes, volume = sum. The bar's
 * `time` label is the first *trading* day present in the bucket (not the
 * theoretical Monday / 1st) so TW holiday weeks label correctly and
 * lightweight-charts gets a real yyyy-mm-dd it can plot.
 *
 * Pure function over daily bars with string "YYYY-MM-DD" times; numeric
 * (intraday) times are ignored — intraday bars are never week-aggregated.
 */
import type { OHLCVBar } from "@/types/market";

export type AggregateUnit = "week" | "month";

/** ISO-8601 week key, e.g. "2026-W28". UTC math throughout so the local
 *  timezone can never shift a bar across a week boundary. */
function isoWeekKey(dateStr: string): string {
  const [y, m, d] = dateStr.split("-").map(Number);
  const date = new Date(Date.UTC(y, m - 1, d));
  const day = date.getUTCDay() || 7; // Mon=1 … Sun=7
  // Shift to the Thursday of this week — its year owns the ISO week.
  date.setUTCDate(date.getUTCDate() + 4 - day);
  const isoYear = date.getUTCFullYear();
  const yearStart = Date.UTC(isoYear, 0, 1);
  const week = Math.ceil(((date.getTime() - yearStart) / 86_400_000 + 1) / 7);
  return `${isoYear}-W${String(week).padStart(2, "0")}`;
}

function bucketKey(dateStr: string, unit: AggregateUnit): string {
  return unit === "month" ? dateStr.slice(0, 7) : isoWeekKey(dateStr);
}

/** Roll daily OHLCV bars up into weekly / monthly bars. Input order is
 *  irrelevant (sorted internally); output is time-ascending. */
export function aggregateBars(daily: OHLCVBar[], unit: AggregateUnit): OHLCVBar[] {
  const dated = daily
    .filter((b): b is OHLCVBar & { time: string } => typeof b.time === "string")
    .sort((a, b) => (a.time < b.time ? -1 : a.time > b.time ? 1 : 0));

  const out: OHLCVBar[] = [];
  let key: string | null = null;
  let cur: OHLCVBar | null = null;

  for (const bar of dated) {
    const k = bucketKey(bar.time, unit);
    if (cur === null || k !== key) {
      if (cur) out.push(cur);
      key = k;
      cur = { ...bar };
    } else {
      cur.high = Math.max(cur.high, bar.high);
      cur.low = Math.min(cur.low, bar.low);
      cur.close = bar.close;
      cur.volume += bar.volume;
    }
  }
  if (cur) out.push(cur);
  return out;
}
