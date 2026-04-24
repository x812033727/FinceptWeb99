import { create } from "zustand";

type Theme = "dark" | "light";

function applyTheme(theme: Theme) {
  if (theme === "light") {
    document.documentElement.setAttribute("data-light", "");
  } else {
    document.documentElement.removeAttribute("data-light");
  }
}

interface ThemeState {
  theme: Theme;
  toggle: () => void;
}

const stored = (localStorage.getItem("theme") as Theme | null) ?? "dark";
applyTheme(stored);

export const useThemeStore = create<ThemeState>((set) => ({
  theme: stored,
  toggle: () =>
    set((s) => {
      const next: Theme = s.theme === "dark" ? "light" : "dark";
      localStorage.setItem("theme", next);
      applyTheme(next);
      return { theme: next };
    }),
}));
