import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "./App";
import "./index.css";
import "@/store/themeStore"; // eagerly initialize theme (applies data-light attribute)
import "@/i18n";              // initialize i18next (en + zh-TW, default zh-TW)

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      retry: 1,
    },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </StrictMode>
);

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
