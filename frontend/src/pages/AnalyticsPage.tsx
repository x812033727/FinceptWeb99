import { useState, FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation } from "@tanstack/react-query";
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, ReferenceLine,
} from "recharts";
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
      <Card title="DCF Inputs">
        <form onSubmit={submit} className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <div><label className={labelCls}>Symbol</label><input className={inputCls} value={symbol} onChange={e => setSymbol(e.target.value)} /></div>
            <div><label className={labelCls}>Market</label>
              <select className={inputCls} value={market} onChange={e => setMarket(e.target.value)}>
                <option value="US">US</option><option value="TW">TW</option>
              </select>
            </div>
            <div><label className={labelCls}>WACC</label><input type="number" step="0.001" className={inputCls} value={wacc} onChange={e => setWacc(e.target.value)} /></div>
            <div><label className={labelCls}>Growth Y1-5</label><input type="number" step="0.01" className={inputCls} value={g1} onChange={e => setG1(e.target.value)} /></div>
            <div><label className={labelCls}>Growth Y6-10</label><input type="number" step="0.01" className={inputCls} value={g2} onChange={e => setG2(e.target.value)} /></div>
            <div><label className={labelCls}>Terminal Growth</label><input type="number" step="0.001" className={inputCls} value={tg} onChange={e => setTg(e.target.value)} /></div>
            <div><label className={labelCls}>Base FCF (optional)</label><input type="number" className={inputCls} value={fcf} onChange={e => setFcf(e.target.value)} placeholder="e.g. 100000000000" /></div>
            <div><label className={labelCls}>Shares (optional)</label><input type="number" className={inputCls} value={shares} onChange={e => setShares(e.target.value)} /></div>
            <div className="col-span-2"><label className={labelCls}>Current Price (for MoS %)</label><input type="number" step="0.01" className={inputCls} value={price} onChange={e => setPrice(e.target.value)} /></div>
          </div>
          <button type="submit" disabled={run.isPending} className="w-full py-2 bg-primary text-primary-foreground text-sm rounded hover:opacity-90 disabled:opacity-50">
            {run.isPending ? "Computing…" : "Run DCF"}
          </button>
          {run.isError && <p className="text-negative text-xs">{(run.error as any)?.response?.data?.detail}</p>}
        </form>
      </Card>

      {r && (
        <Card title="DCF Results"
          action={
            <button
              onClick={() => onAnalyseWithAI(r)}
              className="text-xs text-primary hover:underline"
            >
              🤖 Ask AI
            </button>
          }
        >
          <div className="grid grid-cols-2 gap-3">
            <Metric label="Intrinsic Value" value={`$${r.intrinsic_value.toFixed(2)}`} color="text-primary" />
            <Metric label="Margin of Safety" value={r.margin_of_safety != null ? `${r.margin_of_safety.toFixed(1)}%` : "—"}
              color={r.margin_of_safety > 0 ? "text-positive" : "text-negative"} />
            <Metric label="PV of FCFs"      value={`$${(r.pv_fcf / 1e9).toFixed(1)}B`} />
            <Metric label="PV Terminal"      value={`$${(r.pv_terminal / 1e9).toFixed(1)}B`} />
          </div>

          <div>
            <p className="text-xs text-muted-foreground mb-2">Scenarios</p>
            <div className="grid grid-cols-3 gap-2">
              {Object.entries(r.scenarios).map(([name, val]: [string, any]) => (
                <div key={name} className="text-center bg-secondary/30 rounded p-2">
                  <p className="text-xs text-muted-foreground capitalize">{name}</p>
                  <p className="text-sm font-semibold text-foreground">${val.toFixed(2)}</p>
                </div>
              ))}
            </div>
          </div>

          <div>
            <p className="text-xs text-muted-foreground mb-2">Sensitivity (WACC × Terminal Growth)</p>
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
      <Card title="VaR Inputs">
        <form onSubmit={submit} className="space-y-3">
          <div><label className={labelCls}>Symbols (comma-separated)</label><input className={inputCls} value={symbols} onChange={e => setSymbols(e.target.value)} /></div>
          <div><label className={labelCls}>Markets (US or TW, comma-separated)</label><input className={inputCls} value={markets} onChange={e => setMarkets(e.target.value)} /></div>
          <div><label className={labelCls}>Weights (must match symbols, sum needn't be 1)</label><input className={inputCls} value={weights} onChange={e => setWeights(e.target.value)} /></div>
          <div className="grid grid-cols-2 gap-3">
            <div><label className={labelCls}>Portfolio Value</label><input type="number" className={inputCls} value={value} onChange={e => setValue(e.target.value)} /></div>
            <div><label className={labelCls}>Method</label>
              <select className={inputCls} value={method} onChange={e => setMethod(e.target.value)}>
                <option value="all">All methods</option>
                <option value="historical">Historical</option>
                <option value="parametric">Parametric</option>
                <option value="monte_carlo">Monte Carlo</option>
              </select>
            </div>
            <div><label className={labelCls}>Confidence</label><input type="number" step="0.01" min="0.9" max="0.99" className={inputCls} value={conf} onChange={e => setConf(e.target.value)} /></div>
            <div><label className={labelCls}>Horizon (days)</label><input type="number" min="1" max="30" className={inputCls} value={horizon} onChange={e => setHorizon(e.target.value)} /></div>
          </div>
          <button type="submit" disabled={run.isPending} className="w-full py-2 bg-primary text-primary-foreground text-sm rounded hover:opacity-90 disabled:opacity-50">
            {run.isPending ? "Computing…" : "Compute VaR"}
          </button>
          {run.isError && <p className="text-negative text-xs">{(run.error as any)?.response?.data?.detail}</p>}
        </form>
      </Card>

      {r && (
        <Card title="VaR Results">
          <div className="space-y-4">
            {methods.filter(m => r[m]).map(m => (
              <div key={m}>
                <p className="text-xs font-medium text-muted-foreground capitalize mb-2">{m.replace("_", " ")}</p>
                <div className="grid grid-cols-2 gap-2">
                  <Metric label="VaR Amount" value={`$${r[m].var_amount?.toLocaleString()}`} color="text-negative" />
                  <Metric label="VaR %" value={`${(r[m].var_pct * 100).toFixed(2)}%`} color="text-negative" />
                  {r[m].cvar_pct && <Metric label="CVaR %" value={`${(r[m].cvar_pct * 100).toFixed(2)}%`} />}
                  {r[m].annualised_return && <Metric label="Ann. Return" value={`${(r[m].annualised_return * 100).toFixed(1)}%`} />}
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
  const [symbols, setSymbols] = useState("AAPL");
  const [markets, setMarkets] = useState("US");
  const [strategy, setStrategy] = useState("sma_crossover");
  const [fast, setFast] = useState("20");
  const [slow, setSlow] = useState("50");
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
      <Card title="Backtest Inputs">
        <form onSubmit={submit} className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <div><label className={labelCls}>Symbols</label><input className={inputCls} value={symbols} onChange={e => setSymbols(e.target.value)} /></div>
            <div><label className={labelCls}>Markets</label><input className={inputCls} value={markets} onChange={e => setMarkets(e.target.value)} /></div>
            <div><label className={labelCls}>Strategy</label>
              <select className={inputCls} value={strategy} onChange={e => setStrategy(e.target.value)}>
                <option value="sma_crossover">SMA Crossover</option>
                <option value="rsi_mean_reversion">RSI Mean Reversion</option>
              </select>
            </div>
            {strategy === "sma_crossover" && <>
              <div><label className={labelCls}>Fast SMA</label><input type="number" className={inputCls} value={fast} onChange={e => setFast(e.target.value)} /></div>
              <div><label className={labelCls}>Slow SMA</label><input type="number" className={inputCls} value={slow} onChange={e => setSlow(e.target.value)} /></div>
            </>}
            <div><label className={labelCls}>Start Date</label><input type="date" className={inputCls} value={start} onChange={e => setStart(e.target.value)} /></div>
            <div><label className={labelCls}>End Date</label><input type="date" className={inputCls} value={end} onChange={e => setEnd(e.target.value)} /></div>
            <div><label className={labelCls}>Initial Capital</label><input type="number" className={inputCls} value={capital} onChange={e => setCapital(e.target.value)} /></div>
          </div>
          <button type="submit" disabled={run.isPending} className="w-full py-2 bg-primary text-primary-foreground text-sm rounded hover:opacity-90 disabled:opacity-50">
            {run.isPending ? "Running backtest…" : "Run Backtest"}
          </button>
          {run.isError && <p className="text-negative text-xs">{(run.error as any)?.response?.data?.detail}</p>}
        </form>
      </Card>

      {r?.status === "completed" && r.metrics && (
        <>
          <Card title="Performance Metrics">
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              <Metric label="Total Return" value={`${r.metrics.total_return_pct.toFixed(2)}%`} color={r.metrics.total_return_pct >= 0 ? "text-positive" : "text-negative"} />
              <Metric label="Ann. Return"  value={`${r.metrics.annualised_return_pct.toFixed(2)}%`} />
              <Metric label="Sharpe"       value={r.metrics.sharpe_ratio.toFixed(2)} />
              <Metric label="Max Drawdown" value={`${r.metrics.max_drawdown_pct.toFixed(2)}%`} color="text-negative" />
              <Metric label="Win Rate"     value={`${(r.metrics.win_rate * 100).toFixed(1)}%`} />
              <Metric label="Volatility"   value={`${r.metrics.annualised_volatility.toFixed(2)}%`} />
              <Metric label="Total Trades" value={`${r.metrics.total_trades}`} />
              <Metric label="Final Value"  value={`$${r.metrics.final_value.toLocaleString()}`} color="text-primary" />
            </div>
          </Card>

          {r.equity_curve && r.equity_curve.length > 0 && (
            <Card title="Equity Curve">
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
  const navigate = useNavigate();
  const [tab, setTab] = useState<Tab>("dcf");

  function analyseWithAI(agentId: string, context: unknown, message: string) {
    navigate("/ai", { state: { agentId, context, initialMessage: message } });
  }

  const tabs: { id: Tab; label: string }[] = [
    { id: "dcf",      label: "DCF Valuation" },
    { id: "var",      label: "VaR / Risk" },
    { id: "backtest", label: "Backtest" },
  ];

  return (
    <div className="min-h-screen bg-background p-6 space-y-6">
      <h1 className="text-2xl font-bold text-primary">Analytics</h1>

      <div className="flex gap-1 bg-secondary/30 p-1 rounded-lg w-fit">
        {tabs.map(t => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`px-4 py-1.5 text-sm rounded-md transition-colors ${
              tab === t.id
                ? "bg-card text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === "dcf" && (
        <DCFPanel
          onAnalyseWithAI={(ctx) =>
            analyseWithAI(
              "earnings_analyst",
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
