import { useState, FormEvent } from "react";
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

  const activeId = selected ?? portfolios?.[0]?.id ?? null;

  return (
    <div className="min-h-screen bg-background p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-primary">Portfolio</h1>
        <button onClick={() => setShowCreate(true)} className="px-4 py-2 text-sm bg-primary text-primary-foreground rounded-md hover:opacity-90">
          + New Portfolio
        </button>
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
  if (!detail) return <div className="text-muted-foreground text-sm">{isFetching ? "Loading…" : ""}</div>;

  const pnlPositive = detail.total_pnl >= 0;

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

      {/* Holdings + pie */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-2 bg-card border border-border rounded-lg p-5">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-foreground font-medium">Holdings</h2>
            <button onClick={onAddTx} className="text-xs px-3 py-1 bg-primary/10 text-primary rounded hover:bg-primary/20">
              + Transaction
            </button>
          </div>
          <HoldingsTable holdings={detail.holdings} currency={detail.currency} />
        </div>

        <div className="bg-card border border-border rounded-lg p-5">
          <h2 className="text-foreground font-medium mb-2">Allocation</h2>
          <AllocationPie holdings={detail.holdings} />
        </div>
      </div>

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
    </div>
  );
}
