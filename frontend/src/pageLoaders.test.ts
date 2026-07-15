/**
 * Unit tests for pageLoaders.prefetchPage.
 *
 * The module keeps a module-level Set of attempted prefetch keys so a
 * second call for the same path is a no-op. vi.resetModules() + a
 * dynamic import in beforeEach gives each test a fresh module so the
 * Set doesn't leak across cases.
 *
 * The page chunks themselves are stubbed via vi.mock so the tests
 * don't spin up real React component imports — they just need to know
 * that the loader was invoked the expected number of times.
 */
import { vi, describe, it, expect, beforeEach } from "vitest";

const dashboardLoad = vi.fn(() => Promise.resolve({}));
const marketLoad = vi.fn(() => Promise.resolve({}));
const stockDetailLoad = vi.fn(() => Promise.resolve({}));
const screenerLoad = vi.fn(() => Promise.resolve({}));
const comparisonLoad = vi.fn(() => Promise.resolve({}));
const portfolioLoad = vi.fn(() => Promise.resolve({}));
const analyticsLoad = vi.fn(() => Promise.resolve({}));
const macroLoad = vi.fn(() => Promise.resolve({}));
const watchlistLoad = vi.fn(() => Promise.resolve({}));
const aiLoad = vi.fn(() => Promise.resolve({}));
const alertsLoad = vi.fn(() => Promise.resolve({}));
const settingsLoad = vi.fn(() => Promise.resolve({}));
const adminLoad = vi.fn(() => Promise.resolve({}));

vi.mock("@/pages/DashboardPage", () => ({ default: () => null, __load: dashboardLoad }));
vi.mock("@/pages/MarketPage", () => ({ default: () => null, __load: marketLoad }));
vi.mock("@/pages/StockDetailPage", () => ({ default: () => null, __load: stockDetailLoad }));
vi.mock("@/pages/ScreenerPage", () => ({ default: () => null, __load: screenerLoad }));
vi.mock("@/pages/ComparisonPage", () => ({ default: () => null, __load: comparisonLoad }));
vi.mock("@/pages/PortfolioPage", () => ({ default: () => null, __load: portfolioLoad }));
vi.mock("@/pages/AnalyticsPage", () => ({ default: () => null, __load: analyticsLoad }));
vi.mock("@/pages/MacroPage", () => ({ default: () => null, __load: macroLoad }));
vi.mock("@/pages/WatchlistPage", () => ({ default: () => null, __load: watchlistLoad }));
vi.mock("@/pages/AIPage", () => ({ default: () => null, __load: aiLoad }));
vi.mock("@/pages/AlertsPage", () => ({ default: () => null, __load: alertsLoad }));
vi.mock("@/pages/SettingsPage", () => ({ default: () => null, __load: settingsLoad }));
vi.mock("@/pages/AdminPage", () => ({ default: () => null, __load: adminLoad }));

type PageLoaders = typeof import("./pageLoaders");

describe("prefetchPage", () => {
  let prefetchPage: PageLoaders["prefetchPage"];
  let pageLoaders: PageLoaders["pageLoaders"];
  // Spy applied to each loader inside the module, restored before the
  // next test so call counts don't leak.
  let spies: ReturnType<typeof vi.spyOn>[] = [];

  beforeEach(async () => {
    vi.resetModules();
    const mod = await import("./pageLoaders");
    prefetchPage = mod.prefetchPage;
    pageLoaders = mod.pageLoaders;
    spies = (Object.keys(pageLoaders) as (keyof typeof pageLoaders)[]).map((k) =>
      vi.spyOn(pageLoaders, k).mockImplementation(() => Promise.resolve({} as never))
    );
  });

  function lastCallCount(key: keyof typeof pageLoaders): number {
    const idx = (Object.keys(pageLoaders) as (keyof typeof pageLoaders)[]).indexOf(key);
    return spies[idx].mock.calls.length;
  }

  it.each([
    ["/dashboard", "dashboard"],
    ["/market/US", "market"],
    ["/market/TW", "market"],
    ["/market/CRYPTO", "market"],
    ["/stock/US/AAPL", "stockDetail"],
    ["/stock/TW/2330", "stockDetail"],
    ["/screener", "screener"],
    ["/compare", "comparison"],
    ["/portfolio", "portfolio"],
    ["/analytics", "analytics"],
    ["/macro", "macro"],
    ["/watchlist", "watchlist"],
    ["/ai", "ai"],
    ["/alerts", "alerts"],
    ["/settings", "settings"],
    ["/admin", "admin"],
  ] as const)("path %s triggers loader %s", (path, expectedKey) => {
    prefetchPage(path);
    expect(lastCallCount(expectedKey)).toBe(1);
  });

  it("is idempotent — calling the same path twice still loads once", () => {
    prefetchPage("/portfolio");
    prefetchPage("/portfolio");
    prefetchPage("/portfolio");
    expect(lastCallCount("portfolio")).toBe(1);
  });

  it("ignores unknown paths", () => {
    prefetchPage("/this-route-does-not-exist");
    prefetchPage("/login"); // explicitly not lazy-loaded
    prefetchPage("");
    for (const key of Object.keys(pageLoaders) as (keyof typeof pageLoaders)[]) {
      expect(lastCallCount(key)).toBe(0);
    }
  });

  it("re-allows prefetch after a failed load so navigation can retry", async () => {
    // First call fails. The Set entry must be cleared so a second
    // attempt actually re-invokes the loader.
    spies[Object.keys(pageLoaders).indexOf("ai")].mockReset().mockRejectedValueOnce(new Error("net"));
    spies[Object.keys(pageLoaders).indexOf("ai")].mockResolvedValueOnce({} as never);

    prefetchPage("/ai");
    // Wait for the failed promise to settle and the cleanup .catch to run.
    await new Promise((r) => setTimeout(r, 0));
    prefetchPage("/ai");
    expect(lastCallCount("ai")).toBe(2);
  });

  it("treats /stock/* with a trailing query / fragment as stockDetail", () => {
    prefetchPage("/stock/US/AAPL?from=watchlist");
    expect(lastCallCount("stockDetail")).toBe(1);
  });
});
