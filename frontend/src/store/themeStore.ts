import { create } from "zustand";

type Theme = "dark" | "light";
export type MarketColorMode = "auto" | "tw" | "intl";
export type Density = "comfortable" | "compact";

function applyTheme(theme: Theme) {
  if (theme === "light") {
    document.documentElement.setAttribute("data-light", "");
  } else {
    document.documentElement.removeAttribute("data-light");
  }
}

// "auto" resolves via the user's locale without importing i18n — the store
// is eagerly imported before src/i18n in main.tsx, and an import here would
// couple boot order. Reads the same localStorage key the i18next
// languagedetector caches to, falling back to the browser locale.
function resolveMarketColors(mode: MarketColorMode): "tw" | "intl" {
  if (mode !== "auto") return mode;
  const locale = localStorage.getItem("fincept.locale") ?? navigator.language ?? "";
  return locale.startsWith("zh") ? "tw" : "intl";
}

export function applyMarketColors(mode: MarketColorMode) {
  document.documentElement.setAttribute(
    "data-market-colors",
    resolveMarketColors(mode)
  );
}

export function applyDensity(density: Density) {
  document.documentElement.setAttribute("data-density", density);
}

interface ThemeState {
  theme: Theme;
  marketColorMode: MarketColorMode;
  density: Density;
  toggle: () => void;
  setMarketColorMode: (mode: MarketColorMode) => void;
  setDensity: (density: Density) => void;
}

const stored = (localStorage.getItem("theme") as Theme | null) ?? "dark";
applyTheme(stored);

const storedMarketColorMode =
  (localStorage.getItem("marketColorMode") as MarketColorMode | null) ?? "auto";
applyMarketColors(storedMarketColorMode);

const storedDensity =
  (localStorage.getItem("density") as Density | null) ?? "comfortable";
applyDensity(storedDensity);

export const useThemeStore = create<ThemeState>((set) => ({
  theme: stored,
  marketColorMode: storedMarketColorMode,
  density: storedDensity,
  toggle: () =>
    set((s) => {
      const next: Theme = s.theme === "dark" ? "light" : "dark";
      localStorage.setItem("theme", next);
      applyTheme(next);
      return { theme: next };
    }),
  setMarketColorMode: (mode) => {
    localStorage.setItem("marketColorMode", mode);
    applyMarketColors(mode);
    set({ marketColorMode: mode });
  },
  setDensity: (density) => {
    localStorage.setItem("density", density);
    applyDensity(density);
    set({ density });
  },
}));
