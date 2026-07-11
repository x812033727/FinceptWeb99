import { describe, it, expect, beforeEach } from "vitest";
import { useThemeStore, applyMarketColors } from "./themeStore";

describe("themeStore", () => {
  beforeEach(() => {
    // Reset the store to its defaults first (the setters write localStorage +
    // DOM attributes), then wipe DOM + localStorage so each run starts clean
    if (useThemeStore.getState().theme === "light") {
      useThemeStore.getState().toggle();
    }
    if (useThemeStore.getState().marketColorMode !== "auto") {
      useThemeStore.getState().setMarketColorMode("auto");
    }
    localStorage.clear();
    document.documentElement.removeAttribute("data-light");
    document.documentElement.removeAttribute("data-market-colors");
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

  it("initial marketColorMode is 'auto' when no localStorage value exists", () => {
    expect(useThemeStore.getState().marketColorMode).toBe("auto");
  });

  it("setMarketColorMode('tw') stamps data-market-colors='tw' on <html>", () => {
    useThemeStore.getState().setMarketColorMode("tw");
    expect(useThemeStore.getState().marketColorMode).toBe("tw");
    expect(document.documentElement.getAttribute("data-market-colors")).toBe("tw");
  });

  it("setMarketColorMode('intl') stamps data-market-colors='intl'", () => {
    useThemeStore.getState().setMarketColorMode("intl");
    expect(document.documentElement.getAttribute("data-market-colors")).toBe("intl");
  });

  it("setMarketColorMode persists to localStorage", () => {
    useThemeStore.getState().setMarketColorMode("tw");
    expect(localStorage.getItem("marketColorMode")).toBe("tw");
    useThemeStore.getState().setMarketColorMode("auto");
    expect(localStorage.getItem("marketColorMode")).toBe("auto");
  });

  it("'auto' resolves 'tw' for zh-* locales and 'intl' otherwise", () => {
    // jsdom navigator.language is "en-US" → intl
    useThemeStore.getState().setMarketColorMode("auto");
    expect(document.documentElement.getAttribute("data-market-colors")).toBe("intl");
    // zh-TW cached by the i18next languagedetector → tw
    localStorage.setItem("fincept.locale", "zh-TW");
    applyMarketColors("auto");
    expect(document.documentElement.getAttribute("data-market-colors")).toBe("tw");
  });
});
