import { useState, FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, ReferenceLine,
} from "recharts";
import { ChartTooltip } from "@/components/ui/ChartTooltip";
import { useTranslation } from "react-i18next";
import { Bot } from "lucide-react";
import api from "@/lib/api";
import BacktestHistoryPanel from "@/components/analytics/BacktestHistoryPanel";

type Tab = "dcf" | "var" | "backtest";

// ── Shared helpers ────────────────────────────────────────────────
const inputCls = "w-full px-3 py-1.5 rounded bg-input border border-border text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring";
const labelCls = "block text-xs text-muted-foreground mb-1";

function Card({ title, children, action }: { title: string; children: React.ReactNode; action?: React.ReactNode }) {
  return (
    <div className="bg-card shadow-highlight border border-border rounded-lg p-5 space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-foreground font-medium">{title}</h2>
        {action}
      </div>
      {children}
    </div>
  );
}

function Metric({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div className="bg-secondary/30 rounded p-3">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className={`text-sm font-semibold mt-0.5 ${color ?? "text-foreground"}`}>{value}</p>
    </div>
  );
}

// ── DCF Panel ─────────────────────────────────────────────────────
function DCFPanel({ onAnalyseWithAI }: { onAnalyseWithAI: (ctx: unknown) => void }) {
  const { t } = useTranslation();
  const [symbol, setSymbol] = useState("AAPL");
  const [market, setMarket] = useState("US");
  const [wacc, setWacc] = useState("0.09");
  const [g1, setG1] = useState("0.10");
  const [g2, setG2] = useState("0.05");
  const [tg, setTg] = useState("0.03");
  const [shares, setShares] = useState("");
  const [fcf, setFcf] = useState("");
  const [price, setPrice] = useState("");

  const run = useMutation({
    mutationFn: (body: unknown) => api.post("/analytics/dcf", body).then(r => r.data),
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    const overrides: Record<string, unknown> = { wacc: +wacc, growth_rate_1: +g1, growth_rate_2: +g2, terminal_growth: +tg };
    if (shares) overrides.shares = +shares;
    if (fcf)    overrides.fcf_history = [+fcf];
    if (price)  overrides.current_price = +price;
    run.mutate({ symbol: symbol.toUpperCase(), market, overrides });
  }

  const r = run.data;

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
      <Card title={`${t("analytics.tabs.dcf")} · ${t("analytics.dcf.inputs")}`}>
        <form onSubmit={submit} className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <div><label className={labelCls}>{t("analytics.dcf.symbol")}</label><input className={inputCls} value={symbol} onChange={e => setSymbol(e.target.value)} /></div>
            <div><label className={labelCls}>{t("analytics.dcf.market")}</label>
              <select className={inputCls} value={market} onChange={e => setMarket(e.target.value)}>
                <option value="US">US</option><option value="TW">TW</option>
              </select>
            </div>
            <div><label className={labelCls}>{t("analytics.dcf.wacc")}</label><input type="number" step="0.001" className={inputCls} value={wacc} onChange={e => setWacc(e.target.value)} /></div>
            <div><label className={labelCls}>{t("analytics.dcf.growth_rate")} Y1-5</label><input type="number" step="0.01" className={inputCls} value={g1} onChange={e => setG1(e.target.value)} /></div>
            <div><label className={labelCls}>{t("analytics.dcf.growth_rate")} Y6-10</label><input type="number" step="0.01" className={inputCls} value={g2} onChange={e => setG2(e.target.value)} /></div>
            <div><label className={labelCls}>{t("analytics.dcf.terminal_growth")}</label><input type="number" step="0.001" className={inputCls} value={tg} onChange={e => setTg(e.target.value)} /></div>
            <div><label className={labelCls}>{t("analytics.dcf.fcf_history")}</label><input type="number" className={inputCls} value={fcf} onChange={e => setFcf(e.target.value)} placeholder="100000000000" /></div>
            <div><label className={labelCls}>{t("analytics.dcf.shares")}</label><input type="number" className={inputCls} value={shares} onChange={e => setShares(e.target.value)} /></div>
            <div className="col-span-2"><label className={labelCls}>{t("analytics.dcf.current_price")}</label><input type="number" step="0.01" className={inputCls} value={price} onChange={e => setPrice(e.target.value)} /></div>
          </div>
          <button type="submit" disabled={run.isPending} className="w-full py-2 bg-primary text-primary-foreground text-sm rounded hover:opacity-90 disabled:opacity-50">
            {run.isPending ? t("analytics.dcf.running") : t("analytics.dcf.run")}
          </button>
          {run.isError && <p className="text-negative text-xs">{(run.error as any)?.response?.data?.detail}</p>}
        </form>
      </Card>

      {r && (
        <Card title={`${t("analytics.tabs.dcf")} · ${t("analytics.dcf.results")}`}
          action={
            <button
              onClick={() => onAnalyseWithAI(r)}
              className="text-xs text-primary hover:underline"
            >
              <span className="inline-flex items-center gap-1"><Bot className="h-3.5 w-3.5" aria-hidden="true" />{t("nav.ai")}</span>
            </button>
          }
        >
          <div className="grid grid-cols-2 gap-3">
            <Metric label={t("analytics.dcf.fair_value")} value={`$${r.intrinsic_value.toFixed(2)}`} color="text-primary" />
            <Metric label={t("analytics.dcf.margin_of_safety")} value={r.margin_of_safety != null ? `${r.margin_of_safety.toFixed(1)}%` : "—"}
              color={r.margin_of_safety > 0 ? "text-positive" : "text-negative"} />
            <Metric label="PV of FCFs"      value={`$${(r.pv_fcf / 1e9).toFixed(1)}B`} />
            <Metric label="PV Terminal"      value={`$${(r.pv_terminal / 1e9).toFixed(1)}B`} />
          </div>

          <div>
            <p className="text-xs text-muted-foreground mb-2">{t("analytics.dcf.scenarios")}</p>
            <div className="grid grid-cols-3 gap-2">
              {Object.entries(r.scenarios).map(([name, val]: [string, any]) => (
                <div key={name} className="text-center bg-secondary/30 rounded p-2">
                  <p className="text-xs text-muted-foreground capitalize">
                    {name === "bull" ? t("analytics.dcf.bull") : name === "bear" ? t("analytics.dcf.bear") : t("analytics.dcf.base")}
                  </p>
                  <p className="text-sm font-semibold text-foreground">${val.toFixed(2)}</p>
                </div>
              ))}
            </div>
          </div>

          <div>
            <p className="text-xs text-muted-foreground mb-2">{t("analytics.dcf.sensitivity_grid")}</p>
            <div className="overflow-x-auto">
              <table className="text-xs w-full">
                <thead>
                  <tr>
                    <th className="text-muted-foreground text-left pb-1">WACC \ TG</th>
                    {r.sensitivity.terminal_growth.map((tg: number) => (
                      <th key={tg} className="text-muted-foreground text-right pb-1">{(tg * 100).toFixed(1)}%</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {r.sensitivity.values.map((row: number[], i: number) => (
                    <tr key={i} className="border-t border-border/30">
                      <td className="text-muted-foreground py-1">{(r.sensitivity.wacc[i] * 100).toFixed(1)}%</td>
                      {row.map((v, j) => (
                        <td key={j} className={`text-right py-1 ${i === 2 && j === 1 ? "text-primary font-semibold" : "text-foreground"}`}>
                          ${v.toFixed(0)}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </Card>
      )}
    </div>
  );
}

// ── VaR Panel ─────────────────────────────────────────────────────
function VaRPanel() {
  const { t } = useTranslation();
  const [symbols, setSymbols] = useState("AAPL,MSFT,TSLA");
  const [markets, setMarkets] = useState("US,US,US");
  const [weights, setWeights] = useState("0.4,0.3,0.3");
  const [value, setValue] = useState("100000");
  const [method, setMethod] = useState("all");
  const [conf, setConf] = useState("0.95");
  const [horizon, setHorizon] = useState("1");

  const run = useMutation({
    mutationFn: (body: unknown) => api.post("/analytics/var", body).then(r => r.data),
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    run.mutate({
      symbols: symbols.split(",").map(s => s.trim()),
      markets: markets.split(",").map(s => s.trim()),
      weights: weights.split(",").map(Number),
      portfolio_value: +value,
      method, confidence: +conf, horizon_days: +horizon,
    });
  }

  const r = run.data;
  const methods = ["historical", "parametric", "monte_carlo"] as const;

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
      <Card title={`${t("analytics.tabs.var")} · ${t("analytics.var.inputs")}`}>
        <form onSubmit={submit} className="space-y-3">
          <div><label className={labelCls}>{t("analytics.var.symbols")}</label><input className={inputCls} value={symbols} onChange={e => setSymbols(e.target.value)} /></div>
          <div><label className={labelCls}>{t("alerts.market")} (US,TW)</label><input className={inputCls} value={markets} onChange={e => setMarkets(e.target.value)} /></div>
          <div><label className={labelCls}>{t("analytics.var.weights")}</label><input className={inputCls} value={weights} onChange={e => setWeights(e.target.value)} /></div>
          <div className="grid grid-cols-2 gap-3">
            <div><label className={labelCls}>{t("analytics.var.portfolio_value")}</label><input type="number" className={inputCls} value={value} onChange={e => setValue(e.target.value)} /></div>
            <div><label className={labelCls}>{t("analytics.var.method")}</label>
              <select className={inputCls} value={method} onChange={e => setMethod(e.target.value)}>
                <option value="all">{t("analytics.var.method_all")}</option>
                <option value="historical">{t("analytics.var.method_historical")}</option>
                <option value="parametric">{t("analytics.var.method_parametric")}</option>
                <option value="monte_carlo">{t("analytics.var.method_monte_carlo")}</option>
              </select>
            </div>
            <div><label className={labelCls}>{t("analytics.var.confidence")}</label><input type="number" step="0.01" min="0.9" max="0.99" className={inputCls} value={conf} onChange={e => setConf(e.target.value)} /></div>
            <div><label className={labelCls}>{t("analytics.var.horizon_days")}</label><input type="number" min="1" max="30" className={inputCls} value={horizon} onChange={e => setHorizon(e.target.value)} /></div>
          </div>
          <button type="submit" disabled={run.isPending} className="w-full py-2 bg-primary text-primary-foreground text-sm rounded hover:opacity-90 disabled:opacity-50">
            {run.isPending ? t("analytics.var.running") : t("analytics.var.run")}
          </button>
          {run.isError && <p className="text-negative text-xs">{(run.error as any)?.response?.data?.detail}</p>}
        </form>
      </Card>

      {r && (
        <Card title={`${t("analytics.tabs.var")} · ${t("analytics.var.results")}`}>
          <div className="space-y-4">
            {methods.filter(m => r[m]).map(m => (
              <div key={m}>
                <p className="text-xs font-medium text-muted-foreground capitalize mb-2">{m.replace("_", " ")}</p>
                <div className="grid grid-cols-2 gap-2">
                  <Metric label={t("analytics.var.var_value")} value={`$${r[m].var_amount?.toLocaleString()}`} color="text-negative" />
                  <Metric label={`${t("analytics.var.var_value")} %`} value={`${(r[m].var_pct * 100).toFixed(2)}%`} color="text-negative" />
                  {r[m].cvar_pct && <Metric label={t("analytics.var.cvar_value")} value={`${(r[m].cvar_pct * 100).toFixed(2)}%`} />}
                  {r[m].annualised_return && <Metric label={t("analytics.backtest.annualized_return")} value={`${(r[m].annualised_return * 100).toFixed(1)}%`} />}
                </div>
              </div>
            ))}
            {r.portfolio_metrics && (
              <div>
                <p className="text-xs font-medium text-muted-foreground mb-2">Portfolio Metrics</p>
                <div className="grid grid-cols-2 gap-2">
                  {[
                    ["Sharpe", r.portfolio_metrics.sharpe_ratio?.toFixed(2)],
                    ["Sortino", r.portfolio_metrics.sortino_ratio?.toFixed(2)],
                    ["Max DD", `${(r.portfolio_metrics.max_drawdown * 100).toFixed(1)}%`],
                    ["Beta", r.portfolio_metrics.beta?.toFixed(2) ?? "—"],
                  ].map(([l, v]) => <Metric key={l} label={l} value={v ?? "—"} />)}
                </div>
              </div>
            )}
          </div>
        </Card>
      )}
    </div>
  );
}

// ── Backtest Panel ────────────────────────────────────────────────
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

function BacktestPanel() {
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
export default function AnalyticsPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [tab, setTab] = useState<Tab>("dcf");

  function analyseWithAI(agentId: string, context: unknown, message: string) {
    navigate("/ai", { state: { agentId, context, initialMessage: message } });
  }

  const tabs: { id: Tab; label: string }[] = [
    { id: "dcf",      label: t("analytics.tabs.dcf") },
    { id: "var",      label: t("analytics.tabs.var") },
    { id: "backtest", label: t("analytics.tabs.backtest") },
  ];

  return (
    <div className="min-h-screen bg-background p-4 sm:p-6 space-y-5 sm:space-y-6">
      <h1 className="text-title font-bold text-primary">{t("analytics.title")}</h1>

      <div className="flex gap-1 bg-secondary/30 p-1 rounded-lg w-fit max-w-full overflow-x-auto">
        {tabs.map(tabItem => (
          <button
            key={tabItem.id}
            onClick={() => setTab(tabItem.id)}
            className={`shrink-0 px-3 sm:px-4 py-1.5 text-sm rounded-md transition-colors whitespace-nowrap ${
              tab === tabItem.id
                ? "bg-card text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground"
            }`}
          >
            {tabItem.label}
          </button>
        ))}
      </div>

      {tab === "dcf" && (
        <DCFPanel
          onAnalyseWithAI={(ctx) =>
            analyseWithAI(
              "equity_analyst",
              { dcf_result: ctx },
              "Interpret this DCF result. Is the stock undervalued or overvalued? What are the key assumptions driving the valuation?"
            )
          }
        />
      )}
      {tab === "var"      && <VaRPanel />}
      {tab === "backtest" && <BacktestPanel />}
    </div>
  );
}
