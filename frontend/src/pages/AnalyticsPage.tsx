import { useState, FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Bot } from "lucide-react";
import api from "@/lib/api";
import { Card, Metric, inputCls, labelCls } from "./analytics/_shared";
import { BacktestPanel } from "./analytics/BacktestPanel";

type Tab = "dcf" | "var" | "backtest";

// ── Shared helpers ────────────────────────────────────────────────
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
      <h1 className="text-title font-semibold text-foreground">{t("analytics.title")}</h1>

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
