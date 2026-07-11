/** Indicator selection model + localStorage persistence (feature A1).
 *  Kept out of IndicatorToolbar.tsx so that file only exports components
 *  (react-refresh constraint). */

/** Main-pane overlays — multi-toggle. */
export type OverlayKey = "ma" | "ema" | "boll";
/** Sub-pane oscillators — single-select (null = off). */
export type SubKey = "rsi" | "macd" | "kd";

export interface IndicatorPrefs {
  overlays: OverlayKey[];
  sub: SubKey | null;
}

export const INDICATOR_STORAGE_KEY = "chart.indicators";

export const OVERLAY_KEYS: OverlayKey[] = ["ma", "ema", "boll"];
export const SUB_KEYS: SubKey[] = ["rsi", "macd", "kd"];

export const DEFAULT_INDICATOR_PREFS: IndicatorPrefs = { overlays: [], sub: null };

/** Restore persisted indicator preferences; tolerates missing/corrupt JSON. */
export function loadIndicatorPrefs(): IndicatorPrefs {
  try {
    const raw = localStorage.getItem(INDICATOR_STORAGE_KEY);
    if (!raw) return DEFAULT_INDICATOR_PREFS;
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null) return DEFAULT_INDICATOR_PREFS;
    const p = parsed as { overlays?: unknown; sub?: unknown };
    const overlays = Array.isArray(p.overlays)
      ? OVERLAY_KEYS.filter((k) => (p.overlays as unknown[]).includes(k))
      : [];
    const sub = SUB_KEYS.includes(p.sub as SubKey) ? (p.sub as SubKey) : null;
    return { overlays, sub };
  } catch {
    return DEFAULT_INDICATOR_PREFS;
  }
}

export function saveIndicatorPrefs(prefs: IndicatorPrefs): void {
  try {
    localStorage.setItem(INDICATOR_STORAGE_KEY, JSON.stringify(prefs));
  } catch {
    // Storage full / unavailable (private browsing) — preference simply
    // won't survive a reload; not worth surfacing to the user.
  }
}
