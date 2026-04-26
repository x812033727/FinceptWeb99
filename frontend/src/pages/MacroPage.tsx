import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer,
  ReferenceLine, CartesianGrid,
} from "recharts";
import { useTranslation } from "react-i18next";
import api from "@/lib/api";
import { useNavigate } from "react-router-dom";

// ── types ──────────────────────────────────────────────────────────

interface DataPoint {
  date: string;
  value: number | null;
}

// ── indicator config ───────────────────────────────────────────────

const INDICATOR_CONFIGS = [
  { id: "fed_funds_rate", labelKey: "macro.indicators.fed_funds", color: "#6366f1", unit: "%" },
  { id: "10y_yield",      labelKey: "macro.indicators.yield_10y", color: "#22c55e", unit: "%" },
  { id: "2y_yield",       labelKey: "macro.indicators.yield_2y",  color: "#86efac", unit: "%" },
  { id: "10y_minus_2y",   labelKey: "macro.indicators.yield_curve", color: "#f59e0b", unit: "%" },
  { id: "cpi",            labelKey: "macro.indicators.cpi",      color: "#ef4444", unit: "index" },
  { id: "unemployment",   labelKey: "macro.indicators.unemployment", color: "#a78bfa", unit: "%" },
  { id: "gdp",            labelKey: "macro.indicators.gdp",      color: "#34d399", unit: "$B" },
  { id: "usd_index",      labelKey: "macro.indicators.dxy",      color: "#60a5fa", unit: "" },
  { id: "twd_usd",        labelKey: "macro.indicators.twd_usd",  color: "#fb923c", unit: "" },
] as const;

type IndicatorId = typeof INDICATOR_CONFIGS[number]["id"];

// ── API helper ────────────────────────────────────────────────────

const fetchMacro = (id: IndicatorId) =>
  api.get<DataPoint[]>(`/us/macro/${id}`).then((r) => r.data);

// ── sub-components ─────────────────────────────────────────────────

function MiniCard({
  label, value, unit, color, onClick, active,
}: {
  label: string; value: number | null | undefined; unit: string;
  color: string; onClick: () => void; active: boolean;
}) {
  return (
    <button
      onClick={onClick}
      className={`bg-card border rounded-lg p-3 text-left transition-colors ${
        active ? "border-primary/50 ring-1 ring-primary/30" : "border-border hover:border-primary/30"
      }`}
    >
      <div className="text-xs text-muted-foreground mb-1">{label}</div>
      <div className="text-lg font-bold" style={{ color }}>
        {value != null ? `${value.toFixed(2)}${unit ? " " + unit : ""}` : "—"}
      </div>
    </button>
  );
}

function MacroChart({
  indicator, data, color, unit,
}: {
  indicator: string; data: DataPoint[]; color: string; unit: string;
}) {
  const clean = data
    .filter((d) => d.value != null)
    .map((d) => ({ date: d.date.slice(0, 7), value: d.value as number }))
    .slice(-60);   // show last 5 years (monthly) or last 60 points

  if (!clean.length) {
    return (
      <div className="h-64 flex items-center justify-center text-muted-foreground text-sm">
        {/* No data fallback (English left as-is — backend message) */}
        No data — FRED API key not configured
      </div>
    );
  }

  const last = clean[clean.length - 1].value;

  return (
    <div>
      <div className="flex items-baseline gap-2 mb-3">
        <span className="text-2xl font-bold" style={{ color }}>
          {last.toFixed(2)}{unit ? " " + unit : ""}
        </span>
        <span className="text-sm text-muted-foreground">{indicator}</span>
      </div>
      <ResponsiveContainer width="100%" height={220}>
        <LineChart data={clean} margin={{ left: 0, right: 8, top: 4, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
          <XAxis
            dataKey="date"
            tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
            interval="preserveStartEnd"
          />
          <YAxis tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }} width={45} />
          <Tooltip
            contentStyle={{
              background: "hsl(var(--card))",
              border: "1px solid hsl(var(--border))",
              fontSize: 11,
            }}
            formatter={(v: number) => [`${v.toFixed(3)} ${unit}`, indicator]}
          />
          <ReferenceLine y={0} stroke="hsl(var(--muted-foreground))" strokeDasharray="4 4" />
          <Line
            type="monotone"
            dataKey="value"
            stroke={color}
            strokeWidth={2}
            dot={false}
            activeDot={{ r: 4 }}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

// ── main page ──────────────────────────────────────────────────────

export default function MacroPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [active, setActive] = useState<IndicatorId>("10y_minus_2y");

  const activeSpec = INDICATOR_CONFIGS.find((i) => i.id === active)!;
  const activeLabel = t(activeSpec.labelKey);

  // Fetch latest value for each indicator (for KPI cards)
  const queries = INDICATOR_CONFIGS.map((ind) =>
    // eslint-disable-next-line react-hooks/rules-of-hooks
    useQuery({
      queryKey: ["macro", ind.id],
      queryFn: () => fetchMacro(ind.id),
      staleTime: 3_600_000,
    })
  );

  const latestValues = INDICATOR_CONFIGS.reduce((acc, ind, i) => {
    const data = queries[i].data;
    const last = data?.filter((d) => d.value != null).at(-1);
    acc[ind.id] = last?.value ?? null;
    return acc;
  }, {} as Record<string, number | null>);

  const activeData = queries[INDICATOR_CONFIGS.findIndex((i) => i.id === active)].data ?? [];

  function analyseWithAI() {
    const context = INDICATOR_CONFIGS.reduce((acc, ind) => {
      acc[t(ind.labelKey)] = latestValues[ind.id];
      return acc;
    }, {} as Record<string, number | null>);

    // Navigate to AI page with macro context pre-loaded via URL state
    navigate("/ai", {
      state: {
        agentId: "macro_analyst",
        initialMessage: "Based on the current macro data provided in context, give me a concise analysis of the macro environment and its implications for US and Taiwan equities.",
        context: { macro_indicators: context },
      },
    });
  }

  return (
    <div className="p-4 sm:p-6 space-y-5 sm:space-y-6">
      {/* header */}
      <div className="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-3">
        <div>
          <h1 className="text-xl sm:text-2xl font-bold text-foreground">{t("macro.title")}</h1>
          <p className="text-xs sm:text-sm text-muted-foreground mt-1">
            {t("macro.subtitle")}
          </p>
        </div>
        <button
          onClick={analyseWithAI}
          className="self-start px-3 sm:px-4 py-2 bg-primary/10 border border-primary/30 text-primary rounded-lg text-sm hover:bg-primary/20 transition-colors whitespace-nowrap"
        >
          🤖 {t("nav.ai")}
        </button>
      </div>

      {/* KPI cards */}
      <div className="grid grid-cols-3 sm:grid-cols-5 lg:grid-cols-9 gap-2">
        {INDICATOR_CONFIGS.map((ind) => (
          <MiniCard
            key={ind.id}
            label={t(ind.labelKey)}
            value={latestValues[ind.id]}
            unit={ind.unit}
            color={ind.color}
            active={active === ind.id}
            onClick={() => setActive(ind.id)}
          />
        ))}
      </div>

      {/* main chart */}
      <div className="bg-card border border-border rounded-lg p-5">
        <MacroChart
          indicator={activeLabel}
          data={activeData}
          color={activeSpec.color}
          unit={activeSpec.unit}
        />
      </div>
    </div>
  );
}
