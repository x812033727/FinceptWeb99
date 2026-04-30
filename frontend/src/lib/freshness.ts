/**
 * Compact timestamp formatter for "data freshness" labels.
 *
 * Rule:
 *   - same day as now → HH:MM (or HH:MM:SS if `seconds: true`)
 *   - any other day  → M/D HH:MM
 *
 * The date prefix is critical for off-hours / weekend views where a
 * bare "14:31" could be today's last tick OR Friday's close. Tested
 * paths in DashboardPage / StockDetailPage / WatchlistPage all share
 * this so the cutoff behaviour is consistent.
 *
 * `tsMs` is epoch milliseconds (the same shape backend services emit
 * in their `_normalize_quote` output). Returns null when the input
 * is missing / unparseable so callers can skip rendering rather than
 * showing "—" next to a real-looking price.
 */
export function formatQuoteFreshness(
  tsMs: number | null | undefined,
  locale: string,
  opts: { seconds?: boolean } = {},
): string | null {
  if (typeof tsMs !== "number" || tsMs <= 0) return null;
  const d = new Date(tsMs);
  if (Number.isNaN(d.getTime())) return null;

  const now = new Date();
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();

  const time = d.toLocaleTimeString(locale, {
    hour: "2-digit",
    minute: "2-digit",
    ...(opts.seconds ? { second: "2-digit" } : {}),
  });

  if (sameDay) return time;

  const date = d.toLocaleDateString(locale, {
    month: "numeric",
    day: "numeric",
  });
  return `${date} ${time}`;
}
