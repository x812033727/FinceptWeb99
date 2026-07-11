import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import api from "@/lib/api";

interface Alert {
  id: string;
  symbol: string;
  market: "US" | "TW" | "CRYPTO";
  condition: "above" | "below";
  target_price: number;
  triggered: boolean;
  triggered_at: string | null;
  created_at: string;
}

interface AlertCreate {
  symbol: string;
  market: "US" | "TW" | "CRYPTO";
  condition: "above" | "below";
  target_price: number;
}

interface AlertEvent {
  id: string;
  alert_id: string | null;
  symbol: string;
  market: string;
  kind: "price" | "strategy_health" | string;
  message: string;
  fired_at: string;
  payload: Record<string, unknown> | null;
}

const EMPTY: AlertCreate = { symbol: "", market: "US", condition: "above", target_price: 0 };

export default function AlertsPage() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [form, setForm] = useState<AlertCreate>(EMPTY);
  const [error, setError] = useState("");

  const { data: alerts = [], isLoading } = useQuery<Alert[]>({
    queryKey: ["alerts"],
    queryFn: () => api.get("/alerts").then((r) => r.data),
  });

  const { data: history = [], isLoading: historyLoading } = useQuery<AlertEvent[]>({
    queryKey: ["alerts", "history"],
    queryFn: () => api.get("/alerts/history?limit=50").then((r) => r.data),
  });

  const createMut = useMutation({
    mutationFn: (body: AlertCreate) => api.post("/alerts", body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["alerts"] });
      setForm(EMPTY);
      setError("");
    },
    onError: () => setError(t("common.error")),
  });

  const deleteMut = useMutation({
    mutationFn: (id: string) => api.delete(`/alerts/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["alerts"] }),
  });

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!form.symbol.trim()) { setError(t("alerts.symbol")); return; }
    if (form.target_price <= 0) { setError(t("alerts.target_price")); return; }
    createMut.mutate({ ...form, symbol: form.symbol.trim().toUpperCase() });
  }

  const active = alerts.filter((a) => !a.triggered);
  const triggered = alerts.filter((a) => a.triggered);

  return (
    <div className="p-4 sm:p-6 space-y-5 sm:space-y-6 max-w-3xl">
      <h1 className="text-xl sm:text-2xl font-semibold">{t("alerts.title")}</h1>

      {/* Create form */}
      <form
        onSubmit={handleSubmit}
        className="bg-card border border-border rounded-lg p-4 space-y-3"
      >
        <h2 className="text-sm font-medium">{t("alerts.create")}</h2>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <div className="space-y-1">
            <label className="text-xs text-muted-foreground">{t("alerts.symbol")}</label>
            <input
              className="w-full bg-background border border-border rounded px-2 py-1.5 text-sm"
              placeholder="AAPL"
              value={form.symbol}
              onChange={(e) => setForm((f) => ({ ...f, symbol: e.target.value }))}
            />
          </div>
          <div className="space-y-1">
            <label className="text-xs text-muted-foreground">{t("alerts.market")}</label>
            <select
              className="w-full bg-background border border-border rounded px-2 py-1.5 text-sm"
              value={form.market}
              onChange={(e) => setForm((f) => ({ ...f, market: e.target.value as "US" | "TW" | "CRYPTO" }))}
            >
              <option value="US">US</option>
              <option value="TW">TW</option>
              <option value="CRYPTO">CRYPTO</option>
            </select>
          </div>
          <div className="space-y-1">
            <label className="text-xs text-muted-foreground">{t("alerts.condition")}</label>
            <select
              className="w-full bg-background border border-border rounded px-2 py-1.5 text-sm"
              value={form.condition}
              onChange={(e) =>
                setForm((f) => ({ ...f, condition: e.target.value as "above" | "below" }))
              }
            >
              <option value="above">{t("alerts.above")}</option>
              <option value="below">{t("alerts.below")}</option>
            </select>
          </div>
          <div className="space-y-1">
            <label className="text-xs text-muted-foreground">{t("alerts.target_price")}</label>
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
        {error && <p className="text-xs text-danger">{error}</p>}
        <button
          type="submit"
          disabled={createMut.isPending}
          className="px-4 py-1.5 rounded bg-primary text-primary-foreground text-sm font-medium disabled:opacity-50"
        >
          {createMut.isPending ? t("common.saving") : t("alerts.create")}
        </button>
      </form>

      {/* Active alerts */}
      <section className="space-y-2">
        <h2 className="text-sm font-medium">
          {t("alerts.active")} <span className="text-muted-foreground">({active.length})</span>
        </h2>
        {isLoading && <p className="text-xs text-muted-foreground">{t("common.loading")}</p>}
        {!isLoading && active.length === 0 && (
          <p className="text-xs text-muted-foreground">{t("alerts.no_alerts")}</p>
        )}
        {active.map((a) => (
          <AlertRow key={a.id} alert={a} onDelete={() => deleteMut.mutate(a.id)} />
        ))}
      </section>

      {/* Triggered alerts */}
      {triggered.length > 0 && (
        <section className="space-y-2">
          <h2 className="text-sm font-medium text-muted-foreground">
            {t("alerts.triggered")} <span>({triggered.length})</span>
          </h2>
          {triggered.map((a) => (
            <AlertRow key={a.id} alert={a} onDelete={() => deleteMut.mutate(a.id)} />
          ))}
        </section>
      )}

      {/* Fired-alert history (D5) */}
      <section className="space-y-2">
        <h2 className="text-sm font-medium">
          {t("alerts.history")} <span className="text-muted-foreground">({history.length})</span>
        </h2>
        {historyLoading && <p className="text-xs text-muted-foreground">{t("common.loading")}</p>}
        {!historyLoading && history.length === 0 && (
          <p className="text-xs text-muted-foreground">{t("alerts.no_history")}</p>
        )}
        {history.map((ev) => (
          <HistoryRow key={ev.id} event={ev} />
        ))}
      </section>
    </div>
  );
}

function HistoryRow({ event: ev }: { event: AlertEvent }) {
  const { t } = useTranslation();
  const isStrategyHealth = ev.kind === "strategy_health";
  return (
    <div className="flex items-center gap-3 px-4 py-2.5 rounded border border-border bg-card text-sm">
      <span className="text-xs text-muted-foreground whitespace-nowrap">
        {new Date(ev.fired_at).toLocaleString()}
      </span>
      {isStrategyHealth ? (
        <span className="text-xs text-warning border border-warning/30 px-1.5 py-0.5 rounded whitespace-nowrap">
          {t("alerts.kind_strategy_health")}
        </span>
      ) : (
        <span className="text-xs text-muted-foreground bg-accent/20 px-1.5 py-0.5 rounded whitespace-nowrap">
          {t("alerts.kind_price")}
        </span>
      )}
      <span className="font-medium">{ev.symbol}</span>
      <span className="text-muted-foreground truncate">{ev.message}</span>
    </div>
  );
}

function AlertRow({ alert: a, onDelete }: { alert: Alert; onDelete: () => void }) {
  const { t } = useTranslation();
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
          <span className={a.condition === "above" ? "text-up" : "text-down"}>
            {a.condition === "above" ? t("alerts.above") : t("alerts.below")}
          </span>{" "}
          <span className="text-foreground font-medium">
            {a.market === "TW"
              ? a.target_price.toLocaleString()
              : `$${a.target_price.toFixed(2)}`}
          </span>
        </span>
        {a.triggered && (
          <span className="text-xs text-warning border border-warning/30 px-1.5 py-0.5 rounded">
            {t("alerts.triggered")}
          </span>
        )}
      </div>
      <button
        onClick={onDelete}
        className="text-muted-foreground hover:text-danger transition-colors text-lg leading-none"
        title={t("alerts.delete")}
      >
        ×
      </button>
    </div>
  );
}
