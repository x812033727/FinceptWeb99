/**
 * Numeric formatting helpers for the discussion subsystem
 * (price/percent rendering, compact TW 萬/億 conversion, band colour
 * classes). Pure logic — no JSX.
 */
import { formatPct } from "@/lib/formatters";

export function toFixedSmart(n: number): string {
  // Show prices with up to 2 decimals, trimming trailing zeros so
  // round-number TWSE prices read "55" not "55.00".
  return Number.isInteger(n) ? n.toString() : (Math.round(n * 100) / 100).toString();
}

export function signedPct(n: number): string {
  return formatPct(n, { alreadyPct: false, decimals: 1 });
}

export function signedPctSafe(n: number | null | undefined): string {
  return formatPct(n, { alreadyPct: false });
}

export function pctClass(n: number | null | undefined): string {
  if (n === null || n === undefined) return "text-muted-foreground";
  return n >= 0 ? "text-up" : "text-down";
}

export function latestNonNull(arr: (number | null)[] | undefined | null): number | null {
  if (!arr) return null;
  for (let i = arr.length - 1; i >= 0; i--) {
    const v = arr[i];
    if (v != null) return v;
  }
  return null;
}

export function formatCompactNumber(n: number): string {
  // Foreign-buy volume is in shares; numbers like 12_345_678 are
  // unreadable inline. Convert to 萬/億 (TW convention) so the
  // headline stays terse.
  const abs = Math.abs(n);
  if (abs >= 1e8) return `${(n / 1e8).toFixed(2)} 億`;
  if (abs >= 1e4) return `${(n / 1e4).toFixed(1)} 萬`;
  return n.toLocaleString();
}
