import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer,
  ReferenceLine, CartesianGrid,
} from "recharts";
import api from "@/lib/api";
import { useNavigate } from "react-router-dom";

// ── types ──────────────────────────────────────────────────────────

interface DataPoint {
  date: string;
  value: number | null;
}

// ── indicator config ───────────────────────────────────────────────

const INDICATORS = [
  { id: "fed_funds_rate", label: "Fed Funds Rate", color: "#6366f1", unit: "%" },
  { id: "10y_yield",      label: "10Y Treasury",  color: "#22c55e", unit: "%" },
  { id: "2y_yield",       label: "2Y Treasury",   color: "#86efac", unit: "%" },
  { id: "10y_minus_2y",   label: "10Y−2Y Spread", color: "#f59e0b", unit: "%" },
  { id: "cpi",            label: "CPI",           color: "#ef4444", unit: "index" },
  { id: "unemployment",   label: "Unemployment",  color: "#a78bfa", unit: "%" },
  { id: "gdp",            label: "GDP",           color: "#34d399", unit: "$B" },
  { id: "usd_index",      label: "USD Index",     color: "#60a5fa", unit: "" },
  { id: "twd_usd",        label: "TWD/USD",       color: "#fb923c", unit: "" },
] as const;

type IndicatorId = typeof INDICATORS[number]["id"];

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
          <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
          <XAxis
            dataKey="date"
            tick={{ fontSize: 10, fill: "#6b7280" }}
            interval="preserveStartEnd"
          />
          <YAxis tick={{ fontSize: 10, fill: "#6b7280" }} width={45} />
          <Tooltip
            contentStyle={{ background: "#111827", border: "1px solid #374151", fontSize: 11 }}
            formatter={(v: number) => [`${v.toFixed(3)} ${unit}`, indicator]}
          />
          {indicator === "10Y−2Y Spread" && <ReferenceLine y={0} stroke="#6b7280" strokeDasharray="4 4" />}
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
  const navigate = useNavigate();
  const [active, setActive] = useState<IndicatorId>("10y_minus_2y");

  const activeSpec = INDICATORS.find((i) => i.id === active)!;

  // Fetch latest value for each indicator (for KPI cards)
  const queries = INDICATORS.map((ind) =>
    // eslint-disable-next-line react-hooks/rules-of-hooks
    useQuery({
      queryKey: ["macro", ind.id],
      queryFn: () => fetchMacro(ind.id),
      staleTime: 3_600_000,
    })
  );

  const latestValues = INDICATORS.reduce((acc, ind, i) => {
    const data = queries[i].data;
    const last = data?.filter((d) => d.value != null).at(-1);
    acc[ind.id] = last?.value ?? null;
    return acc;
  }, {} as Record<string, number | null>);

  const activeData = queries[INDICATORS.findIndex((i) => i.id === active)].data ?? [];

  function analyseWithAI() {
    const context = INDICATORS.reduce((acc, ind) => {
      acc[ind.label] = latestValues[ind.id];
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
    <div className="p-6 space-y-6">
      {/* header */}
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-bold text-foreground">Macro Dashboard</h1>
          <p className="text-sm text-muted-foreground mt-1">
            FRED economic indicators · updated monthly
          </p>
        </div>
        <button
          onClick={analyseWithAI}
          className="px-4 py-2 bg-primary/10 border border-primary/30 text-primary rounded-lg text-sm hover:bg-primary/20 transition-colors"
        >
          🤖 Analyse with AI
        </button>
      </div>

      {/* KPI cards */}
      <div className="grid grid-cols-3 sm:grid-cols-5 lg:grid-cols-9 gap-2">
        {INDICATORS.map((ind) => (
          <MiniCard
            key={ind.id}
            label={ind.label}
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
          indicator={activeSpec.label}
          data={activeData}
          color={activeSpec.color}
          unit={activeSpec.unit}
        />
      </div>

      {/* yield curve spread interpretation */}
      {active === "10y_minus_2y" && (
        <div className="bg-card border border-border rounded-lg p-4 text-xs text-muted-foreground space-y-1">
          <span className="text-foreground font-medium">Yield Curve Note</span>
          <p>
            The 10Y–2Y spread is a leading recession indicator. Negative spread (inverted curve) has
            historically preceded recessions by 6–18 months. A zero-crossing back to positive ("disinversion")
            often coincides with recession onset.
          </p>
        </div>
      )}
    </div>
  );
}
