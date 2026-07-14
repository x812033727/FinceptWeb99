import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, ReferenceLine,
} from "recharts";
import { ChartTooltip } from "@/components/ui/ChartTooltip";
import { useTranslation } from "react-i18next";
import api from "@/lib/api";
import BacktestHistoryPanel from "@/components/analytics/BacktestHistoryPanel";
import { Card, Metric, inputCls, labelCls } from "./_shared";

interface StrategyParamInfo {
  name: string;
  type: string;
  default: number;
  min: number | null;
  max: number | null;
  description: string;
}
interface StrategyInfo {
  name: string;
  label: string;
  description: string;
  params: StrategyParamInfo[];
}

// Shown until GET /analytics/backtest/strategies responds (or if it fails).
const FALLBACK_STRATEGIES: StrategyInfo[] = [
  {
    name: "sma_crossover", label: "SMA Crossover", description: "",
    params: [
      { name: "fast", type: "int", default: 20, min: 2, max: 200, description: "" },
      { name: "slow", type: "int", default: 50, min: 5, max: 400, description: "" },
    ],
  },
  {
    name: "rsi_mean_reversion", label: "RSI Mean Reversion", description: "",
    params: [
      { name: "period", type: "int", default: 14, min: 2, max: 100, description: "" },
      { name: "oversold", type: "float", default: 30, min: 1, max: 50, description: "" },
      { name: "overbought", type: "float", default: 70, min: 50, max: 99, description: "" },
    ],
  },
];

export function BacktestPanel() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [symbols, setSymbols] = useState("AAPL");
  const [markets, setMarkets] = useState("US");
  const [strategy, setStrategy] = useState("sma_crossover");
  const [paramValues, setParamValues] = useState<Record<string, string>>({});
  const [start, setStart] = useState("2020-01-01");
  const [end, setEnd] = useState("2024-12-31");
  const [capital, setCapital] = useState("100000");
  // Advanced (risk controls) — empty string = leave engine default (off).
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [stopLoss, setStopLoss] = useState("");
  const [takeProfit, setTakeProfit] = useState("");
  const [trailingStop, setTrailingStop] = useState("");
  const [positionSize, setPositionSize] = useState("");
  const [slippageBps, setSlippageBps] = useState("");
  const [commissionBps, setCommissionBps] = useState("");
  const [allowShort, setAllowShort] = useState(false);
  // C3 persistence — off by default.
  const [saveRun, setSaveRun] = useState(false);
  const [runName, setRunName] = useState("");

  const strategiesQuery = useQuery({
    queryKey: ["backtest-strategies"],
    queryFn: () => api.get("/analytics/backtest/strategies").then(r => r.data as StrategyInfo[]),
    staleTime: 5 * 60 * 1000,
  });
  const strategies = strategiesQuery.data?.length ? strategiesQuery.data : FALLBACK_STRATEGIES;
  const spec = strategies.find(s => s.name === strategy) ?? strategies[0];

  const run = useMutation({
    mutationFn: (body: unknown) => api.post("/analytics/backtest", body).then(r => r.data),
    onSuccess: (data) => {
      // A persisted run must show up in the 歷史回測 list right away.
      if (data?.run_id) qc.invalidateQueries({ queryKey: ["backtest-runs"] });
    },
  });

  function strategyLabel(s: StrategyInfo): string {
    return t(`analytics.backtest.strategy_${s.name}`, s.label);
  }
  function paramLabel(p: StrategyParamInfo): string {
    return t(`analytics.backtest.param_${p.name}`, p.name);
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    const params: Record<string, unknown> = {};
    for (const p of spec.params) {
      const raw = paramValues[p.name];
      params[p.name] = raw != null && raw !== "" ? +raw : p.default;
    }
    const body: Record<string, unknown> = {
      symbols: symbols.split(",").map(s => s.trim()),
      markets: markets.split(",").map(s => s.trim()),
      strategy: spec.name, params,
      start_date: start, end_date: end,
      initial_capital: +capital,
    };
    if (stopLoss)      body.stop_loss_pct = +stopLoss / 100;
    if (takeProfit)    body.take_profit_pct = +takeProfit / 100;
    if (trailingStop)  body.trailing_stop_pct = +trailingStop / 100;
    if (positionSize)  body.position_size_pct = +positionSize / 100;
    if (slippageBps)   body.slippage_bps = +slippageBps;
    if (commissionBps) body.commission_bps = +commissionBps;
    if (allowShort)    body.allow_short = true;
    if (saveRun) {
      body.save = true;
      if (runName.trim()) body.name = runName.trim().slice(0, 120);
    }
    run.mutate(body);
  }

  const r = run.data;

  return (
    <div className="space-y-6">
      <Card title={`${t("analytics.tabs.backtest")} · ${t("analytics.backtest.inputs")}`}>
        <form onSubmit={submit} className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <div><label className={labelCls}>{t("analytics.var.symbols")}</label><input className={inputCls} value={symbols} onChange={e => setSymbols(e.target.value)} /></div>
            <div><label className={labelCls}>{t("alerts.market")}</label><input className={inputCls} value={markets} onChange={e => setMarkets(e.target.value)} /></div>
            <div><label className={labelCls}>{t("analytics.backtest.strategy")}</label>
              <select className={inputCls} value={strategy} onChange={e => { setStrategy(e.target.value); setParamValues({}); }}>
                {strategies.map(s => (
                  <option key={s.name} value={s.name}>{strategyLabel(s)}</option>
                ))}
              </select>
            </div>
            {spec.params.map(p => (
              <div key={`${spec.name}.${p.name}`}>
                <label className={labelCls} title={p.description}>{paramLabel(p)}</label>
                <input
                  type="number"
                  step={p.type === "int" ? 1 : "any"}
                  min={p.min ?? undefined}
                  max={p.max ?? undefined}
                  className={inputCls}
                  value={paramValues[p.name] ?? String(p.default)}
                  onChange={e => setParamValues(v => ({ ...v, [p.name]: e.target.value }))}
                />
              </div>
            ))}
            <div><label className={labelCls}>{t("analytics.backtest.start_date")}</label><input type="date" className={inputCls} value={start} onChange={e => setStart(e.target.value)} /></div>
            <div><label className={labelCls}>{t("analytics.backtest.end_date")}</label><input type="date" className={inputCls} value={end} onChange={e => setEnd(e.target.value)} /></div>
            <div><label className={labelCls}>{t("analytics.backtest.initial_capital")}</label><input type="number" className={inputCls} value={capital} onChange={e => setCapital(e.target.value)} /></div>
          </div>

          <button
            type="button"
            onClick={() => setShowAdvanced(v => !v)}
            className="text-xs text-primary hover:underline"
          >
            {showAdvanced ? "▾" : "▸"} {t("analytics.backtest.advanced")}
          </button>
          {showAdvanced && (
            <div className="grid grid-cols-2 gap-3 border border-border/60 rounded p-3">
              <div><label className={labelCls}>{t("analytics.backtest.stop_loss")}</label><input type="number" step="any" min="0" className={inputCls} value={stopLoss} onChange={e => setStopLoss(e.target.value)} placeholder="—" /></div>
              <div><label className={labelCls}>{t("analytics.backtest.take_profit")}</label><input type="number" step="any" min="0" className={inputCls} value={takeProfit} onChange={e => setTakeProfit(e.target.value)} placeholder="—" /></div>
              <div><label className={labelCls}>{t("analytics.backtest.trailing_stop")}</label><input type="number" step="any" min="0" className={inputCls} value={trailingStop} onChange={e => setTrailingStop(e.target.value)} placeholder="—" /></div>
              <div><label className={labelCls}>{t("analytics.backtest.position_size")}</label><input type="number" step="any" min="0" max="100" className={inputCls} value={positionSize} onChange={e => setPositionSize(e.target.value)} placeholder="—" /></div>
              <div><label className={labelCls}>{t("analytics.backtest.slippage_bps")}</label><input type="number" step="any" min="0" className={inputCls} value={slippageBps} onChange={e => setSlippageBps(e.target.value)} placeholder="0" /></div>
              <div><label className={labelCls}>{t("analytics.backtest.commission_bps")}</label><input type="number" step="any" min="0" className={inputCls} value={commissionBps} onChange={e => setCommissionBps(e.target.value)} placeholder="0" /></div>
              <label className="col-span-2 flex items-center gap-2 text-xs text-foreground">
                <input type="checkbox" checked={allowShort} onChange={e => setAllowShort(e.target.checked)} />
                {t("analytics.backtest.allow_short")}
              </label>
            </div>
          )}

          <div className="flex items-center gap-3 flex-wrap">
            <label className="flex items-center gap-2 text-xs text-foreground whitespace-nowrap">
              <input type="checkbox" checked={saveRun} onChange={e => setSaveRun(e.target.checked)} />
              {t("analytics.backtest.save_run")}
            </label>
            {saveRun && (
              <input
                className={`${inputCls} flex-1 min-w-40`}
                maxLength={120}
                value={runName}
                onChange={e => setRunName(e.target.value)}
                placeholder={t("analytics.backtest.run_name_placeholder")}
                aria-label={t("analytics.backtest.run_name")}
              />
            )}
          </div>

          <button type="submit" disabled={run.isPending} className="w-full py-2 bg-primary text-primary-foreground text-sm rounded hover:opacity-90 disabled:opacity-50">
            {run.isPending ? t("analytics.backtest.running") : t("analytics.backtest.run")}
          </button>
          {run.isError && <p className="text-negative text-xs">{(run.error as any)?.response?.data?.detail}</p>}
          {run.data?.run_id && <p className="text-positive text-xs">{t("analytics.backtest.saved")}</p>}
        </form>
      </Card>

      {r?.status === "completed" && r.metrics && (
        <>
          <Card title={t("analytics.backtest.performance_metrics")}>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              <Metric label={t("analytics.backtest.total_return")} value={`${r.metrics.total_return_pct.toFixed(2)}%`} color={r.metrics.total_return_pct >= 0 ? "text-positive" : "text-negative"} />
              <Metric label={t("analytics.backtest.annualized_return")}  value={`${r.metrics.annualised_return_pct.toFixed(2)}%`} />
              <Metric label={t("analytics.backtest.sharpe")}       value={r.metrics.sharpe_ratio.toFixed(2)} />
              <Metric label={t("analytics.backtest.max_drawdown")} value={`${r.metrics.max_drawdown_pct.toFixed(2)}%`} color="text-negative" />
              <Metric label={t("analytics.backtest.win_rate")}     value={`${(r.metrics.win_rate * 100).toFixed(1)}%`} />
              <Metric label={t("portfolio.optimizer.expected_vol")}   value={`${r.metrics.annualised_volatility.toFixed(2)}%`} />
              <Metric label={t("analytics.backtest.trades")} value={`${r.metrics.total_trades}`} />
              <Metric label={t("portfolio.summary.total_value")}  value={`$${r.metrics.final_value.toLocaleString()}`} color="text-primary" />
              {r.metrics.total_commission != null && (
                <Metric label={t("analytics.backtest.total_commission")} value={`$${r.metrics.total_commission.toLocaleString()}`} />
              )}
              {r.metrics.total_slippage != null && (
                <Metric label={t("analytics.backtest.total_slippage")} value={`$${r.metrics.total_slippage.toLocaleString()}`} />
              )}
            </div>
          </Card>

          {r.equity_curve && r.equity_curve.length > 0 && (
            <Card title={t("analytics.backtest.equity_curve")}>
              <ResponsiveContainer width="100%" height={280}>
                <AreaChart data={r.equity_curve} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                  <defs>
                    <linearGradient id="eq" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%"  stopColor="hsl(var(--chart-1))" stopOpacity={0.3} />
                      <stop offset="95%" stopColor="hsl(var(--chart-1))" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
                  <XAxis dataKey="date" tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
                    tickFormatter={d => d.slice(0, 7)} interval={Math.floor(r.equity_curve.length / 8)} />
                  <YAxis tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
                    tickFormatter={v => `$${(v / 1000).toFixed(0)}k`} width={60} />
                  <Tooltip content={<ChartTooltip valueFormatter={(v) => `$${v.toLocaleString()}`} />} />
                  <ReferenceLine y={+capital} stroke="hsl(var(--muted-foreground))" strokeDasharray="4 2" />
                  <Area type="monotone" dataKey="value" stroke="hsl(var(--chart-1))" fill="url(#eq)" strokeWidth={1.5} dot={false} />
                </AreaChart>
              </ResponsiveContainer>
            </Card>
          )}
        </>
      )}
      {r?.status === "failed" && <p className="text-negative text-sm">{r.error}</p>}

      <BacktestHistoryPanel />
    </div>
  );
}

// ── Main page ─────────────────────────────────────────────────────
