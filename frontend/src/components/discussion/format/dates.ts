/**
 * Date formatting helpers for the discussion subsystem. All render in
 * Asia/Taipei regardless of browser TZ. Pure logic — no JSX.
 */
import { formatTaipei } from "@/lib/timeFormat";

// ── date formatting ────────────────────────────────────────────────

export function formatDateShort(iso: string | undefined): string {
  if (!iso) return "";
  // Render in Asia/Taipei regardless of browser TZ. `date` variant
  // returns `2026/05/28`; slice off the year so the short form
  // matches the prior `month/day` shape callers expect.
  const full = formatTaipei(iso, "date");
  if (!full) return "";
  const parts = full.split("/");
  return parts.length === 3 ? `${parts[1]}/${parts[2]}` : full;
}

export function formatDateLong(iso: string | undefined): string {
  if (!iso) return "";
  return formatTaipei(iso, "datetime");
}

// ── dynamic discussion title ──────────────────────────────────────

export function formatTaipeiDateCompact(iso: string): string {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  // en-CA gives YYYY-MM-DD; strip dashes for the compact YYYYMMDD form.
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Taipei",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(d);
  return parts.replace(/-/g, "");
}
