/**
 * i18n entry — wires react-i18next with the bundled zh-TW + en JSON resources.
 *
 * Default locale: zh-TW. Fallback: en. Persisted user choice is read from
 * localStorage on boot via i18next-browser-languagedetector and survives reload.
 *
 * Coverage scope: main UI (sidebar, top bar, dashboard, market, portfolio,
 * analytics, AI, watchlist, alerts, macro, screener, stock detail).
 * Out of scope (intentionally left in English for now): toast/error messages,
 * Login / Settings / Admin pages, backend HTTPException details surfaced via
 * `err.response.data.detail`. See CLAUDE.md i18n section for rationale.
 */
import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import LanguageDetector from "i18next-browser-languagedetector";

import en from "./locales/en.json";
import zhTW from "./locales/zh-TW.json";

void i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources: {
      en: { translation: en },
      "zh-TW": { translation: zhTW },
    },
    fallbackLng: "en",
    supportedLngs: ["en", "zh-TW"],
    // First-time visitors with unspecified browser locale get zh-TW.
    // Detected zh-* (any variant) maps to zh-TW; everything else falls back.
    load: "currentOnly",
    detection: {
      order: ["localStorage", "navigator"],
      caches: ["localStorage"],
      lookupLocalStorage: "fincept.locale",
    },
    interpolation: {
      escapeValue: false, // React already escapes
    },
    react: {
      useSuspense: false, // bundled resources, no async load
    },
  });

// Map any zh-* variant to zh-TW; default unknown to zh-TW (per project intent).
const resolved = i18n.language || navigator.language || "";
if (resolved.startsWith("zh") && resolved !== "zh-TW") {
  void i18n.changeLanguage("zh-TW");
} else if (!resolved.startsWith("en") && !resolved.startsWith("zh")) {
  void i18n.changeLanguage("zh-TW");
}

export default i18n;
