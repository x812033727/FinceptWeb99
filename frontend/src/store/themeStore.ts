import { create } from "zustand";

type Theme = "dark" | "light";
export type MarketColorMode = "auto" | "tw" | "intl";

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

interface ThemeState {
  theme: Theme;
  marketColorMode: MarketColorMode;
  toggle: () => void;
  setMarketColorMode: (mode: MarketColorMode) => void;
}

const stored = (localStorage.getItem("theme") as Theme | null) ?? "dark";
applyTheme(stored);

const storedMarketColorMode =
  (localStorage.getItem("marketColorMode") as MarketColorMode | null) ?? "auto";
applyMarketColors(storedMarketColorMode);

export const useThemeStore = create<ThemeState>((set) => ({
  theme: stored,
  marketColorMode: storedMarketColorMode,
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
}));
