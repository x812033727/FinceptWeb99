import { useState, FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation } from "@tanstack/react-query";
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, ReferenceLine,
} from "recharts";
import { useTranslation } from "react-i18next";
import api from "@/lib/api";

type Tab = "dcf" | "var" | "backtest";

// ── Shared helpers ────────────────────────────────────────────────
const inputCls = "w-full px-3 py-1.5 rounded bg-input border border-border text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring";
const labelCls = "block text-xs text-muted-foreground mb-1";

function Card({ title, children, action }: { title: string; children: React.ReactNode; action?: React.ReactNode }) {
  return (
    <div className="bg-card border border-border rounded-lg p-5 space-y-4">
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
              🤖 {t("nav.ai")}
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
function BacktestPanel() {
  const { t } = useTranslation();
  const [symbols, setSymbols] = useState("AAPL");
  const [markets, setMarkets] = useState("US");
  const [strategy, setStrategy] = useState("sma_crossover");
  const [fast, setFast] = useState("20");
  const [slow, setSlow] = useState("50");
  const [rsiPeriod, setRsiPeriod] = useState("14");
  const [rsiOversold, setRsiOversold] = useState("30");
  const [rsiOverbought, setRsiOverbought] = useState("70");
  const [start, setStart] = useState("2020-01-01");
  const [end, setEnd] = useState("2024-12-31");
  const [capital, setCapital] = useState("100000");

  const run = useMutation({
    mutationFn: (body: unknown) => api.post("/analytics/backtest", body).then(r => r.data),
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    const params: Record<string, unknown> = {};
    if (strategy === "sma_crossover") { params.fast = +fast; params.slow = +slow; }
    if (strategy === "rsi_mean_reversion") { params.period = +rsiPeriod; params.oversold = +rsiOversold; params.overbought = +rsiOverbought; }
    run.mutate({
      symbols: symbols.split(",").map(s => s.trim()),
      markets: markets.split(",").map(s => s.trim()),
      strategy, params,
      start_date: start, end_date: end,
      initial_capital: +capital,
    });
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
              <select className={inputCls} value={strategy} onChange={e => setStrategy(e.target.value)}>
                <option value="sma_crossover">{t("analytics.backtest.strategy_sma")}</option>
                <option value="rsi_mean_reversion">{t("analytics.backtest.strategy_rsi")}</option>
              </select>
            </div>
            {strategy === "sma_crossover" && <>
              <div><label className={labelCls}>{t("analytics.backtest.fast")} SMA</label><input type="number" className={inputCls} value={fast} onChange={e => setFast(e.target.value)} /></div>
              <div><label className={labelCls}>{t("analytics.backtest.slow")} SMA</label><input type="number" className={inputCls} value={slow} onChange={e => setSlow(e.target.value)} /></div>
            </>}
            {strategy === "rsi_mean_reversion" && <>
              <div><label className={labelCls}>RSI {t("analytics.backtest.period")}</label><input type="number" className={inputCls} value={rsiPeriod} onChange={e => setRsiPeriod(e.target.value)} /></div>
              <div><label className={labelCls}>{t("analytics.backtest.oversold")}</label><input type="number" className={inputCls} value={rsiOversold} onChange={e => setRsiOversold(e.target.value)} /></div>
              <div><label className={labelCls}>{t("analytics.backtest.overbought")}</label><input type="number" className={inputCls} value={rsiOverbought} onChange={e => setRsiOverbought(e.target.value)} /></div>
            </>}
            <div><label className={labelCls}>{t("analytics.backtest.start_date")}</label><input type="date" className={inputCls} value={start} onChange={e => setStart(e.target.value)} /></div>
            <div><label className={labelCls}>{t("analytics.backtest.end_date")}</label><input type="date" className={inputCls} value={end} onChange={e => setEnd(e.target.value)} /></div>
            <div><label className={labelCls}>{t("analytics.backtest.initial_capital")}</label><input type="number" className={inputCls} value={capital} onChange={e => setCapital(e.target.value)} /></div>
          </div>
          <button type="submit" disabled={run.isPending} className="w-full py-2 bg-primary text-primary-foreground text-sm rounded hover:opacity-90 disabled:opacity-50">
            {run.isPending ? t("analytics.backtest.running") : t("analytics.backtest.run")}
          </button>
          {run.isError && <p className="text-negative text-xs">{(run.error as any)?.response?.data?.detail}</p>}
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
            </div>
          </Card>

          {r.equity_curve && r.equity_curve.length > 0 && (
            <Card title={t("analytics.backtest.equity_curve")}>
              <ResponsiveContainer width="100%" height={280}>
                <AreaChart data={r.equity_curve} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                  <defs>
                    <linearGradient id="eq" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%"  stopColor="#38bdf8" stopOpacity={0.3} />
                      <stop offset="95%" stopColor="#38bdf8" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
                  <XAxis dataKey="date" tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
                    tickFormatter={d => d.slice(0, 7)} interval={Math.floor(r.equity_curve.length / 8)} />
                  <YAxis tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
                    tickFormatter={v => `$${(v / 1000).toFixed(0)}k`} width={60} />
                  <Tooltip
                    contentStyle={{ background: "hsl(var(--card))", border: "1px solid hsl(var(--border))", borderRadius: 6 }}
                    labelStyle={{ color: "hsl(var(--muted-foreground))", fontSize: 11 }}
                    formatter={(v: number) => [`$${v.toLocaleString()}`, "Portfolio"]}
                  />
                  <ReferenceLine y={+capital} stroke="hsl(var(--muted-foreground))" strokeDasharray="4 2" />
                  <Area type="monotone" dataKey="value" stroke="#38bdf8" fill="url(#eq)" strokeWidth={1.5} dot={false} />
                </AreaChart>
              </ResponsiveContainer>
            </Card>
          )}
        </>
      )}
      {r?.status === "failed" && <p className="text-negative text-sm">{r.error}</p>}
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
      <h1 className="text-xl sm:text-2xl font-bold text-primary">{t("analytics.title")}</h1>

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
