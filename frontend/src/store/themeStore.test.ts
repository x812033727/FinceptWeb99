import { describe, it, expect, beforeEach } from "vitest";
import { useThemeStore } from "./themeStore";

describe("themeStore", () => {
  beforeEach(() => {
    // Reset DOM + localStorage between tests so each run starts clean
    localStorage.clear();
    document.documentElement.removeAttribute("data-light");
    // Reset the store to dark manually (toggle() when theme==="light")
    if (useThemeStore.getState().theme === "light") {
      useThemeStore.getState().toggle();
    }
  });

  it("initial theme is 'dark' when no localStorage value exists", () => {
    expect(useThemeStore.getState().theme).toBe("dark");
  });

  it("toggle flips dark → light and sets data-light on <html>", () => {
    useThemeStore.getState().toggle();
    expect(useThemeStore.getState().theme).toBe("light");
    expect(document.documentElement.hasAttribute("data-light")).toBe(true);
  });

  it("toggle flips light → dark and removes data-light", () => {
    useThemeStore.getState().toggle();       // → light
    useThemeStore.getState().toggle();       // → dark
    expect(useThemeStore.getState().theme).toBe("dark");
    expect(document.documentElement.hasAttribute("data-light")).toBe(false);
  });

  it("toggle persists to localStorage", () => {
    useThemeStore.getState().toggle();
    expect(localStorage.getItem("theme")).toBe("light");
    useThemeStore.getState().toggle();
    expect(localStorage.getItem("theme")).toBe("dark");
  });
});
