import { useState, FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Legend,
} from "recharts";
import {
  usePortfolios,
  usePortfolioDetail,
  useCreatePortfolio,
  useDeletePortfolio,
  useAddTransaction,
  useOptimise,
} from "@/hooks/usePortfolio";
import HoldingsTable from "@/components/portfolio/HoldingsTable";
import AllocationPie from "@/components/portfolio/AllocationPie";
import api from "@/lib/api";

// ── Add Transaction form ──────────────────────────────────────────
function AddTransactionForm({ portfolioId, onClose }: { portfolioId: string; onClose: () => void }) {
  const add = useAddTransaction(portfolioId);
  const [form, setForm] = useState({
    symbol: "", market: "US", tx_type: "buy",
    quantity: "", price: "", fx_rate: "1",
    tx_date: new Date().toISOString().slice(0, 10), notes: "",
  });
  const set = (k: string) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setForm((f) => ({ ...f, [k]: e.target.value }));

  async function submit(e: FormEvent) {
    e.preventDefault();
    await add.mutateAsync({
      symbol: form.symbol.toUpperCase(),
      market: form.market,
      tx_type: form.tx_type,
      quantity: parseFloat(form.quantity),
      price: parseFloat(form.price),
      fx_rate: parseFloat(form.fx_rate),
      tx_date: form.tx_date,
      notes: form.notes || undefined,
    } as any);
    onClose();
  }

  const input = "w-full px-3 py-1.5 rounded bg-input border border-border text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring";
  const label = "block text-xs text-muted-foreground mb-1";

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <form onSubmit={submit} className="bg-card border border-border rounded-lg p-6 w-full max-w-md space-y-4">
        <h3 className="text-foreground font-semibold">Add Transaction</h3>
        <div className="grid grid-cols-2 gap-3">
          <div><label className={label}>Symbol</label><input required className={input} value={form.symbol} onChange={set("symbol")} placeholder="AAPL / 2330" /></div>
          <div><label className={label}>Market</label>
            <select className={input} value={form.market} onChange={set("market")}>
              <option value="US">US</option><option value="TW">TW</option>
            </select>
          </div>
          <div><label className={label}>Type</label>
            <select className={input} value={form.tx_type} onChange={set("tx_type")}>
              <option value="buy">Buy</option><option value="sell">Sell</option><option value="dividend">Dividend</option>
            </select>
          </div>
          <div><label className={label}>Date</label><input type="date" required className={input} value={form.tx_date} onChange={set("tx_date")} /></div>
          <div><label className={label}>Quantity</label><input type="number" required min="0" step="any" className={input} value={form.quantity} onChange={set("quantity")} /></div>
          <div><label className={label}>Price</label><input type="number" required min="0" step="any" className={input} value={form.price} onChange={set("price")} /></div>
          <div><label className={label}>FX Rate</label><input type="number" min="0" step="any" className={input} value={form.fx_rate} onChange={set("fx_rate")} /></div>
          <div><label className={label}>Notes</label><input className={input} value={form.notes} onChange={set("notes")} /></div>
        </div>
        <div className="flex gap-3 justify-end">
          <button type="button" onClick={onClose} className="px-4 py-1.5 text-sm text-muted-foreground hover:text-foreground">Cancel</button>
          <button type="submit" disabled={add.isPending} className="px-4 py-1.5 text-sm bg-primary text-primary-foreground rounded hover:opacity-90 disabled:opacity-50">
            {add.isPending ? "Saving…" : "Add"}
          </button>
        </div>
      </form>
    </div>
  );
}

// ── Create Portfolio modal ────────────────────────────────────────
function CreatePortfolioModal({ onClose }: { onClose: () => void }) {
  const create = useCreatePortfolio();
  const [name, setName] = useState("");
  const [currency, setCurrency] = useState("USD");

  async function submit(e: FormEvent) {
    e.preventDefault();
    await create.mutateAsync({ name, currency });
    onClose();
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <form onSubmit={submit} className="bg-card border border-border rounded-lg p-6 w-full max-w-sm space-y-4">
        <h3 className="text-foreground font-semibold">New Portfolio</h3>
        <div><label className="block text-xs text-muted-foreground mb-1">Name</label>
          <input required className="w-full px-3 py-1.5 rounded bg-input border border-border text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring" value={name} onChange={(e) => setName(e.target.value)} /></div>
        <div><label className="block text-xs text-muted-foreground mb-1">Base Currency</label>
          <select className="w-full px-3 py-1.5 rounded bg-input border border-border text-sm text-foreground" value={currency} onChange={(e) => setCurrency(e.target.value)}>
            <option value="USD">USD</option><option value="TWD">TWD</option>
          </select></div>
        <div className="flex gap-3 justify-end">
          <button type="button" onClick={onClose} className="px-4 py-1.5 text-sm text-muted-foreground hover:text-foreground">Cancel</button>
          <button type="submit" disabled={create.isPending} className="px-4 py-1.5 text-sm bg-primary text-primary-foreground rounded hover:opacity-90 disabled:opacity-50">Create</button>
        </div>
      </form>
    </div>
  );
}

// ── Performance chart ─────────────────────────────────────────────

const PERIOD_DAYS: Record<string, number> = { "1M": 30, "3M": 90, "6M": 180, "1Y": 365 };
const PERIOD_TO_YFINANCE: Record<string, string> = { "1M": "1mo", "3M": "3mo", "6M": "6mo", "1Y": "1y" };

function normalize(points: { date: string; value: number }[]): { date: string; indexed: number }[] {
  if (!points.length) return [];
  const base = points[0].value;
  if (base === 0) return [];
  return points.map((p) => ({ date: p.date.slice(5), indexed: parseFloat(((p.value / base) * 100).toFixed(2)) }));
}

function PerformanceChart({ portfolioId }: { portfolioId: string }) {
  const [period, setPeriod] = useState("3M");
  const days = PERIOD_DAYS[period];
  const yfinancePeriod = PERIOD_TO_YFINANCE[period];

  const { data: perfData = [], isLoading: perfLoading } = useQuery<{ date: string; value: number }[]>({
    queryKey: ["portfolio-performance", portfolioId, days],
    queryFn: () =>
      api.get(`/portfolio/${portfolioId}/performance?days=${days}`).then((r) => r.data),
    staleTime: 3_600_000,
  });

  const { data: spyHistory = [] } = useQuery<{ date: string; close: number }[]>({
    queryKey: ["spy-history", yfinancePeriod],
    queryFn: () =>
      api.get(`/us/history/SPY?period=${yfinancePeriod}&interval=1d`).then((r) => r.data),
    staleTime: 3_600_000,
  });

  const portfolioIndexed = normalize(perfData);

  const spyIndexed = (() => {
    if (!spyHistory.length) return [];
    const base = spyHistory[0].close;
    if (base === 0) return [];
    return spyHistory.map((p) => ({ date: p.date?.slice(0, 10).slice(5), spy: parseFloat(((p.close / base) * 100).toFixed(2)) }));
  })();

  // Merge on date key
  const merged: Record<string, { date: string; portfolio?: number; spy?: number }> = {};
  for (const p of portfolioIndexed) merged[p.date] = { date: p.date, portfolio: p.indexed };
  for (const s of spyIndexed) {
    if (merged[s.date]) merged[s.date].spy = s.spy;
    else merged[s.date] = { date: s.date, spy: s.spy };
  }
  const chartData = Object.values(merged).sort((a, b) => a.date.localeCompare(b.date));

  const hasPerfData = portfolioIndexed.length > 0;

  return (
    <div className="bg-card border border-border rounded-lg p-5 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-foreground font-medium">Performance</h2>
          {hasPerfData && (
            <p className="text-[10px] text-muted-foreground">Indexed to 100 at start of period</p>
          )}
        </div>
        <div className="flex gap-1">
          {Object.keys(PERIOD_DAYS).map((p) => (
            <button
              key={p}
              onClick={() => setPeriod(p)}
              className={`px-2.5 py-1 text-xs rounded transition-colors ${
                period === p
                  ? "bg-primary text-primary-foreground"
                  : "text-muted-foreground hover:text-foreground"
              }`}
            >
              {p}
            </button>
          ))}
        </div>
      </div>

      {perfLoading ? (
        <div className="h-[200px] flex items-center justify-center text-muted-foreground text-sm animate-pulse">
          Loading…
        </div>
      ) : !hasPerfData ? (
        <div className="h-[200px] flex items-center justify-center text-muted-foreground text-sm">
          No snapshot data yet — snapshots are recorded daily at 23:00 UTC.
        </div>
      ) : (
        <ResponsiveContainer width="100%" height={200}>
          <LineChart data={chartData} margin={{ left: 0, right: 8, top: 4, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
            <XAxis
              dataKey="date"
              tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
              interval="preserveStartEnd"
            />
            <YAxis
              tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
              width={44}
              tickFormatter={(v) => v.toFixed(0)}
              domain={["auto", "auto"]}
            />
            <Tooltip
              contentStyle={{
                background: "hsl(var(--card))",
                border: "1px solid hsl(var(--border))",
                fontSize: 11,
              }}
              formatter={(v: number, name: string) => [`${v.toFixed(2)}`, name]}
            />
            <Legend wrapperStyle={{ fontSize: 11 }} />
            <Line
              type="monotone"
              dataKey="portfolio"
              name="Portfolio"
              stroke="hsl(var(--primary))"
              strokeWidth={2}
              dot={false}
              connectNulls
            />
            <Line
              type="monotone"
              dataKey="spy"
              name="SPY (benchmark)"
              stroke="#94a3b8"
              strokeWidth={1.5}
              strokeDasharray="4 2"
              dot={false}
              connectNulls
            />
          </LineChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}

// ── CSV export ────────────────────────────────────────────────────

function exportCSV(rows: Record<string, unknown>[], filename: string) {
  if (!rows.length) return;
  const headers = Object.keys(rows[0]);
  const csv = [
    headers.join(","),
    ...rows.map((r) =>
      headers
        .map((h) => {
          const v = r[h];
          const s = String(v ?? "");
          return s.includes(",") || s.includes('"') ? `"${s.replace(/"/g, '""')}"` : s;
        })
        .join(",")
    ),
  ].join("\n");
  const blob = new Blob([csv], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

// ── Transaction history ───────────────────────────────────────────

interface TransactionRow {
  id: string;
  symbol: string;
  market: string;
  tx_type: string;
  quantity: number;
  price: number;
  fx_rate: number;
  tx_date: string;
  notes: string | null;
  created_at: string;
}

function TransactionHistory({ portfolioId }: { portfolioId: string }) {
  const { data: txns = [], isLoading } = useQuery<TransactionRow[]>({
    queryKey: ["portfolio-transactions", portfolioId],
    queryFn: () =>
      api.get(`/portfolio/${portfolioId}/transactions?limit=200`).then((r) => r.data),
    staleTime: 30_000,
  });

  function handleExport() {
    exportCSV(
      txns.map((t) => ({
        date: t.tx_date,
        symbol: t.symbol,
        market: t.market,
        type: t.tx_type,
        quantity: t.quantity,
        price: t.price,
        fx_rate: t.fx_rate,
        value: t.quantity * t.price,
        notes: t.notes ?? "",
      })),
      `transactions-${portfolioId}.csv`
    );
  }

  return (
    <div className="bg-card border border-border rounded-lg p-5 space-y-3">
      <div className="flex items-center justify-between">
        <h2 className="text-foreground font-medium">Transaction History</h2>
        <button
          onClick={handleExport}
          disabled={!txns.length}
          className="text-xs text-primary hover:underline disabled:opacity-40"
        >
          Export CSV
        </button>
      </div>

      {isLoading && <p className="text-xs text-muted-foreground animate-pulse">Loading…</p>}
      {!isLoading && txns.length === 0 && (
        <p className="text-xs text-muted-foreground py-4 text-center">No transactions yet.</p>
      )}

      {txns.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-border text-muted-foreground">
                <th className="text-left py-2 pr-4 font-medium">Date</th>
                <th className="text-left py-2 px-2 font-medium">Symbol</th>
                <th className="text-left py-2 px-2 font-medium">Mkt</th>
                <th className="text-left py-2 px-2 font-medium">Type</th>
                <th className="text-right py-2 px-2 font-medium">Qty</th>
                <th className="text-right py-2 px-2 font-medium">Price</th>
                <th className="text-right py-2 px-2 font-medium">Value</th>
                <th className="text-left py-2 pl-4 font-medium">Notes</th>
              </tr>
            </thead>
            <tbody>
              {txns.map((t) => (
                <tr key={t.id} className="border-b border-border/30 hover:bg-accent/5">
                  <td className="py-1.5 pr-4 text-muted-foreground">{t.tx_date}</td>
                  <td className="py-1.5 px-2 font-medium text-primary">{t.symbol}</td>
                  <td className="py-1.5 px-2 text-muted-foreground">{t.market}</td>
                  <td className={`py-1.5 px-2 font-medium capitalize ${
                    t.tx_type === "buy"      ? "text-green-400"
                    : t.tx_type === "sell"   ? "text-red-400"
                    : "text-amber-400"
                  }`}>{t.tx_type}</td>
                  <td className="py-1.5 px-2 text-right text-foreground">
                    {t.quantity.toLocaleString()}
                  </td>
                  <td className="py-1.5 px-2 text-right text-foreground">
                    {t.price.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                  </td>
                  <td className="py-1.5 px-2 text-right text-muted-foreground">
                    {(t.quantity * t.price).toLocaleString(undefined, { maximumFractionDigits: 0 })}
                  </td>
                  <td className="py-1.5 pl-4 text-muted-foreground max-w-[160px] truncate">
                    {t.notes ?? ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ── Main page ─────────────────────────────────────────────────────
export default function PortfolioPage() {
  const { data: portfolios, isLoading } = usePortfolios();
  const [selected, setSelected] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [showAddTx, setShowAddTx] = useState(false);
  const [showOptimise, setShowOptimise] = useState(false);

  const { data: detail, isFetching } = usePortfolioDetail(selected);
  const deleteP = useDeletePortfolio();
  const optimise = useOptimise(selected ?? "");

  const navigate = useNavigate();
  const activeId = selected ?? portfolios?.[0]?.id ?? null;

  function analyseWithAI() {
    navigate("/ai", {
      state: {
        agentId: "portfolio_advisor",
        initialMessage: "Review my portfolio and suggest improvements to the allocation, risk profile, and any concentration risks.",
        context: { portfolio: detail ?? null },
      },
    });
  }

  return (
    <div className="min-h-screen bg-background p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-primary">Portfolio</h1>
        <div className="flex gap-2">
          {detail && (
            <button
              onClick={analyseWithAI}
              className="px-4 py-2 text-sm bg-primary/10 border border-primary/30 text-primary rounded-md hover:bg-primary/20 transition-colors"
            >
              🤖 Analyse with AI
            </button>
          )}
          <button onClick={() => setShowCreate(true)} className="px-4 py-2 text-sm bg-primary text-primary-foreground rounded-md hover:opacity-90">
            + New Portfolio
          </button>
        </div>
      </div>

      {isLoading && <p className="text-muted-foreground text-sm">Loading…</p>}

      {/* Portfolio selector tabs */}
      {portfolios && portfolios.length > 0 && (
        <div className="flex gap-2 flex-wrap">
          {portfolios.map((p) => (
            <button
              key={p.id}
              onClick={() => setSelected(p.id)}
              className={`px-4 py-1.5 text-sm rounded-full border transition-colors ${
                (selected ?? portfolios[0].id) === p.id
                  ? "bg-primary text-primary-foreground border-primary"
                  : "border-border text-muted-foreground hover:text-foreground"
              }`}
            >
              {p.name} <span className="text-xs opacity-60">{p.currency}</span>
            </button>
          ))}
        </div>
      )}

      {/* Portfolio detail */}
      {activeId && (
        <PortfolioDetail
          portfolioId={activeId}
          detail={detail}
          isFetching={isFetching}
          onAddTx={() => setShowAddTx(true)}
          onOptimise={() => setShowOptimise(true)}
          onDelete={async () => {
            await deleteP.mutateAsync(activeId);
            setSelected(null);
          }}
          optimiseResult={optimise.data}
          optimisePending={optimise.isPending}
          onRunOptimise={(risk) => optimise.mutate({ target_risk: risk, max_weight: 1 })}
        />
      )}

      {!portfolios?.length && !isLoading && (
        <div className="text-center py-16 text-muted-foreground">
          <p className="text-lg">No portfolios yet.</p>
          <p className="text-sm mt-1">Create one to start tracking your investments.</p>
        </div>
      )}

      {showCreate && <CreatePortfolioModal onClose={() => setShowCreate(false)} />}
      {showAddTx && activeId && <AddTransactionForm portfolioId={activeId} onClose={() => setShowAddTx(false)} />}
    </div>
  );
}

// ── Portfolio detail panel ────────────────────────────────────────
function PortfolioDetail({
  portfolioId, detail, isFetching,
  onAddTx, onOptimise, onDelete,
  optimiseResult, optimisePending, onRunOptimise,
}: any) {
  const [detailTab, setDetailTab] = useState<"overview" | "transactions">("overview");

  if (!detail) return <div className="text-muted-foreground text-sm">{isFetching ? "Loading…" : ""}</div>;

  const pnlPositive = detail.total_pnl >= 0;

  function exportHoldings() {
    exportCSV(
      detail.holdings.map((h: any) => ({
        symbol: h.symbol,
        market: h.market,
        quantity: h.quantity,
        avg_cost: h.avg_cost,
        current_price: h.current_price,
        current_value: h.current_value,
        unrealized_pnl: h.unrealized_pnl,
        unrealized_pnl_pct: h.unrealized_pnl_pct,
        weight_pct: h.weight_pct,
      })),
      `holdings-${portfolioId}.csv`
    );
  }

  return (
    <div className="space-y-6">
      {/* Summary cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {[
          { label: "Total Value", value: `${detail.currency} ${detail.total_value.toLocaleString()}` },
          { label: "Total Cost",  value: `${detail.currency} ${detail.total_cost.toLocaleString()}` },
          {
            label: "Unrealized P&L",
            value: `${pnlPositive ? "+" : ""}${detail.total_pnl.toLocaleString(undefined, { maximumFractionDigits: 0 })}`,
            color: pnlPositive ? "text-positive" : "text-negative",
          },
          {
            label: "Return",
            value: `${pnlPositive ? "+" : ""}${detail.total_pnl_pct.toFixed(2)}%`,
            color: pnlPositive ? "text-positive" : "text-negative",
          },
        ].map((c) => (
          <div key={c.label} className="bg-card border border-border rounded-lg p-4">
            <p className="text-xs text-muted-foreground">{c.label}</p>
            <p className={`text-lg font-semibold mt-1 ${c.color ?? "text-foreground"}`}>{c.value}</p>
          </div>
        ))}
      </div>

      {/* Tab selector */}
      <div className="flex gap-1 bg-secondary/30 p-1 rounded-lg w-fit">
        {(["overview", "transactions"] as const).map((t) => (
          <button
            key={t}
            onClick={() => setDetailTab(t)}
            className={`px-4 py-1.5 text-sm rounded-md transition-colors capitalize ${
              detailTab === t
                ? "bg-card text-foreground shadow-sm"
                : "text-muted-foreground hover:text-foreground"
            }`}
          >
            {t === "overview" ? "Overview" : "Transactions"}
          </button>
        ))}
      </div>

      {detailTab === "overview" && (
        <>
          {/* Holdings + pie */}
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
            <div className="lg:col-span-2 bg-card border border-border rounded-lg p-5">
              <div className="flex items-center justify-between mb-4">
                <h2 className="text-foreground font-medium">Holdings</h2>
                <div className="flex gap-2">
                  <button
                    onClick={exportHoldings}
                    disabled={!detail.holdings.length}
                    className="text-xs text-primary hover:underline disabled:opacity-40"
                  >
                    Export CSV
                  </button>
                  <button onClick={onAddTx} className="text-xs px-3 py-1 bg-primary/10 text-primary rounded hover:bg-primary/20">
                    + Transaction
                  </button>
                </div>
              </div>
              <HoldingsTable holdings={detail.holdings} currency={detail.currency} />
            </div>

            <div className="bg-card border border-border rounded-lg p-5">
              <h2 className="text-foreground font-medium mb-2">Allocation</h2>
              <AllocationPie holdings={detail.holdings} />
            </div>
          </div>

          {/* Performance chart */}
          <PerformanceChart portfolioId={portfolioId} />

      {/* Optimiser */}
      <div className="bg-card border border-border rounded-lg p-5 space-y-4">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-foreground font-medium">Portfolio Optimiser</h2>
            <p className="text-xs text-muted-foreground mt-0.5">Mean-variance (max Sharpe). Suggested weights only — no trades executed.</p>
          </div>
          <div className="flex gap-2">
            {(["low","medium","high"] as const).map((risk) => (
              <button
                key={risk}
                onClick={() => onRunOptimise(risk)}
                disabled={optimisePending}
                className="px-3 py-1 text-xs border border-border rounded hover:bg-secondary/50 disabled:opacity-40 capitalize"
              >
                {risk}
              </button>
            ))}
          </div>
        </div>

        {optimisePending && <p className="text-sm text-muted-foreground animate-pulse">Optimising…</p>}

        {optimiseResult && (
          <div className="space-y-3">
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              {[
                { k: "Expected Return", v: `${(optimiseResult.metrics.expected_annual_return * 100).toFixed(1)}%` },
                { k: "Volatility",      v: `${(optimiseResult.metrics.annual_volatility * 100).toFixed(1)}%` },
                { k: "Sharpe",          v: optimiseResult.metrics.sharpe_ratio.toFixed(2) },
                { k: "Max Drawdown",    v: `${(optimiseResult.metrics.max_drawdown * 100).toFixed(1)}%` },
              ].map((m) => (
                <div key={m.k} className="bg-secondary/30 rounded p-3">
                  <p className="text-xs text-muted-foreground">{m.k}</p>
                  <p className="text-sm font-medium text-foreground mt-0.5">{m.v}</p>
                </div>
              ))}
            </div>
            <div className="flex flex-wrap gap-2">
              {Object.entries(optimiseResult.weights).map(([sym, w]: [string, any]) => (
                <span key={sym} className="text-xs bg-secondary/40 px-2 py-1 rounded">
                  {sym} <span className="text-primary font-medium">{(w * 100).toFixed(1)}%</span>
                </span>
              ))}
            </div>
          </div>
        )}
      </div>

      <div className="flex justify-end">
        <button
          onClick={onDelete}
          className="text-xs text-negative/70 hover:text-negative"
        >
          Delete portfolio
        </button>
      </div>
        </>
      )}

      {detailTab === "transactions" && (
        <TransactionHistory portfolioId={portfolioId} />
      )}
    </div>
  );
}
