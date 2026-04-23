import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import axios from "axios";

// ── Health check page (Phase 0 placeholder) ──────────────────────
function HealthPage() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["health"],
    queryFn: () => axios.get("/api/health").then((r) => r.data),
    refetchInterval: 30_000,
  });

  return (
    <div className="min-h-screen flex items-center justify-center">
      <div className="bg-card border border-border rounded-lg p-8 max-w-md w-full space-y-4">
        <h1 className="text-2xl font-bold text-primary">Fincept Web Terminal</h1>
        <p className="text-muted-foreground text-sm">Phase 0 — Infrastructure</p>

        <div className="space-y-2">
          <div className="flex items-center justify-between text-sm">
            <span className="text-foreground">API Server</span>
            {isLoading ? (
              <span className="text-muted-foreground">checking…</span>
            ) : isError ? (
              <span className="text-negative">offline</span>
            ) : (
              <span className="text-positive">{data?.status}</span>
            )}
          </div>

          <div className="flex items-center justify-between text-sm">
            <span className="text-foreground">Redis</span>
            {isLoading ? (
              <span className="text-muted-foreground">checking…</span>
            ) : isError ? (
              <span className="text-negative">offline</span>
            ) : (
              <span className={data?.redis === "ok" ? "text-positive" : "text-negative"}>
                {data?.redis ?? "—"}
              </span>
            )}
          </div>

          <div className="flex items-center justify-between text-sm">
            <span className="text-foreground">Version</span>
            <span className="text-muted-foreground">{data?.version ?? "—"}</span>
          </div>
        </div>

        <p className="text-xs text-muted-foreground pt-2 border-t border-border">
          Full UI will be available from Phase 9. Auth, market data, and analytics are being built in
          preceding phases.
        </p>
      </div>
    </div>
  );
}

// ── Router ────────────────────────────────────────────────────────
// Phase 1+ will add: /login, /dashboard, /stock/:market/:symbol,
// /screener, /portfolio, /analytics, /ai
export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<HealthPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
