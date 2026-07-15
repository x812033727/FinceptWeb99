import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "./App";
import "./index.css";
import { clearQueryCacheOnAuthChange } from "@/lib/authQueryCache";
import { clearPushSubscriptionOnAuthChange } from "@/lib/webPush";
import { clearNotificationsOnAuthChange } from "@/store/notificationStore";
import "@/store/themeStore"; // eagerly initialize theme (applies data-light + data-market-colors attributes)
import "@/i18n";              // initialize i18next (en + zh-TW, default zh-TW)

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      retry: 1,
    },
  },
});

clearQueryCacheOnAuthChange(queryClient);
clearPushSubscriptionOnAuthChange();
clearNotificationsOnAuthChange();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </StrictMode>
);

// ── Deploy-time lazy-chunk self-healing ─────────────────────────────
// After a release, hashed chunk filenames change; a tab that loaded the
// old index.html will 404 when it lazily imports a page chunk. Vite
// surfaces that as a cancellable "vite:preloadError" window event.
// Recovery: reload once so the browser picks up the new index.html
// (the service worker's per-version caches make the fresh assets
// available immediately). A sessionStorage timestamp guards against
// reload loops — if a reload didn't fix it (e.g. the server is down),
// we let the error propagate to the nearest ErrorBoundary instead of
// spinning.
const PRELOAD_RELOAD_KEY = "fincept:preload-error-reloaded-at";
window.addEventListener("vite:preloadError", (event) => {
  const last = Number(sessionStorage.getItem(PRELOAD_RELOAD_KEY) ?? 0);
  if (Date.now() - last < 30_000) return; // recently reloaded — don't loop
  sessionStorage.setItem(PRELOAD_RELOAD_KEY, String(Date.now()));
  event.preventDefault(); // swallow the failed import; the reload supersedes it
  window.location.reload();
});

// Register service worker for PWA offline support. The version query string
// changes on every release, which (a) forces the browser to fetch the new
// sw.js and (b) is read inside the worker to scope cache names per build,
// so old caches are evicted on activate.
declare const __APP_VERSION__: string;
if ("serviceWorker" in navigator && import.meta.env.PROD) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register(`/sw.js?v=${__APP_VERSION__}`).catch(() => {
      // SW registration is best-effort; ignore failures
    });
  });
}

// Core Web Vitals reporter — only in PROD so dev-build perf doesn't
// pollute the Prometheus histograms with noisy numbers.
if (import.meta.env.PROD) {
  void import("@/lib/webVitals").then(({ registerWebVitals }) => registerWebVitals());
}
