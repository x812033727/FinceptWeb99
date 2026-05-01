/**
 * Shared display formatters for numeric values.
 *
 * Designed to centralize the conventions across pages and prevent
 * the kind of double-multiplication bug we hit in PR #156, where
 * `StockDetailPage`'s local `fmtPct(value, alreadyPct = false)`
 * default disagreed with `MarketPage` / `WatchlistPage` (which assume
 * the backend already returns percent units like `9.93`).
 *
 * Backend convention: `change_pct` is in percent units. Pass through
 * via `formatPct(value)` with the default `alreadyPct=true`.
 */

export interface FormatPctOptions {
  /**
   * Set to `false` only if the value is a fraction in `[-1, 1]` and
   * needs to be multiplied by 100 (e.g. weight ratios). Default `true`
   * matches the backend's `change_pct` / `unrealized_pnl_pct` /
   * `revenue_yoy` shape.
   */
  alreadyPct?: boolean;
  /**
   * Prefix non-negative values with `+`. Default `true` so price-move
   * tiles read `+1.23%` / `-1.23%`. Pass `false` for KPI cells where
   * the column header already implies sign.
   */
  signed?: boolean;
  /** Decimals to render. Default `2`. */
  decimals?: number;
}

/**
 * Format a number as a percentage string, or `—` when null/undefined.
 *
 * @example
 * formatPct(9.93)                   // "+9.93%"
 * formatPct(-2.50)                  // "-2.50%"
 * formatPct(0.0993, { alreadyPct: false })  // "+9.93%"
 * formatPct(15.2, { signed: false }) // "15.20%"
 * formatPct(null)                   // "—"
 */
export function formatPct(
  n: number | null | undefined,
  opts: FormatPctOptions = {},
): string {
  if (n == null) return "—";
  const { alreadyPct = true, signed = true, decimals = 2 } = opts;
  const v = alreadyPct ? n : n * 100;
  const sign = signed && v >= 0 ? "+" : "";
  return `${sign}${v.toFixed(decimals)}%`;
}

/**
 * Format a number with locale-aware thousands separators, or `—` when
 * null/undefined. Uses the user's browser locale.
 */
export function formatNumber(
  n: number | null | undefined,
  decimals = 2,
): string {
  if (n == null) return "—";
  return n.toLocaleString(undefined, {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

/**
 * Compact "K / M / B" suffix for large counts (volume, market cap).
 * Returns the raw number as a string for values < 1000.
 */
export function formatCompact(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(0)}K`;
  return String(n);
}
