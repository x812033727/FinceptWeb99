import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import api from "@/lib/api";
import AlertBuilder, {
  AlertRulePayload,
  ConditionType,
} from "@/components/alerts/AlertBuilder";

interface Alert {
  id: string;
  symbol: string;
  market: "US" | "TW" | "CRYPTO";
  condition: "above" | "below" | null;
  target_price: number | null;
  condition_type: ConditionType;
  params: Record<string, number> | null;
  cooldown_seconds: number;
  repeat: boolean;
  last_fired_at: string | null;
  triggered: boolean;
  triggered_at: string | null;
  created_at: string;
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

export default function AlertsPage() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [serverError, setServerError] = useState("");

  const { data: alerts = [], isLoading } = useQuery<Alert[]>({
    queryKey: ["alerts"],
    queryFn: () => api.get("/alerts").then((r) => r.data),
  });

  const { data: history = [], isLoading: historyLoading } = useQuery<AlertEvent[]>({
    queryKey: ["alerts", "history"],
    queryFn: () => api.get("/alerts/history?limit=50").then((r) => r.data),
  });

  const createMut = useMutation({
    mutationFn: (body: AlertRulePayload) => api.post("/alerts", body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["alerts"] });
      setServerError("");
    },
    onError: () => setServerError(t("common.error")),
  });

  const deleteMut = useMutation({
    mutationFn: (id: string) => api.delete(`/alerts/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["alerts"] }),
  });

  const active = alerts.filter((a) => !a.triggered);
  const triggered = alerts.filter((a) => a.triggered);

  return (
    <div className="p-4 sm:p-6 space-y-5 sm:space-y-6 max-w-3xl">
      <h1 className="text-xl sm:text-2xl font-semibold">{t("alerts.title")}</h1>

      {/* Rule builder (PR-D1) */}
      <AlertBuilder
        onSubmit={(body) => createMut.mutate(body)}
        isPending={createMut.isPending}
        serverError={serverError}
      />

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

function formatPrice(a: Alert): string {
  if (a.target_price == null) return "";
  return a.market === "TW"
    ? a.target_price.toLocaleString()
    : `$${a.target_price.toFixed(2)}`;
}

/** Human-readable condition summary per rule type. */
function RuleSummary({ alert: a }: { alert: Alert }) {
  const { t } = useTranslation();
  const p = a.params ?? {};

  switch (a.condition_type) {
    case "price_above":
    case "price_below": {
      const above = a.condition_type === "price_above";
      return (
        <span className="text-muted-foreground">
          <span className={above ? "text-up" : "text-down"}>
            {above ? t("alerts.above") : t("alerts.below")}
          </span>{" "}
          <span className="text-foreground font-medium">{formatPrice(a)}</span>
        </span>
      );
    }
    case "pct_change_above":
      return (
        <span className="text-up">{t("alerts.summary_pct_above", { pct: p.pct })}</span>
      );
    case "pct_change_below":
      return (
        <span className="text-down">{t("alerts.summary_pct_below", { pct: p.pct })}</span>
      );
    case "breakout_high":
      return (
        <span className="text-up">
          {t("alerts.summary_breakout_high", { days: p.lookback_days ?? 20 })}
        </span>
      );
    case "breakout_low":
      return (
        <span className="text-down">
          {t("alerts.summary_breakout_low", { days: p.lookback_days ?? 20 })}
        </span>
      );
    case "volume_surge":
      return (
        <span className="text-muted-foreground">
          {t("alerts.summary_volume_surge", {
            days: p.lookback_days ?? 20,
            multiple: p.multiple ?? 2,
          })}
        </span>
      );
    case "foreign_net_buy_streak":
      return (
        <span className="text-muted-foreground">
          {t("alerts.summary_streak", { days: p.days })}
        </span>
      );
    default:
      return <span className="text-muted-foreground">{a.condition_type}</span>;
  }
}

function cooldownLabel(seconds: number): string {
  if (seconds >= 86400) return `${Math.round(seconds / 86400)}d`;
  if (seconds >= 3600) return `${Math.round(seconds / 3600)}h`;
  if (seconds >= 60) return `${Math.round(seconds / 60)}m`;
  return "";
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
        <RuleSummary alert={a} />
        {a.repeat && (
          <span className="text-xs text-muted-foreground border border-border px-1.5 py-0.5 rounded whitespace-nowrap">
            {t("alerts.repeat_badge")}
            {a.cooldown_seconds > 0 ? ` · ${cooldownLabel(a.cooldown_seconds)}` : ""}
          </span>
        )}
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
