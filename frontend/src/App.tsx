import { lazy, Suspense, useEffect, useState } from "react";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { useAuthStore } from "@/store/authStore";
import { silentRefresh } from "@/lib/auth";

import AppLayout from "@/components/layout/AppLayout";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import Toaster from "@/components/Toaster";
import LoginPage from "@/pages/LoginPage";

// Authenticated pages are split into their own chunks so the initial
// bundle doesn't pay for code paths the user hasn't reached yet. The
// login page stays in the main chunk because it's the first thing
// every unauthenticated visitor renders.
const DashboardPage = lazy(() => import("@/pages/DashboardPage"));
const MarketPage = lazy(() => import("@/pages/MarketPage"));
const StockDetailPage = lazy(() => import("@/pages/StockDetailPage"));
const ScreenerPage = lazy(() => import("@/pages/ScreenerPage"));
const PortfolioPage = lazy(() => import("@/pages/PortfolioPage"));
const AnalyticsPage = lazy(() => import("@/pages/AnalyticsPage"));
const MacroPage = lazy(() => import("@/pages/MacroPage"));
const WatchlistPage = lazy(() => import("@/pages/WatchlistPage"));
const AIPage = lazy(() => import("@/pages/AIPage"));
const AlertsPage = lazy(() => import("@/pages/AlertsPage"));
const SettingsPage = lazy(() => import("@/pages/SettingsPage"));
const AdminPage = lazy(() => import("@/pages/AdminPage"));

// ── Protected route ───────────────────────────────────────────────
function RequireAuth({ children }: { children: React.ReactNode }) {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated());
  if (!isAuthenticated) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

type Role = "viewer" | "analyst" | "admin";

// Role hierarchy: admin >= analyst >= viewer. Pages that require analyst
// will accept admin too. Backend remains the source of truth — this guard
// only prevents the client from rendering features the user obviously
// can't use.
const ROLE_RANK: Record<Role, number> = { viewer: 0, analyst: 1, admin: 2 };

function RequireRole({ role, children }: { role: Role; children: React.ReactNode }) {
  const user = useAuthStore((s) => s.user);
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated());
  if (!isAuthenticated) return <Navigate to="/login" replace />;
  const userRank = user ? ROLE_RANK[user.role] : -1;
  if (userRank < ROLE_RANK[role]) return <Navigate to="/dashboard" replace />;
  return <>{children}</>;
}

function RouteFallback() {
  return (
    <div className="p-6 text-muted-foreground text-sm animate-pulse">Loading…</div>
  );
}

// ── App shell ─────────────────────────────────────────────────────
export default function App() {
  const [booting, setBooting] = useState(true);

  useEffect(() => {
    silentRefresh().finally(() => setBooting(false));
  }, []);

  if (booting) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="text-muted-foreground text-sm animate-pulse">Loading…</div>
      </div>
    );
  }

  return (
    <BrowserRouter>
      <Toaster />
      <ErrorBoundary>
        <Routes>
          <Route path="/login" element={<LoginPage />} />

          {/* All authenticated pages share the AppLayout (sidebar + main area) */}
          <Route
            element={
              <RequireAuth>
                <AppLayout />
              </RequireAuth>
            }
          >
            <Route path="/dashboard" element={<ErrorBoundary><Suspense fallback={<RouteFallback />}><DashboardPage /></Suspense></ErrorBoundary>} />
            <Route path="/market/:market" element={<ErrorBoundary><Suspense fallback={<RouteFallback />}><MarketPage /></Suspense></ErrorBoundary>} />
            <Route path="/stock/:market/:symbol" element={<ErrorBoundary><Suspense fallback={<RouteFallback />}><StockDetailPage /></Suspense></ErrorBoundary>} />
            <Route path="/screener" element={<ErrorBoundary><Suspense fallback={<RouteFallback />}><ScreenerPage /></Suspense></ErrorBoundary>} />
            <Route path="/portfolio" element={<ErrorBoundary><Suspense fallback={<RouteFallback />}><PortfolioPage /></Suspense></ErrorBoundary>} />
            <Route path="/analytics" element={<RequireRole role="analyst"><ErrorBoundary><Suspense fallback={<RouteFallback />}><AnalyticsPage /></Suspense></ErrorBoundary></RequireRole>} />
            <Route path="/macro" element={<ErrorBoundary><Suspense fallback={<RouteFallback />}><MacroPage /></Suspense></ErrorBoundary>} />
            <Route path="/watchlist" element={<ErrorBoundary><Suspense fallback={<RouteFallback />}><WatchlistPage /></Suspense></ErrorBoundary>} />
            <Route path="/alerts" element={<ErrorBoundary><Suspense fallback={<RouteFallback />}><AlertsPage /></Suspense></ErrorBoundary>} />
            <Route path="/ai" element={<ErrorBoundary><Suspense fallback={<RouteFallback />}><AIPage /></Suspense></ErrorBoundary>} />
            <Route path="/settings" element={<ErrorBoundary><Suspense fallback={<RouteFallback />}><SettingsPage /></Suspense></ErrorBoundary>} />
            <Route path="/admin" element={<RequireRole role="admin"><ErrorBoundary><Suspense fallback={<RouteFallback />}><AdminPage /></Suspense></ErrorBoundary></RequireRole>} />
          </Route>

          <Route path="/" element={<Navigate to="/dashboard" replace />} />
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Routes>
      </ErrorBoundary>
    </BrowserRouter>
  );
}
