import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { AlertTriangle, FlaskConical } from "lucide-react";

import api, { errorDetail } from "@/lib/api";

type StressResult = {
  currency: string; portfolio_value: number; gap_symbol: string | null; disclaimer: string;
  scenarios: Array<{
    scenario: string; label: string; pnl: number; pnl_pct: number; post_scenario_value: number;
    holdings: Array<{ symbol: string; market: string; shock_pct: number; pnl: number; risk_contribution_pct: number }>;
    rebalance_suggestions: Array<{ symbol: string; current_stressed_weight_pct: number; target_weight_pct: number; indicative_amount: number; reason: string }>;
  }>;
};

const OPTIONS = [
  ["taiex_drawdown", "TAIEX drawdown"],
  ["semiconductor_downturn", "Semiconductor downturn"],
  ["twd_depreciation", "TWD depreciation"],
  ["rates_up_100bp", "Rates +100bp"],
  ["single_stock_gap", "Single-stock gap"],
] as const;
type ScenarioKey = (typeof OPTIONS)[number][0];

export default function StressTestPanel({ portfolioId }: { portfolioId: string }) {
  const [selected, setSelected] = useState<ScenarioKey[]>(OPTIONS.map(([key]) => key));
  const [gapSymbol, setGapSymbol] = useState("");
  const [gapPct, setGapPct] = useState(-20);
  const stress = useMutation({
    mutationFn: () => api.post<StressResult>(`/portfolio/${portfolioId}/stress-test`, {
      scenarios: selected, gap_symbol: gapSymbol.trim().toUpperCase() || null, gap_pct: gapPct,
    }).then((response) => response.data),
  });

  function toggle(key: ScenarioKey) {
    setSelected((current) => current.includes(key) ? current.filter((item) => item !== key) : [...current, key]);
  }

  return (
    <div className="space-y-5">
      <div className="rounded-lg border border-border bg-card p-5 shadow-highlight">
        <div className="flex items-start justify-between gap-4">
          <div><h2 className="flex items-center gap-2 font-medium"><FlaskConical className="h-4 w-4 text-primary" /> Scenario stress test</h2><p className="mt-1 text-xs text-muted-foreground">Transparent deterministic shocks. No orders are generated or executed.</p></div>
          <button disabled={!selected.length || stress.isPending} onClick={() => stress.mutate()} className="rounded bg-primary px-3 py-2 text-sm text-primary-foreground disabled:opacity-40">{stress.isPending ? "Running…" : "Run scenarios"}</button>
        </div>
        <div className="mt-4 flex flex-wrap gap-2">{OPTIONS.map(([key, label]) => <label key={key} className={`cursor-pointer rounded-full border px-3 py-1.5 text-xs ${selected.includes(key) ? "border-primary bg-primary/10 text-primary" : "border-border text-muted-foreground"}`}><input type="checkbox" checked={selected.includes(key)} onChange={() => toggle(key)} className="sr-only" />{label}</label>)}</div>
        {selected.includes("single_stock_gap") && <div className="mt-4 grid gap-3 sm:grid-cols-2"><label className="text-xs text-muted-foreground">Gap symbol (blank = largest holding)<input value={gapSymbol} onChange={(e) => setGapSymbol(e.target.value)} placeholder="2330" className="mt-1 w-full rounded border border-border bg-background px-3 py-2 text-sm text-foreground" /></label><label className="text-xs text-muted-foreground">Gap shock (%)<input type="number" min={-100} max={100} value={gapPct} onChange={(e) => setGapPct(Number(e.target.value))} className="mt-1 w-full rounded border border-border bg-background px-3 py-2 text-sm text-foreground" /></label></div>}
        {stress.isError && <p className="mt-3 text-sm text-negative">{errorDetail(stress.error)}</p>}
      </div>

      {stress.data && <div className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">{stress.data.scenarios.map((scenario) => <div key={scenario.scenario} className="rounded-lg border border-border bg-card p-4"><p className="text-xs text-muted-foreground">{scenario.label}</p><p className={`mt-1 text-xl font-semibold ${scenario.pnl >= 0 ? "text-positive" : "text-negative"}`}>{scenario.pnl >= 0 ? "+" : ""}{scenario.pnl.toLocaleString()} {stress.data!.currency}</p><p className={`text-sm ${scenario.pnl_pct >= 0 ? "text-positive" : "text-negative"}`}>{scenario.pnl_pct >= 0 ? "+" : ""}{scenario.pnl_pct.toFixed(2)}%</p></div>)}</div>
        {stress.data.scenarios.map((scenario) => <div key={`${scenario.scenario}-detail`} className="overflow-hidden rounded-lg border border-border bg-card"><div className="flex items-center justify-between border-b border-border px-4 py-3"><h3 className="font-medium">{scenario.label}</h3><span className="text-xs text-muted-foreground">Post value {scenario.post_scenario_value.toLocaleString()} {stress.data!.currency}</span></div><div className="overflow-x-auto"><table className="w-full text-sm"><thead className="bg-secondary/20 text-left text-xs text-muted-foreground"><tr><th className="p-3">Holding</th><th className="p-3">Shock</th><th className="p-3">P&amp;L</th><th className="p-3">Risk contribution</th></tr></thead><tbody>{scenario.holdings.map((row) => <tr key={`${row.market}-${row.symbol}`} className="border-t border-border"><td className="p-3 font-medium">{row.market}:{row.symbol}</td><td className="p-3">{row.shock_pct.toFixed(2)}%</td><td className={`p-3 ${row.pnl >= 0 ? "text-positive" : "text-negative"}`}>{row.pnl.toLocaleString()}</td><td className="p-3">{row.risk_contribution_pct.toFixed(2)}%</td></tr>)}</tbody></table></div>{scenario.rebalance_suggestions.length > 0 && <div className="border-t border-warning/30 bg-warning/5 p-4"><p className="mb-2 flex items-center gap-2 text-sm font-medium"><AlertTriangle className="h-4 w-4 text-warning" /> Concentration review</p>{scenario.rebalance_suggestions.map((item) => <p key={item.symbol} className="text-xs text-muted-foreground">{item.symbol}: stressed weight {item.current_stressed_weight_pct}% → review toward {item.target_weight_pct}% (indicative {item.indicative_amount.toLocaleString()} {stress.data!.currency}). {item.reason}</p>)}</div>}</div>)}
        <p className="text-xs text-muted-foreground">{stress.data.disclaimer}</p>
      </div>}
    </div>
  );
}
