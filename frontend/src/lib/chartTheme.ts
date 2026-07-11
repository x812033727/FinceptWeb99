/**
 * Theme-aware color access for canvas chart libraries.
 *
 * lightweight-charts v4's color parser only accepts hex / named colors /
 * rgb() — it rejects every form of hsl(). The project's CSS vars are
 * shadcn-style space-separated HSL components ("215 20% 55%"), so this
 * module reads them off <html> at call time and converts to rgb() strings.
 * Call getChartTheme() again after a theme / market-convention toggle to
 * pick up the new values (the CSS vars are re-resolved on each call).
 */

export function hslVarToRgb(raw: string): string {
  // shadcn-style CSS vars: "215 20% 55%" (H S% L%, no hsl() wrapper).
  const parts = raw.split(/\s+/);
  if (parts.length < 3) return "rgb(128, 128, 128)";
  const h = parseFloat(parts[0]);
  const s = parseFloat(parts[1]) / 100;
  const l = parseFloat(parts[2]) / 100;
  const k = (n: number) => (n + h / 30) % 12;
  const a = s * Math.min(l, 1 - l);
  const f = (n: number) =>
    l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
  const r = Math.round(255 * f(0));
  const g = Math.round(255 * f(8));
  const b = Math.round(255 * f(4));
  return `rgb(${r}, ${g}, ${b})`;
}

export interface ChartTheme {
  up: string;
  down: string;
  flat: string;
  grid: string;
  text: string;
  series: string[];
}

export function getChartTheme(): ChartTheme {
  const style = getComputedStyle(document.documentElement);
  const v = (name: string) => hslVarToRgb(style.getPropertyValue(name).trim());
  return {
    up: v("--up"),
    down: v("--down"),
    flat: v("--flat"),
    grid: v("--border"),
    text: v("--muted-foreground"),
    series: [
      v("--chart-1"),
      v("--chart-2"),
      v("--chart-3"),
      v("--chart-4"),
      v("--chart-5"),
      v("--chart-6"),
    ],
  };
}
