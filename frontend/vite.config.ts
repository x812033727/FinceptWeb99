/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";
import { readFileSync } from "fs";
import { configDefaults } from "vitest/config";

const pkg = JSON.parse(readFileSync(path.resolve(__dirname, "package.json"), "utf-8"));

// ── Vendor chunking ─────────────────────────────────────────────────
// Group heavyweight, rarely-changing vendor code into stable named
// chunks so (a) the entry chunk shrinks and (b) a release that only
// touches app code doesn't invalidate the browser cache for React,
// recharts, etc.
//
// Matching uses the /node_modules/<pkg>/ boundary — a naive
// id.includes("recharts") would also match "recharts-scale" or an
// unlucky project path segment.
//
// IMPORTANT: every package listed under a lazy-loaded group
// (vendor-recharts / vendor-lwc) must be used ONLY by that library.
// If a package here were also imported by boot-path code, the boot
// chunk would drag the whole vendor group in. Verified via `npm ls`:
// lodash, react-is, tiny-invariant, eventemitter3, fast-equals,
// react-smooth, react-transition-group, dom-helpers, recharts-scale,
// decimal.js-light, victory-vendor and all d3-* come in solely via
// recharts; fancy-canvas solely via lightweight-charts.
const VENDOR_GROUPS: Record<string, readonly string[]> = {
  // Boot-critical framework code — statically imported by the entry,
  // so Vite emits it with the initial HTML (script/modulepreload).
  "vendor-react": [
    "react",
    "react-dom",
    "scheduler",
    "react-router-dom",
    "react-router",
    "@remix-run/router",
    "zustand",
    "@tanstack/react-query",
    "@tanstack/query-core",
    "i18next",
    "react-i18next",
    "i18next-browser-languagedetector",
    "html-parse-stringify",
    "void-elements",
    "use-sync-external-store",
    // clsx is shared by recharts AND the app's cn() helper (boot path).
    // Left unassigned, Rollup merges it into vendor-recharts, which
    // makes the entry statically import the whole 440 kB recharts
    // chunk. Pin it here so the boot path stays recharts-free.
    "clsx",
  ],
  // recharts + its d3/victory dependency cone (lazy: chart pages only)
  "vendor-recharts": [
    "recharts",
    "recharts-scale",
    "victory-vendor",
    "react-smooth",
    "react-transition-group",
    "dom-helpers",
    "fast-equals",
    "eventemitter3",
    "decimal.js-light",
    "internmap",
    "react-is",
    "tiny-invariant",
    "lodash",
  ],
  // lightweight-charts (lazy: candlestick views)
  "vendor-lwc": ["lightweight-charts", "fancy-canvas"],
};

function vendorChunk(id: string): string | undefined {
  if (!id.includes("node_modules")) return undefined;
  // Scoped package family: everything under @radix-ui/*
  if (id.includes("/node_modules/@radix-ui/")) return "vendor-radix";
  // All d3-* packages present are recharts' (via victory-vendor)
  if (id.includes("/node_modules/d3-")) return "vendor-recharts";
  for (const [chunk, pkgs] of Object.entries(VENDOR_GROUPS)) {
    if (pkgs.some((p) => id.includes(`/node_modules/${p}/`))) return chunk;
  }
  return undefined; // everything else: Rollup's default chunking
}

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
    chunkSizeWarningLimit: 800,
    rollupOptions: {
      output: {
        manualChunks: vendorChunk,
      },
    },
  },
  server: {
    port: 5173,
    // Playwright writes traces, screenshots and videos under output/.
    // Watching those artifacts reloads the page in the middle of an auth
    // journey and can replace the in-memory access token via silentRefresh.
    watch: { ignored: ["**/output/**"] },
    proxy: {
      "/api": { target: process.env.VITE_API_TARGET ?? "http://localhost:8000", changeOrigin: true },
      "/ws": { target: (process.env.VITE_API_TARGET ?? "http://localhost:8000").replace(/^http/, "ws"), ws: true, changeOrigin: true },
    },
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
    css: false,
    // Playwright specs use a different runner and must never be collected by
    // Vitest's broad `*.spec.ts` include pattern.
    exclude: [...configDefaults.exclude, "e2e/**"],
  },
});
