import "@testing-library/jest-dom";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

// Initialize i18n synchronously for tests and lock to English so existing
// component tests that assert English copy keep passing. Production code
// boots in zh-TW via i18next-browser-languagedetector + the default we set
// in src/i18n/index.ts.
import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import en from "@/i18n/locales/en.json";
import zhTW from "@/i18n/locales/zh-TW.json";

void i18n.use(initReactI18next).init({
  resources: {
    en: { translation: en },
    "zh-TW": { translation: zhTW },
  },
  lng: "en",
  fallbackLng: "en",
  interpolation: { escapeValue: false },
  react: { useSuspense: false },
});

afterEach(() => {
  cleanup();
});
