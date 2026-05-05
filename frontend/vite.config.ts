/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";
import { readFileSync } from "fs";

const pkg = JSON.parse(readFileSync(path.resolve(__dirname, "package.json"), "utf-8"));

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  define: {
    // Stamped into the bundle so the service worker registration can pass
    // the version as a query string and bust old caches on each release.
    __APP_VERSION__: JSON.stringify(pkg.version),
  },
  build: {
    rollupOptions: {
      output: {
        // Pull large vendor libs out of the main entry chunk so:
        //   1. The 500 kB warning goes away.
        //   2. App-code edits don't bust the long-lived vendor chunks
        //      (better browser cache hit-rate across releases).
        // Recharts + lightweight-charts already lazy-load with the
        // pages that import them; we only split libs the entry pulls in.
        manualChunks: {
          "react-vendor": ["react", "react-dom", "react-router-dom"],
          "tanstack-vendor": [
            "@tanstack/react-query",
            "@tanstack/react-table",
            "@tanstack/react-virtual",
          ],
          "radix-vendor": [
            "@radix-ui/react-dialog",
            "@radix-ui/react-dropdown-menu",
            "@radix-ui/react-popover",
            "@radix-ui/react-select",
            "@radix-ui/react-slot",
            "@radix-ui/react-tabs",
            "@radix-ui/react-visually-hidden",
            "cmdk",
          ],
          "i18n-vendor": [
            "i18next",
            "i18next-browser-languagedetector",
            "react-i18next",
          ],
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://localhost:8000", changeOrigin: true },
      "/ws": { target: "ws://localhost:8000", ws: true, changeOrigin: true },
    },
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
    css: false,
  },
});
