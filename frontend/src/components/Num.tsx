import { formatCompact, formatNumber, formatPct } from "@/lib/formatters";

/**
 * Typography atoms for numeric display.
 *
 * `<Num>` renders a value in `font-mono tabular-nums` so digits line up
 * in columns and don't jitter on refresh. `<DeltaText>` adds the signed
 * (+/−) convention and colors purely by sign via the semantic
 * `text-up` / `text-down` / `text-flat` tokens — the actual red/green
 * mapping is owned by the `[data-market-colors]` CSS convention layer,
 * never by components.
 *
 * Formatting reuses `lib/formatters` (see the PR #156 note there about
 * percent-unit conventions) — do not inline `toFixed` here.
 */

export type NumFormat = "number" | "percent" | "compact";

export interface NumProps {
  value: number | null | undefined;
  /**
   * "number"  → locale thousands separators (default)
   * "percent" → unsigned percent, value already in percent units
   * "compact" → K / M / B suffix for large counts
   */
  format?: NumFormat;
  /** Decimals for "number" / "percent". Ignored by "compact". Default 2. */
  decimals?: number;
  className?: string;
}

export function Num({ value, format = "number", decimals = 2, className }: NumProps) {
  const text =
    format === "percent"
      ? formatPct(value, { signed: false, decimals })
      : format === "compact"
        ? formatCompact(value)
        : formatNumber(value, decimals);
  return (
    <span className={`font-mono tabular-nums${className ? ` ${className}` : ""}`}>
      {text}
    </span>
  );
}

export interface DeltaTextProps {
  value: number | null | undefined;
  /** Append a percent suffix (value already in percent units). */
  percent?: boolean;
  /** Decimals to render. Default 2. */
  decimals?: number;
  className?: string;
}

/**
 * Signed delta: `+1.23` / `-1.23` (or `+1.23%` with `percent`), colored
 * by sign only — up / down / flat. Null / undefined renders a flat `—`.
 */
export function DeltaText({ value, percent = false, decimals = 2, className }: DeltaTextProps) {
  const tone =
    value == null || value === 0 ? "text-flat" : value > 0 ? "text-up" : "text-down";
  const text =
    value == null
      ? "—"
      : percent
        ? formatPct(value, { decimals })
        : `${value >= 0 ? "+" : ""}${formatNumber(value, decimals)}`;
  return (
    <span className={`font-mono tabular-nums ${tone}${className ? ` ${className}` : ""}`}>
      {text}
    </span>
  );
}
