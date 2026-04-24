import { useEffect, useState } from "react";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { useAuthStore } from "@/store/authStore";
import { silentRefresh } from "@/lib/auth";

import AppLayout from "@/components/layout/AppLayout";
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

// ── Protected route ───────────────────────────────────────────────
function RequireAuth({ children }: { children: React.ReactNode }) {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated());
  if (!isAuthenticated) return <Navigate to="/login" replace />;
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
          <Route path="/dashboard" element={<DashboardPage />} />
          <Route path="/market/:market" element={<MarketPage />} />
          <Route path="/stock/:market/:symbol" element={<StockDetailPage />} />
          <Route path="/screener" element={<ScreenerPage />} />
          <Route path="/portfolio" element={<PortfolioPage />} />
          <Route path="/analytics" element={<AnalyticsPage />} />
          <Route path="/macro" element={<MacroPage />} />
          <Route path="/watchlist" element={<WatchlistPage />} />
          <Route path="/alerts" element={<AlertsPage />} />
          <Route path="/ai" element={<AIPage />} />
        </Route>

        <Route path="/" element={<Navigate to="/dashboard" replace />} />
        <Route path="*" element={<Navigate to="/dashboard" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
