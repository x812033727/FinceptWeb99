/**
 * Taipei-pinned timestamp formatting.
 *
 * Every UI surface that renders a backend timestamp goes through here
 * so the displayed value is `Asia/Taipei` regardless of the browser's
 * configured timezone. Previously most call sites used bare
 * `Date.prototype.toLocaleString()` which defers to host TZ — a user
 * on a UTC laptop saw raw UTC, which is what triggered the bug
 * report ("why is captured_at 13:57?").
 */

const TAIPEI_TZ = "Asia/Taipei";
const TAIPEI_LOCALE = "zh-TW";

/**
 * Detect strings that look like ISO-8601 timestamps with a time
 * component (and optional offset / Z). Pure dates (`2026-05-28`,
 * e.g. `session_date`) are intentionally NOT matched — they don't
 * have a clock to localise.
 */
const ISO_TIMESTAMP_RE =
  /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$/;

/**
 * Intl.DateTimeFormat in zh-TW separates date from time with U+2009
 * (THIN SPACE) and uses U+202F (NARROW NO-BREAK SPACE) inside time
 * parts on some hosts. Normalise to ASCII so the output is greppable
 * and stable across browsers.
 */
const FANCY_SPACE_RE = /[   ]/g;

export type TimeVariant = "datetime" | "datetime-tz" | "date" | "time";

const FORMATTERS: Record<TimeVariant, Intl.DateTimeFormat> = {
  datetime: new Intl.DateTimeFormat(TAIPEI_LOCALE, {
    timeZone: TAIPEI_TZ,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }),
  "datetime-tz": new Intl.DateTimeFormat(TAIPEI_LOCALE, {
    timeZone: TAIPEI_TZ,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }),
  date: new Intl.DateTimeFormat(TAIPEI_LOCALE, {
    timeZone: TAIPEI_TZ,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }),
  time: new Intl.DateTimeFormat(TAIPEI_LOCALE, {
    timeZone: TAIPEI_TZ,
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }),
};

/**
 * Format a moment as a Taipei-local string, regardless of host TZ.
 *
 * - `datetime` (default): `2026/05/28 21:57`
 * - `datetime-tz`: `2026/05/28 21:57:18 +08:00` — for the JSON view
 *    where the trailing offset disambiguates the converted string
 *    from raw UTC at a glance.
 * - `date`: `2026/05/28`
 * - `time`: `21:57`
 *
 * Returns `""` for null/undefined/invalid input so call sites can
 * use the result directly in JSX without guarding.
 */
export function formatTaipei(
  iso: string | number | Date | null | undefined,
  variant: TimeVariant = "datetime",
): string {
  if (iso == null || iso === "") return "";
  const d = iso instanceof Date ? iso : new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const formatted = FORMATTERS[variant].format(d).replace(FANCY_SPACE_RE, " ");
  if (variant === "datetime-tz") {
    return `${formatted} +08:00`;
  }
  return formatted;
}

/**
 * Recursively walk a JSON-shaped value and replace every ISO-8601
 * timestamp string with a Taipei-localised display string. Pure
 * dates (`YYYY-MM-DD`), numbers, booleans, and null pass through.
 *
 * Returns a NEW value — never mutates input. Used by the round-
 * context JSON view so the raw API payload shown to the user is
 * already in Taipei time without needing to JSON.parse twice.
 */
export function convertJsonTimestampsToTaipei<T>(value: T): T {
  if (value == null) return value;
  if (typeof value === "string") {
    if (ISO_TIMESTAMP_RE.test(value)) {
      return formatTaipei(value, "datetime-tz") as unknown as T;
    }
    return value;
  }
  if (Array.isArray(value)) {
    return value.map((v) => convertJsonTimestampsToTaipei(v)) as unknown as T;
  }
  if (typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
      out[k] = convertJsonTimestampsToTaipei(v);
    }
    return out as T;
  }
  return value;
}
