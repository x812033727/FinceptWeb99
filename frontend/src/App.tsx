import { useEffect, useState } from "react";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { useAuthStore } from "@/store/authStore";
import { silentRefresh } from "@/lib/auth";

import AppLayout from "@/components/layout/AppLayout";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import Toaster from "@/components/Toaster";
import LoginPage from "@/pages/LoginPage";
import DashboardPage from "@/pages/DashboardPage";
import MarketPage from "@/pages/MarketPage";
import StockDetailPage from "@/pages/StockDetailPage";
import ScreenerPage from "@/pages/ScreenerPage";
import PortfolioPage from "@/pages/PortfolioPage";
import AnalyticsPage from "@/pages/AnalyticsPage";
import MacroPage from "@/pages/MacroPage";
import WatchlistPage from "@/pages/WatchlistPage";
import AIPage from "@/pages/AIPage";
import AlertsPage from "@/pages/AlertsPage";
import SettingsPage from "@/pages/SettingsPage";
import AdminPage from "@/pages/AdminPage";

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
            <Route path="/dashboard" element={<ErrorBoundary><DashboardPage /></ErrorBoundary>} />
            <Route path="/market/:market" element={<ErrorBoundary><MarketPage /></ErrorBoundary>} />
            <Route path="/stock/:market/:symbol" element={<ErrorBoundary><StockDetailPage /></ErrorBoundary>} />
            <Route path="/screener" element={<ErrorBoundary><ScreenerPage /></ErrorBoundary>} />
            <Route path="/portfolio" element={<ErrorBoundary><PortfolioPage /></ErrorBoundary>} />
            <Route path="/analytics" element={<RequireRole role="analyst"><ErrorBoundary><AnalyticsPage /></ErrorBoundary></RequireRole>} />
            <Route path="/macro" element={<ErrorBoundary><MacroPage /></ErrorBoundary>} />
            <Route path="/watchlist" element={<ErrorBoundary><WatchlistPage /></ErrorBoundary>} />
            <Route path="/alerts" element={<ErrorBoundary><AlertsPage /></ErrorBoundary>} />
            <Route path="/ai" element={<ErrorBoundary><AIPage /></ErrorBoundary>} />
            <Route path="/settings" element={<ErrorBoundary><SettingsPage /></ErrorBoundary>} />
            <Route path="/admin" element={<RequireRole role="admin"><ErrorBoundary><AdminPage /></ErrorBoundary></RequireRole>} />
          </Route>

          <Route path="/" element={<Navigate to="/dashboard" replace />} />
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Routes>
      </ErrorBoundary>
    </BrowserRouter>
  );
}
