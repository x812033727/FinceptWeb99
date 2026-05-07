/**
 * Centralised dynamic-import functions for every authenticated route.
 *
 * Each loader is consumed twice:
 *   1. By React.lazy() in App.tsx to define the route component.
 *   2. By prefetchPage() below, called on nav-link hover / touch /
 *      focus, so the chunk is already in the module cache by the
 *      time the user actually clicks.
 *
 * Both call the same import() function; the bundler dedupes the
 * resulting network request so a prefetched module is reused — not
 * re-fetched — when React.lazy resolves it.
 */

export const pageLoaders = {
  dashboard: () => import("@/pages/DashboardPage"),
  market: () => import("@/pages/MarketPage"),
  stockDetail: () => import("@/pages/StockDetailPage"),
  screener: () => import("@/pages/ScreenerPage"),
  portfolio: () => import("@/pages/PortfolioPage"),
  analytics: () => import("@/pages/AnalyticsPage"),
  macro: () => import("@/pages/MacroPage"),
  watchlist: () => import("@/pages/WatchlistPage"),
  ai: () => import("@/pages/AIPage"),
  discussion: () => import("@/pages/DiscussionPage"),
  lessonLibrary: () => import("@/pages/LessonLibraryPage"),
  alerts: () => import("@/pages/AlertsPage"),
  settings: () => import("@/pages/SettingsPage"),
  admin: () => import("@/pages/AdminPage"),
  finmind: () => import("@/pages/FinmindPage"),
} as const;

/** Map a top-level nav `to` path → a loader key. Returns undefined for
 *  unknown paths (deep links like /stock/US/AAPL aren't in nav, and
 *  prefetching them would require knowing the URL ahead of time). */
function loaderForPath(path: string): keyof typeof pageLoaders | undefined {
  if (path === "/dashboard") return "dashboard";
  if (path.startsWith("/market/")) return "market";
  if (path.startsWith("/stock/")) return "stockDetail";
  if (path === "/screener") return "screener";
  if (path === "/portfolio") return "portfolio";
  if (path === "/analytics") return "analytics";
  if (path === "/macro") return "macro";
  if (path === "/watchlist") return "watchlist";
  if (path === "/ai") return "ai";
  if (path === "/discussion") return "discussion";
  if (path === "/alerts") return "alerts";
  if (path === "/settings") return "settings";
  if (path === "/admin") return "admin";
  if (path === "/finmind") return "finmind";
  return undefined;
}

const prefetched = new Set<string>();

/** Trigger the dynamic import for the page at `path`. No-op if the
 *  chunk has already been requested in this session. Errors are
 *  swallowed — prefetch is a hint, not a guarantee, so a network
 *  blip here shouldn't bubble up to the user. */
export function prefetchPage(path: string): void {
  const key = loaderForPath(path);
  if (!key || prefetched.has(key)) return;
  prefetched.add(key);
  pageLoaders[key]().catch(() => {
    // Failed prefetch — let the cache miss happen on real navigation
    // so React.lazy's own error path can handle it properly.
    prefetched.delete(key);
  });
}
