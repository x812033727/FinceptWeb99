import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import api from "@/lib/api";

interface Alert {
  id: string;
  symbol: string;
  market: "US" | "TW";
  condition: "above" | "below";
  target_price: number;
  triggered: boolean;
  triggered_at: string | null;
  created_at: string;
}

interface AlertCreate {
  symbol: string;
  market: "US" | "TW";
  condition: "above" | "below";
  target_price: number;
}

const EMPTY: AlertCreate = { symbol: "", market: "US", condition: "above", target_price: 0 };

export default function AlertsPage() {
  const qc = useQueryClient();
  const [form, setForm] = useState<AlertCreate>(EMPTY);
  const [error, setError] = useState("");

  const { data: alerts = [], isLoading } = useQuery<Alert[]>({
    queryKey: ["alerts"],
    queryFn: () => api.get("/api/alerts").then((r) => r.data),
  });

  const createMut = useMutation({
    mutationFn: (body: AlertCreate) => api.post("/api/alerts", body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["alerts"] });
      setForm(EMPTY);
      setError("");
    },
    onError: () => setError("Failed to create alert."),
  });

  const deleteMut = useMutation({
    mutationFn: (id: string) => api.delete(`/api/alerts/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["alerts"] }),
  });

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!form.symbol.trim()) { setError("Symbol is required."); return; }
    if (form.target_price <= 0) { setError("Target price must be > 0."); return; }
    createMut.mutate({ ...form, symbol: form.symbol.trim().toUpperCase() });
  }

  const active = alerts.filter((a) => !a.triggered);
  const triggered = alerts.filter((a) => a.triggered);

  return (
    <div className="p-6 space-y-6 max-w-3xl">
      <h1 className="text-xl font-semibold">Price Alerts</h1>

      {/* Create form */}
      <form
        onSubmit={handleSubmit}
        className="bg-card border border-border rounded-lg p-4 space-y-3"
      >
        <h2 className="text-sm font-medium">New Alert</h2>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <div className="space-y-1">
            <label className="text-xs text-muted-foreground">Symbol</label>
            <input
              className="w-full bg-background border border-border rounded px-2 py-1.5 text-sm"
              placeholder="AAPL"
              value={form.symbol}
              onChange={(e) => setForm((f) => ({ ...f, symbol: e.target.value }))}
            />
          </div>
          <div className="space-y-1">
            <label className="text-xs text-muted-foreground">Market</label>
            <select
              className="w-full bg-background border border-border rounded px-2 py-1.5 text-sm"
              value={form.market}
              onChange={(e) => setForm((f) => ({ ...f, market: e.target.value as "US" | "TW" }))}
            >
              <option value="US">US</option>
              <option value="TW">TW</option>
            </select>
          </div>
          <div className="space-y-1">
            <label className="text-xs text-muted-foreground">Condition</label>
            <select
              className="w-full bg-background border border-border rounded px-2 py-1.5 text-sm"
              value={form.condition}
              onChange={(e) =>
                setForm((f) => ({ ...f, condition: e.target.value as "above" | "below" }))
              }
            >
              <option value="above">Price above</option>
              <option value="below">Price below</option>
            </select>
          </div>
          <div className="space-y-1">
            <label className="text-xs text-muted-foreground">Target Price</label>
            <input
              type="number"
              min="0"
              step="0.01"
              className="w-full bg-background border border-border rounded px-2 py-1.5 text-sm"
              value={form.target_price || ""}
              onChange={(e) =>
                setForm((f) => ({ ...f, target_price: parseFloat(e.target.value) || 0 }))
              }
            />
          </div>
        </div>
        {error && <p className="text-xs text-red-400">{error}</p>}
        <button
          type="submit"
          disabled={createMut.isPending}
          className="px-4 py-1.5 rounded bg-primary text-primary-foreground text-sm font-medium disabled:opacity-50"
        >
          {createMut.isPending ? "Creating…" : "Create Alert"}
        </button>
      </form>

      {/* Active alerts */}
      <section className="space-y-2">
        <h2 className="text-sm font-medium">
          Active <span className="text-muted-foreground">({active.length})</span>
        </h2>
        {isLoading && <p className="text-xs text-muted-foreground">Loading…</p>}
        {!isLoading && active.length === 0 && (
          <p className="text-xs text-muted-foreground">No active alerts.</p>
        )}
        {active.map((a) => (
          <AlertRow key={a.id} alert={a} onDelete={() => deleteMut.mutate(a.id)} />
        ))}
      </section>

      {/* Triggered alerts */}
      {triggered.length > 0 && (
        <section className="space-y-2">
          <h2 className="text-sm font-medium text-muted-foreground">
            Triggered <span>({triggered.length})</span>
          </h2>
          {triggered.map((a) => (
            <AlertRow key={a.id} alert={a} onDelete={() => deleteMut.mutate(a.id)} />
          ))}
        </section>
      )}
    </div>
  );
}

function AlertRow({ alert: a, onDelete }: { alert: Alert; onDelete: () => void }) {
  return (
    <div
      className={`flex items-center justify-between px-4 py-2.5 rounded border text-sm ${
        a.triggered
          ? "border-border/50 bg-card/50 opacity-60"
          : "border-border bg-card"
      }`}
    >
      <div className="flex items-center gap-3">
        <span className="font-medium w-16">{a.symbol}</span>
        <span className="text-xs text-muted-foreground bg-accent/20 px-1.5 py-0.5 rounded">
          {a.market}
        </span>
        <span className="text-muted-foreground">
          price{" "}
          <span className={a.condition === "above" ? "text-green-400" : "text-red-400"}>
            {a.condition}
          </span>{" "}
          <span className="text-foreground font-medium">
            {a.market === "TW"
              ? a.target_price.toLocaleString()
              : `$${a.target_price.toFixed(2)}`}
          </span>
        </span>
        {a.triggered && (
          <span className="text-xs text-amber-400 border border-amber-400/30 px-1.5 py-0.5 rounded">
            triggered
          </span>
        )}
      </div>
      <button
        onClick={onDelete}
        className="text-muted-foreground hover:text-red-400 transition-colors text-lg leading-none"
        title="Delete alert"
      >
        ×
      </button>
    </div>
  );
}
